"""Permission-scoped, compact native resource discovery for Agent API v2."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.datasource import agent_metrics_source
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.node_access import node_login_address, saved_node_access
from lockbot.backend.app.bots.node_inventory import inventory_document, resource_node_hardware
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.snapshot import QUEUE_CLAIM_WINDOW_SECONDS, build_resources
from lockbot.backend.app.resource_api.traffic import credential_budget
from lockbot.backend.app.resource_api.v2.binding import (
    PersonalGrant,
    active_resource_grants,
    active_resource_versions,
    api_error,
    grant_active,
    require_principal,
    router,
)
from lockbot.backend.app.resource_api.v2.identity import CARD_BOTS, NODE, ResourceIdentity, config_units, identities
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.core.config import normalize_label_settings
from lockbot.core.platforms.feishu import FeishuClient

_SUPPORTED = {"lock", "slock", "book", "free", "label"}
_ROW_FIELDS = {
    "resource_ref",
    "grant_ref",
    "source",
    "platform",
    "node",
    "device",
    "unit",
    "label",
    "node_access",
    "node_attributes",
    "hardware",
    "status",
    "available",
    "operations",
    "metrics",
    "holding",
    "booking",
    "holders",
    "queue",
    "estimated_seconds",
}
_CATALOG_FIELDS = {"resource_ref", "grant_ref", "source", "platform", "node", "device", "unit", "label", "hardware"}
_MAX_PAGE_BYTES = 8 * 1024
_MAX_PEOPLE_PAGE_BYTES = 64 * 1024


def _resource_people(raw, principal, client, observed):
    from lockbot.backend.app.resource_api.task_categories import validate_task_category

    def person(entry):
        identity = entry.get("user_id")
        if principal.platform == "feishu":
            name = client.cached_user_name(identity) if isinstance(client, FeishuClient) else None
        else:
            name = identity
        person = {"name": name, "is_you": identity == principal.external_user_id}
        if category := validate_task_category(entry.get("task_category")):
            person["task_category"] = category
        return person

    def timestamp(epoch):
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch > 0 else None

    now = observed.timestamp()
    holders = []
    for user in raw.get("current_users") or ():
        start = user.get("start_time") or 0
        duration = user.get("duration") or 0
        end = start + duration if start > 0 else 0
        holders.append(
            {
                **person(user),
                "locked_at": timestamp(start),
                "expires_at": timestamp(end),
                "duration_seconds": duration,
                "remaining_seconds": max(0, math.ceil(end - now)) if end else None,
            }
        )

    queue = []
    waiting = _remaining_lock_seconds(raw, now)
    for index, user in enumerate(raw.get("booking_list") or (), start=1):
        notified = bool(user.get("is_notified"))
        start = user.get("start_time") or 0
        booked_at = (user.get("booked_start_time") or 0) if notified else (user.get("booked_start_time") or start)
        duration = user.get("duration") or 0
        claim_by = start + QUEUE_CLAIM_WINDOW_SECONDS if notified and start > 0 and index == 1 and not holders else 0
        if claim_by and claim_by <= now:
            waiting = None
        queue.append(
            {
                **person(user),
                "position": index,
                "queued_at": timestamp(booked_at),
                "waited_seconds": max(0, int(now - booked_at)) if booked_at > 0 else None,
                "estimated_wait_seconds": math.ceil(waiting) if waiting is not None else None,
                "duration_seconds": duration,
                "claim_expires_at": timestamp(claim_by) if claim_by > now else None,
            }
        )
        if waiting is not None:
            waiting = waiting + duration + max(0, claim_by - now) if duration > 0 else None
    return {"holders": holders, "queue": queue}


def fresh_grant(db, request, principal_id, grant_id):
    db.rollback()
    principal = require_principal(request, db)
    return _current_group(db, principal, principal_id, grant_id)


def _current_group(db, principal, principal_id, grant_id):
    grant = db.get(PersonalGrant, grant_id)
    bot = db.get(Bot, grant.bot_id) if grant else None
    if principal.public_id != principal_id or grant is None or not grant_active(db, principal, grant, bot):
        raise api_error("resource_unavailable", 404)
    return principal, grant, bot


def check_budget(principal_id, kind):
    allowed, retry_after = credential_budget.check(("agent_v2", principal_id), kind)
    if not allowed:
        raise errors.rate_limited(retry_after)


def _remaining_lock_seconds(raw, now):
    holders = raw.get("current_users") or ()
    if raw.get("status") != "idle" and not holders:
        return None
    expiries = [x.get("start_time", 0) + x.get("duration", 0) for x in holders]
    if any(expiry <= now for expiry in expiries):
        return None
    return max(expiries, default=now) - now


def _estimated_wait(raw, user_id, captured_at):
    now = captured_at.timestamp()
    holders = raw.get("current_users") or ()
    queue = raw.get("booking_list") or ()
    remaining = _remaining_lock_seconds(raw, now)
    if remaining is None or any(user.get("user_id") == user_id for user in holders):
        return None
    ahead = next((i for i, item in enumerate(queue) if item.get("user_id") == user_id), len(queue))
    durations = [item.get("duration") for item in queue[:ahead]]
    if any(type(duration) is not int or duration <= 0 for duration in durations):
        return None
    window = 0
    if ahead and not holders and queue[0].get("is_notified"):
        window = queue[0].get("start_time", 0) + QUEUE_CLAIM_WINDOW_SECONDS - now
        if window <= 0:
            return None
    return math.ceil(remaining + sum(durations) + window)


def booking_ref(resource_ref, principal_id, raw):
    identity = raw.get("holding_id")
    if not identity:
        # The queue records the original timestamp before opening a claim window.
        identity = ["legacy", raw["user_id"], raw.get("booked_start_time", raw["start_time"]), raw["duration"]]
    value = json.dumps([resource_ref, principal_id, identity])
    return f"book_{resource_ref[4:]}_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def project_rows(bot, grant, principal, commands, aliases, refs, observed, state, *, hardware):
    caller = principal.external_user_id
    supported = sorted(_SUPPORTED.intersection(commands))
    public_states = build_resources(
        captured_at=observed, state=state, bot_type=bot.bot_type, platform=principal.platform, caller_id=caller
    )
    devices = bot.bot_type in CARD_BOTS
    metrics_source = agent_metrics_source(bot)
    mapped_nodes = metrics_source["nodeMapping"] if metrics_source else {}
    mapped_devices = metrics_source["deviceMapping"] if metrics_source else {}
    rows = []
    for node in public_states:
        name = node["id"]
        units = node["devices"] if devices else (node,)
        raw_units = state[name] if devices else (state[name],)
        for unit, raw in zip(units, raw_units, strict=True):
            slot = unit["id"] if devices else NODE
            ref = refs.get((name, slot))
            if ref is None:
                continue
            models = (
                ([{"model": unit["dev_model"], "count": 1}] if unit.get("dev_model") else [])
                if devices
                else (hardware.get(name, []))
            )
            own_hold = next((x for x in unit["holders"] if x["external_user_id"] == caller), None)
            own_booking = next((x for x in raw.get("booking_list", []) if x.get("user_id") == caller), None)
            state_name = unit["lock_status"]
            row = {
                "resource_ref": ref,
                "grant_ref": grant.public_id,
                "source": bot.name,
                "platform": principal.platform,
                "node": name,
                "device": slot if devices else None,
                "unit": "card" if devices else "node",
                "label": aliases.get(name),
                "hardware": models or None,
                "status": state_name,
                "available": state_name == "idle" and not raw.get("booking_list"),
                "operations": supported,
                "metrics": "unavailable",
            }
            if name in mapped_nodes and (not devices or f"{name}:dev{slot}" in mapped_devices):
                row["metrics"] = "ready"
            if own_hold is not None:
                live_hold = next(user for user in raw["current_users"] if user["user_id"] == caller)
                row["holding"] = {
                    "expires_at": own_hold["expires_at"],
                    "mode": state_name,
                    "renewal_due": bool(live_hold.get("is_notified")),
                }
            if own_booking is not None:
                claim_seconds = (
                    own_booking["start_time"] + QUEUE_CLAIM_WINDOW_SECONDS if own_booking.get("is_notified") else 0
                )
                claim_expired = claim_seconds and observed.timestamp() >= claim_seconds
                booking = row["booking"] = {
                    "booking_ref": booking_ref(ref, principal.public_id, own_booking),
                    "position": unit["caller_queue_position"] + 1,
                    "claimable": unit["claimable_by_you"] and not claim_expired,
                }
                if booking["claimable"] and claim_seconds:
                    booking["claim_expires_at"] = datetime.fromtimestamp(claim_seconds, tz=timezone.utc).isoformat()
                elif not booking["claimable"]:
                    queue_head = raw["booking_list"][0]
                    if raw.get("current_users") or queue_head.get("is_notified"):
                        wait = _estimated_wait(raw, caller, observed)
                        if wait:
                            # Match the wait estimate while publishing an absolute,
                            # stable time so the stream only changes when the queue moves.
                            ready = math.floor(observed.timestamp()) + wait
                            booking["estimated_ready_at"] = datetime.fromtimestamp(ready, tz=timezone.utc).isoformat()
            rows.append((row, raw))
    return rows


def _cursor_hash(filters):
    return hashlib.sha256(json.dumps(filters, separators=(",", ":")).encode()).hexdigest()[:16]


def _catalog_rows(bot, grant, principal, refs, configured, hardware):
    try:
        overrides = normalize_label_settings(json.loads(bot.config_overrides or "{}"))
        aliases = overrides.get("NODE_LABELS") or {}
    except (AttributeError, TypeError, ValueError):
        aliases = {}
    if not isinstance(aliases, dict):
        aliases = {}
    for (node, slot), ref in refs.items():
        if slot == NODE:
            models = hardware.get(node)
        else:
            _, model = json.loads(configured[node, slot])
            models = [{"model": model, "count": 1}] if isinstance(model, str) and model else None
        yield {
            "resource_ref": ref,
            "grant_ref": grant.public_id,
            "source": bot.name,
            "platform": principal.platform,
            "node": node,
            "device": slot if slot != NODE else None,
            "unit": "card" if slot != NODE else "node",
            "label": aliases.get(node),
            "hardware": models,
        }


def _read_identities(db, bot_ids, *, node=None):
    # Return one SQLite row per bot. Decoding a batch per bot avoids materializing
    # each resource as a separate SQLAlchemy row on the cross-group read path.
    unit = func.json_array(
        ResourceIdentity.node, ResourceIdentity.slot, ResourceIdentity.fingerprint, ResourceIdentity.ref
    )
    query = (
        select(ResourceIdentity.bot_id, func.json_group_array(unit))
        .where(ResourceIdentity.bot_id.in_(bot_ids))
        .group_by(ResourceIdentity.bot_id)
    )
    if node is not None:
        query = query.where(ResourceIdentity.node == node)
    return {
        bot_id: {(name, slot): (fingerprint, ref) for name, slot, fingerprint, ref in json.loads(units)}
        for bot_id, units in db.execute(query)
    }


def _resource_refs(db, load_principal, principal, grant, bot, known, configured):
    refs = {
        unit: entry[1]
        for unit, fingerprint in configured.items()
        if (entry := known.get(unit)) is not None and entry[0] == fingerprint
    }
    if len(refs) == len(configured):
        return refs

    # Existing installations may still need persistent identifiers on their first visit.
    # Only that path takes the same authority guard as commands and commits a write.
    with authority_guard(bot.id, platform=principal.platform, external_user_id=principal.external_user_id):
        db.rollback()
        current = load_principal(db)
        if current.generation != principal.generation:
            raise api_error("credential_invalid", 401)
        _, _, current_bot = _current_group(db, current, principal.public_id, grant.public_id)
        if current_bot.cluster_configs != bot.cluster_configs or current_bot.bot_type != bot.bot_type:
            raise api_error("resource_unavailable", 404)
        refs = {key: row.ref for key, row in identities(db, current_bot).items() if key in configured}
        db.commit()
        return refs


def _group_version(grant, bot):
    return (grant.confirmed_at, bot.uuid, bot.identity_revision, bot.bot_type, bot.cluster_configs)


def _resource_state(state, node, slot):
    if node is None:
        return state
    value = state.get(node)
    if value is None:
        return {}
    if slot not in (None, NODE):
        value = tuple(card for card in value if card.get("dev_id") == slot)
    return {node: value}


def _filter_state(bot, state, aliases, caller, *, hardware, unit, model, available, mine, node, device=None):
    """Narrow raw state before building holder and queue projections."""
    cards = bot.bot_type in CARD_BOTS
    if unit != "any" and unit != ("card" if cards else "node"):
        return {}
    if model is None and available is None and not mine and node is None and device is None:
        return state

    def selected(raw):
        bookings = raw.get("booking_list") or ()
        idle = raw.get("status", "idle") == "idle" and not bookings
        if available is not None and idle != available:
            return False
        if not mine:
            return True
        return any(item.get("user_id") == caller for item in raw.get("current_users") or ()) or any(
            item.get("user_id") == caller for item in bookings
        )

    result = {}
    for name, raw in state.items():
        if node is not None and name != node and aliases.get(name) != node:
            continue
        if cards:
            matches = tuple(
                card
                for card in raw
                if (device is None or card.get("dev_id") == device)
                and (model is None or (card.get("dev_model") or "").strip().casefold() == model)
                and selected(card)
            )
            if matches:
                result[name] = matches
        elif selected(raw) and (
            model is None or any(entry["model"].strip().casefold() == model for entry in hardware.get(name, []))
        ):
            result[name] = raw
    return result


@router.get("/resources")
def list_resources(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    model: Annotated[str | None, Query(min_length=1, max_length=96)] = None,
    unit: Literal["any", "node", "card"] = "any",
    available: bool | None = None,
    mine: bool = False,
    view: Literal["list", "wait"] = "list",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    node: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    node_prefix: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    device: Annotated[int | None, Query(ge=0, description="Filter by card number.")] = None,
    grant_ref: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    resource_ref: Annotated[str | None, Query(pattern=r"^res_[0-9a-f]{32}$")] = None,
    parent_ref: Annotated[str | None, Query(pattern=r"^res_[0-9a-f]{32}$")] = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    fields: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    include_people: Annotated[bool, Query(description="Include display names of holders and queued users.")] = False,
    include_node_access: Annotated[
        bool, Query(description="Include registered login addresses for an exact node name or resource reference.")
    ] = False,
    include_node_attributes: Annotated[
        bool, Query(description="Include registered attributes for an exact node name or resource reference.")
    ] = False,
):
    """Search native resources within the groups the caller has confirmed."""
    response.headers["Cache-Control"] = "no-store"
    return resource_page(
        request,
        db,
        model=model,
        unit=unit,
        available=available,
        mine=mine,
        view=view,
        limit=limit,
        node=node,
        node_prefix=node_prefix,
        device=device,
        grant_ref=grant_ref,
        resource_ref=resource_ref,
        parent_ref=parent_ref,
        cursor=cursor,
        fields=fields,
        include_people=include_people,
        include_node_access=include_node_access,
        include_node_attributes=include_node_attributes,
    )


def resource_page(request, db, **filters):
    return query_resource_page(db, lambda session: require_principal(request, session), **filters)


def query_resource_page(
    db,
    load_principal,
    *,
    model=None,
    unit="any",
    available=None,
    mine=False,
    view="list",
    limit=20,
    node=None,
    node_prefix=None,
    device=None,
    grant_ref=None,
    resource_ref=None,
    parent_ref=None,
    cursor=None,
    fields=None,
    include_people=False,
    include_node_access=False,
    include_node_attributes=False,
    charge_budget=True,
    allow_empty_match=False,
):
    principal = load_principal(db)
    principal_id = principal.public_id
    if charge_budget:
        check_budget(principal_id, "read")
    selected = None
    if fields is not None:
        selected = {field.strip() for field in fields.split(",")} | {"resource_ref"}
        if not selected <= _ROW_FIELDS:
            raise api_error("invalid_fields", 422)
        include_people = include_people or bool(selected & {"holders", "queue"})
        include_node_access = include_node_access or "node_access" in selected
        include_node_attributes = include_node_attributes or "node_attributes" in selected
    if include_node_access and not (node or resource_ref):
        raise api_error("node_access_requires_target", 422)
    if include_node_attributes and not (node or resource_ref):
        raise api_error("node_attributes_require_target", 422)
    catalog_only = (
        selected is not None and selected <= _CATALOG_FIELDS and available is None and not mine and view == "list"
    )
    if model is not None:
        model = model.strip().casefold()
        if not model:
            raise api_error("invalid_model", 422)
    bot_id = target_node = None
    target_slot = device
    if resource_ref or parent_ref:
        exact = db.get(ResourceIdentity, resource_ref or parent_ref)
        if exact is None:
            raise api_error("resource_unavailable", 404)
        bot_id = exact.bot_id
        target_node = exact.node
        if resource_ref:
            if device is not None and device != exact.slot:
                raise api_error("resource_unavailable", 404)
            target_slot = exact.slot
        if parent_ref:
            node = exact.node
    needs_hardware = selected is None or "hardware" in selected or model is not None
    groups = active_resource_grants(db, principal, grant_ref=grant_ref, bot_id=bot_id, inventory=needs_hardware)
    if not groups and (resource_ref or parent_ref):
        raise api_error("resource_unavailable", 404)
    if resource_ref:
        known = {bot_id: {(target_node, target_slot): (exact.fingerprint, exact.ref)}}
    else:
        known = _read_identities(db, [bot.id for _, bot in groups], node=target_node) if groups else {}
    # Capture the authorization and configuration we read, without holding a SQL connection
    # during state capture and projection. Revalidate this view before returning it.
    db.expunge_all()
    db.rollback()
    rows, unavailable = [], []
    people_data = {}
    for grant, bot in groups:
        if device is not None and bot.bot_type not in CARD_BOTS:
            continue
        try:
            hardware = resource_node_hardware(bot) if needs_hardware else {}
            if catalog_only:
                configured = config_units(bot, node=target_node, slot=target_slot)
                if node_prefix is not None:
                    configured = {
                        unit_key: value for unit_key, value in configured.items() if unit_key[0].startswith(node_prefix)
                    }
                refs = _resource_refs(db, load_principal, principal, grant, bot, known.get(bot.id, {}), configured)
                rows.extend(
                    row
                    for row in _catalog_rows(bot, grant, principal, refs, configured, hardware)
                    if _matches(row, unit, model, None, False, node, resource_ref)
                )
                continue
            with bot_manager.admit(bot.id, for_read=True, separate_read_capacity=True) as lease:
                if (lease.health or bot.status) != "running":
                    unavailable.append(grant.public_id)
                    continue
                snapshot = lease.bot.published_snapshot()
                observed = datetime.now(timezone.utc)
                state = _resource_state(snapshot.state, target_node, target_slot)
                commands = lease.bot.supported_commands()
                if not lease.bot.config.get_val("ALLOW_USER_LABEL"):
                    commands = [command for command in commands if command != "label"]
                config_aliases = lease.bot.config.get_val("NODE_LABELS")
                aliases = dict(config_aliases) if isinstance(config_aliases, dict) else {}
                feishu_client = lease.bot.config.get_val("_FEISHU_CLIENT") if include_people else None
            if node_prefix is not None:
                state = {name: raw for name, raw in state.items() if name.startswith(node_prefix)}
            state = _filter_state(
                bot,
                state,
                aliases,
                principal.external_user_id,
                hardware=hardware,
                unit=unit,
                model=model,
                available=available,
                mine=mine,
                node=node,
                device=device,
            )
            if not state:
                continue
            configured = config_units(bot, node=target_node, slot=target_slot)
            refs = _resource_refs(db, load_principal, principal, grant, bot, known.get(bot.id, {}), configured)
            for row, raw in project_rows(
                bot, grant, principal, commands, aliases, refs, observed, state, hardware=hardware
            ):
                if not _matches(row, unit, model, available, mine, node, resource_ref):
                    continue
                if view == "wait":
                    wait = _estimated_wait(raw, principal.external_user_id, observed)
                    if wait is None:
                        continue
                    row["estimated_seconds"] = wait
                rows.append(row)
                if include_people:
                    people_data[row["resource_ref"]] = (raw, feishu_client, observed)
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            unavailable.append(grant.public_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise

    if groups:
        db.rollback()
        current = load_principal(db)
        if current.public_id != principal_id or current.generation != principal.generation:
            raise api_error("credential_invalid", 401)
        versions = active_resource_versions(db, current, grant_ref=grant_ref, bot_id=bot_id)
        valid = {
            grant.public_id for grant, bot in groups if versions.get(grant.public_id) == _group_version(grant, bot)
        }
        rows = [row for row in rows if row["grant_ref"] in valid]
        unavailable = [ref for ref in unavailable if ref in valid]
        if parent_ref and not valid:
            raise api_error("resource_unavailable", 404)
    if resource_ref and not rows and not unavailable and not allow_empty_match:
        raise api_error("resource_unavailable", 404)

    rows.sort(key=lambda row: (row.get("estimated_seconds", 0), row["node"], row["grant_ref"], row["resource_ref"]))
    filters = (principal_id, model, unit, available, mine, view, node, grant_ref, resource_ref, parent_ref)
    if node_prefix is not None:
        filters = (*filters, node_prefix)
    if device is not None:
        filters = (*filters, ("device", device))
    if include_people:
        filters = (*filters, True)
    if include_node_access:
        filters = (*filters, "node_access")
    if include_node_attributes:
        filters = (*filters, "node_attributes")
    query_hash = _cursor_hash(filters)
    if cursor:
        prefix, _, anchor = cursor.partition(".")
        if prefix != query_hash:
            raise api_error("invalid_cursor", 422)
        offset = next((i + 1 for i, row in enumerate(rows) if row["resource_ref"] == anchor), None)
        if offset is None:
            raise api_error("stale_cursor", 409)
        rows = rows[offset:]
    page, size = [], 0
    authorized, addresses, attributes = {}, {}, {}
    if (include_node_access or include_node_attributes) and rows:
        authorized = {grant.public_id: bot.id for grant, bot in groups if grant.public_id in valid}
        nodes_by_bot = {}
        for row in rows[:limit]:
            nodes_by_bot.setdefault(authorized[row["grant_ref"]], set()).add(row["node"])
        columns = [Bot.id]
        if include_node_access:
            columns.extend((Bot.node_access_config, Bot.cluster_configs))
        if include_node_attributes:
            columns.append(Bot.node_inventory_config)
        for record in db.execute(select(*columns).where(Bot.id.in_(nodes_by_bot))):
            if include_node_access:
                addresses[record.id] = saved_node_access(record)
            if include_node_attributes:
                attributes[record.id] = inventory_document(record, nodes_by_bot[record.id])["nodes"]
    max_page_bytes = _MAX_PEOPLE_PAGE_BYTES if include_people else _MAX_PAGE_BYTES
    for row in rows[:limit]:
        bot_id = authorized.get(row["grant_ref"])
        if login := addresses.get(bot_id, {}).get(row["node"]):
            row["node_access"] = node_login_address(login)
        sample = attributes.get(bot_id, {}).get(row["node"], {})
        if sample.get("attributes") or sample.get("devices"):
            row["node_attributes"] = {
                key: sample[key] for key in ("observed_at", "attributes", "devices") if key in sample
            }
        if row["resource_ref"] in people_data:
            raw, client, observed = people_data[row["resource_ref"]]
            row.update(_resource_people(raw, principal, client, observed))
        if selected is not None:
            row = {key: value for key, value in row.items() if key in selected}
        row_size = 1 + len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
        if size + row_size > max_page_bytes:
            if not page:
                raise api_error("resource_row_too_large", 422)
            break
        page.append(row)
        size += row_size
    next_cursor = f"{query_hash}.{page[-1]['resource_ref']}" if len(rows) > len(page) else None
    return {
        "rows": page,
        "next_cursor": next_cursor,
        "complete": not unavailable,
        "unavailable": unavailable[:10],
        "unavailable_count": len(unavailable),
    }


def _matches(row, unit, model, available, mine, node, resource_ref):
    return (
        (unit == "any" or row["unit"] == unit)
        and (available is None or row["available"] == available)
        and (node is None or row["node"] == node or row["label"] == node)
        and (resource_ref is None or row["resource_ref"] == resource_ref)
        and (not mine or "holding" in row or "booking" in row)
        and (model is None or any(entry["model"].strip().casefold() == model for entry in row["hardware"] or []))
    )
