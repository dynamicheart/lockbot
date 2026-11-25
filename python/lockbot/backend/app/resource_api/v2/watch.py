"""Capability-scoped resource snapshot updates for API clients."""

import hashlib
import json
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from functools import partial
from typing import Annotated, Literal
from uuid import uuid4

import anyio
from fastapi import Header, HTTPException, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from lockbot.backend.app import database
from lockbot.backend.app.resource_api.idle_recovery import CONFIRMATION_SECONDS
from lockbot.backend.app.resource_api.resource_changes import (
    MAX_RESOURCE_STREAMS,
    MAX_RESOURCE_STREAMS_PER_IDENTITY,
    resource_changes,
)
from lockbot.backend.app.resource_api.v2.binding import active_resource_grants, api_error, require_principal, router
from lockbot.backend.app.resource_api.v2.identity import ResourceIdentity
from lockbot.backend.app.resource_api.v2.resources import check_budget, resource_page

_HEARTBEAT_SECONDS = 20
_MAX_CHECKPOINTS = 1024
_FIELDS = "grant_ref,source,node,device,unit,hardware,status,available,holding,booking"
_active = Counter()
_checkpoints = OrderedDict()
_workers = anyio.CapacityLimiter(8)


def _identify(request):
    with database.SessionLocal() as db:
        principal = require_principal(request, db)
        check_budget(principal.public_id, "read")
        return (principal.public_id, principal.generation), principal.external_user_id


def _read_view(request, identity, filters, rows=None, grants=None, unavailable=None, changed=None):
    with database.SessionLocal() as db:
        principal = require_principal(request, db)
        if (principal.public_id, principal.generation) != identity:
            raise api_error("credential_invalid", 401)

        ref = filters.get("resource_ref")
        item = db.get(ResourceIdentity, ref) if ref else None
        if ref and item is None:
            raise api_error("resource_unavailable", 404)
        current = {
            bot.id: grant.public_id
            for grant, bot in active_resource_grants(
                db, principal, grant_ref=filters.get("grant_ref"), bot_id=item.bot_id if item else None
            )
        }
        db.rollback()

        if rows is None or current != grants or changed is None or None in unavailable:
            page = resource_page(
                request, db, **filters, limit=100, fields=_FIELDS, charge_budget=False, allow_empty_match=True
            )
            rows = {row["resource_ref"]: row for row in page["rows"]}
            unavailable = set(page["unavailable"])
            if page["unavailable_count"] > len(unavailable):
                unavailable.add(None)
        else:
            affected = {current[bot] for bot in changed if bot in current}
            rows = {key: row for key, row in rows.items() if row["grant_ref"] not in affected}
            unavailable = unavailable - affected
            page = {"next_cursor": None}
            for grant_ref in affected:
                page = resource_page(
                    request,
                    db,
                    **{**filters, "grant_ref": grant_ref},
                    limit=100,
                    fields=_FIELDS,
                    charge_budget=False,
                    allow_empty_match=True,
                )
                rows.update((row["resource_ref"], row) for row in page["rows"])
                unavailable.update(page["unavailable"])
                if page["next_cursor"]:
                    break
        if page["next_cursor"] or len(rows) > 100:
            raise api_error("watch_scope_too_large", 422)
        return rows, current, unavailable


def _frame(event, data, cursor=None):
    prefix = f"id: {cursor}\n" if cursor else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _idle_notices(recovery, user_id, grants, rows):
    if recovery is None:
        return {}
    scope = {(row["grant_ref"], row["node"], row["device"]): row["resource_ref"] for row in rows.values()}
    visible = {}
    for notice in recovery.for_holder(user_id, grants.keys(), channel="api"):
        bot_id, node, card = notice.resource
        resource_ref = scope.get((grants[bot_id], node, card))
        if resource_ref is not None:
            visible[notice.ref] = (notice, resource_ref)
    return visible


def _checkpoint(scope, rows, unavailable):
    key = uuid4().hex
    _checkpoints[key] = (scope, time.monotonic(), rows, unavailable)
    while len(_checkpoints) > _MAX_CHECKPOINTS:
        _checkpoints.popitem(last=False)
    return key


def _ended_in_scope(grants, rows, user_id, events):
    selected = {}
    for row in rows.values():
        node, device = row["node"], row["device"]
        resource = node if device is None else f"{node}:dev{device}"
        selected[(row["grant_ref"], resource)] = row

    ended = []
    for bot_id, resource, actor, kind, deadline in events:
        if actor != user_id:
            continue
        row = selected.get((grants.get(bot_id), resource))
        if row is None:
            continue
        field = "holding" if kind in ("holding_expired", "holding_reclaimed") else "booking"
        if field in row:
            ended.append(
                {
                    "resource_ref": row["resource_ref"],
                    "kind": kind,
                    "deadline": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
                }
            )
    return ended


def _update(scope, rows, unavailable, before=None, *, ended=()):
    cursor = _checkpoint(scope, rows, unavailable)
    visible = {"complete": not unavailable, "unavailable": sorted(ref for ref in unavailable if ref is not None)}
    if before is None:
        return _frame("snapshot", {**visible, "rows": list(rows.values())}, cursor)
    change = {
        **visible,
        "rows": [row for key, row in rows.items() if before.get(key) != row],
        "removed": [key for key in before if key not in rows],
    }
    if ended:
        change["ended"] = ended
    return _frame("resource.changed", change, cursor)


@router.get(
    "/resources/events",
    responses={200: {"content": {"text/event-stream": {}}, "description": "Authorized resource updates"}},
    response_class=StreamingResponse,
)
async def watch_resources(
    request: Request,
    model: Annotated[str | None, Query(min_length=1, max_length=96)] = None,
    unit: Literal["any", "node", "card"] = "any",
    available: bool | None = None,
    mine: bool = False,
    node: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    grant_ref: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    resource_ref: Annotated[str | None, Query(pattern=r"^res_[0-9a-f]{32}$")] = None,
    last_event_id: Annotated[str | None, Header(max_length=64)] = None,
):
    """Read one snapshot and subsequent changes within the chosen resource scope."""
    filters = dict(
        model=model,
        unit=unit,
        available=available,
        mine=mine,
        node=node,
        grant_ref=grant_ref,
        resource_ref=resource_ref,
    )
    identity, user_id = await anyio.to_thread.run_sync(_identify, request, limiter=_workers)
    if _active[identity[0]] >= MAX_RESOURCE_STREAMS_PER_IDENTITY or _active.total() >= MAX_RESOURCE_STREAMS:
        raise HTTPException(503, {"code": "resource_watch_busy"}, headers={"Retry-After": "5"})
    _active[identity[0]] += 1
    released = False

    async def release():
        nonlocal released
        if not released:
            released = True
            _active[identity[0]] -= 1
            if _active[identity[0]] == 0:
                del _active[identity[0]]

    scope = (identity, hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest())
    sequence = resource_changes.current()
    try:
        rows, grants, unavailable = await anyio.to_thread.run_sync(
            _read_view, request, identity, filters, limiter=_workers
        )
    except BaseException:
        await release()
        raise

    async def stream():
        nonlocal sequence, rows, grants, unavailable
        app_state = getattr(request.scope.get("app"), "state", None)
        observer = getattr(app_state, "idle_observer", None)
        recovery = observer.recovery if observer else None
        sent_notices = set()
        try:
            with resource_changes.listen():
                previous = _checkpoints.get(last_event_id)
                resume = previous and previous[0] == scope and time.monotonic() - previous[1] <= 120
                yield _update(scope, rows, unavailable, previous[2] if resume else None)
                checked_at = time.monotonic()
                while not await request.is_disconnected():
                    notices = _idle_notices(recovery, user_id, grants, rows)
                    sent_notices.intersection_update(notices)
                    for ref, (notice, target_ref) in notices.items():
                        if ref in sent_notices:
                            continue
                        yield _frame(
                            "idle.notice",
                            {
                                "resource_ref": target_ref,
                                "notice_ref": ref,
                                "confirm_within_seconds": CONFIRMATION_SECONDS,
                                "confirm_by": notice.confirm_by,
                                "review_at": notice.review_at,
                                "confirmed": notice.confirmed,
                                "utilization_percent": notice.utilization_percent,
                                "threshold_percent": notice.threshold_percent,
                            },
                        )
                        recovery.delivered(notice)
                        sent_notices.add(ref)
                    sequence, changed, ends = await resource_changes.after(sequence, _HEARTBEAT_SECONDS)
                    if (
                        changed is not None
                        and not changed.intersection(grants)
                        and time.monotonic() - checked_at < _HEARTBEAT_SECONDS - 0.1
                    ):
                        continue
                    try:
                        new_rows, new_grants, new_unavailable = await anyio.to_thread.run_sync(
                            partial(_read_view, request, identity, filters, rows, grants, unavailable, changed),
                            limiter=_workers,
                        )
                    except HTTPException as exc:
                        yield _frame("error", exc.detail)
                        break
                    checked_at = time.monotonic()
                    ended = _ended_in_scope(new_grants, rows, user_id, ends) if ends else ()
                    if rows != new_rows or unavailable != new_unavailable or ended:
                        yield _update(scope, new_rows, new_unavailable, rows, ended=ended)
                    else:
                        yield ": heartbeat\n\n"
                    rows, grants, unavailable = new_rows, new_grants, new_unavailable
        finally:
            await release()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        background=BackgroundTask(release),
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
