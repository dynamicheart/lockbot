"""Bot-scoped operational snapshots and queue lock releases using OP credentials."""

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.service import _anon_operator, write_audit_log
from lockbot.backend.app.bots.manager import (
    AdmissionFull,
    BotNotRunning,
    InstanceRetiring,
    MayHaveExecuted,
    bot_manager,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.privacy import visible_declarations
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_api.snapshot import build_resources, scrub_private
from lockbot.backend.app.resource_api.usage import active_declarations, attach_declarations, declarations_for_state
from lockbot.core.command_context import operation_attempt
from lockbot.core.platforms.registry import normalize_platform_name

router = APIRouter(prefix="/api/bots", tags=["Public API"])


def _require_operator(db, request, bot_id, *, fresh=False):
    if fresh:
        db.rollback()
    bot = db.get(Bot, bot_id)
    if bot is None or bot.is_deleted:
        raise HTTPException(404, "Bot not found")
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not bot.api_key or not token or not secrets.compare_digest(bot.api_key.encode(), token.encode()):
        raise HTTPException(401, "Invalid OP API key")
    request.state.traffic_bot_id = bot.id
    request.state.traffic_operator = True
    return bot


@router.get("/{bot_id}/op/history")
@limiter.limit("30/minute")
def operator_history(
    bot_id: int,
    request: Request,
    response: Response,
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(25, ge=1, le=100),
    query: str = Query("", max_length=128),
    db: Session = Depends(get_db),
):
    """Public Agent API operation history, never detailed Agent task notes or a full usage ledger."""
    from lockbot.backend.app.resource_api.event_log import READ_BUDGET, read_history

    response.headers["Cache-Control"] = "no-store"
    try:
        _require_operator(db, request, bot_id, fresh=True)
        result = read_history(bot_id, limit=limit, cursor=cursor, query=query, private=False)
        # Rotation/revocation/deletion during a file read must take effect before disclosure.
        _require_operator(db, request, bot_id, fresh=True)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    return {
        "bot_id": bot_id,
        "history_schema_version": 1,
        "visibility": "public",
        **result,
        "coverage": {
            "source": "agent_api_operations",
            "complete_lifecycle": False,
            "complete_history": False,
            "read_budget_bytes": READ_BUDGET,
            "excludes": ["chat_commands", "op_actions", "automatic_expiry", "hardware_utilization"],
        },
    }


@router.get("/{bot_id}/op/lease-history")
@limiter.limit("30/minute")
def operator_lease_history(
    bot_id: int,
    request: Request,
    response: Response,
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.resource_api.event_log import READ_BUDGET, read_history
    from lockbot.backend.app.resource_api.lease_history import MAX_LINE_BYTES

    response.headers["Cache-Control"] = "no-store"
    try:
        _require_operator(db, request, bot_id, fresh=True)
        result = read_history(bot_id, limit=limit, cursor=cursor, stream="lifecycle")
        checkpoint = None
        try:
            with bot_manager.admit(bot_id, for_read=True) as lease, lease.bot._lock:
                journal = getattr(lease.bot.config, "_lifecycle_observer", None)
                if journal is not None:
                    checkpoint = journal.checkpoint(lease.bot.state.bot_state)
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            pass
        _require_operator(db, request, bot_id, fresh=True)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    return {
        "bot_id": bot_id,
        **result,
        "checkpoint": checkpoint,
        "coverage": {
            "source": "allocator_commits",
            "complete_history": False,
            "read_budget_bytes": READ_BUDGET,
            "max_record_bytes": MAX_LINE_BYTES,
            "units": ["node_seconds", "device_seconds"],
            "physical_utilization": False,
        },
    }


@router.get("/{bot_id}/op/lifecycle-audit")
@limiter.limit("30/minute")
def operator_lifecycle_audit(
    bot_id: int,
    request: Request,
    response: Response,
    cursor: str | None = Query(None, max_length=16384),
    limit: int = Query(25, ge=1, le=100),
    resource: str = Query("", max_length=160),
    start: float | None = Query(None, ge=0, le=253402300799),
    end: float | None = Query(None, ge=0, le=253402300799),
    db: Session = Depends(get_db),
):
    """Frozen public allocator evidence; ordinary OP credentials never reveal detailed notes."""
    from lockbot.backend.app.resource_api.lifecycle_feed import read_feed

    response.headers["Cache-Control"] = "no-store"
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both start and end for a lifecycle window")
    if start is not None and end is not None and (start > end or end - start > 7 * 86400):
        raise HTTPException(422, "Lifecycle window must be ordered and at most seven days")
    try:
        bot = _require_operator(db, request, bot_id, fresh=True)
        identity = (bot.uuid, bot.identity_revision)
        try:
            with bot_manager.admit(bot_id, for_read=True) as lease, lease.bot._lock:
                journal = getattr(lease.bot.config, "_lifecycle_observer", None)
                if journal is not None:
                    checkpoint = journal.checkpoint(lease.bot.state.bot_state)
                    checkpoint.pop("state", None)
                    with journal.lock:
                        result = read_feed(
                            bot,
                            cursor=cursor,
                            limit=limit,
                            resource=resource,
                            start=start,
                            end=end,
                            checkpoint=checkpoint,
                        )
                else:
                    result = read_feed(bot, cursor=cursor, limit=limit, resource=resource, start=start, end=end)
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            result = read_feed(bot, cursor=cursor, limit=limit, resource=resource, start=start, end=end)
        current = _require_operator(db, request, bot_id, fresh=True)
        if identity != (current.uuid, current.identity_revision):
            raise HTTPException(409, "Bot identity changed during lifecycle export; start a new export")
        return result
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise


@router.get("/{bot_id}/op/resources")
@limiter.limit("60/minute")
def operator_resources(
    bot_id: int,
    request: Request,
    usage_offset: int = Query(default=0, ge=0),
    usage_limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Fresh OP snapshot: operational state and public task categories, never detailed Agent task notes.

    Resource state is complete; task declarations are paginated independently. Follow
    usage_page.next_offset until null. A null embedded usage on a partial page does not
    prove there is no declaration. Pages reflect current state, not a frozen transaction.
    """
    _require_operator(db, request, bot_id)
    try:
        with bot_manager.admit(bot_id, for_read=True) as lease:
            bot = _require_operator(db, request, bot_id, fresh=True)
            captured_at, state = lease.bot.state_snapshot()
            rows = active_declarations(db, bot, datetime.utcnow(), limit=usage_limit + 1, offset=usage_offset)
            has_more = len(rows) > usage_limit
            declarations = visible_declarations(declarations_for_state(rows[:usage_limit], state, captured_at))
            resources = scrub_private(
                build_resources(
                    captured_at=captured_at,
                    state=state,
                    bot_type=bot.bot_type,
                    platform=normalize_platform_name(bot.platform),
                    caller_id="",
                )
            )

            def strip_caller(items):
                for resource in items:
                    for key in ("caller_queue_position", "caller_is_queue_head", "claimable_by_you", "claim_command"):
                        resource.pop(key, None)
                    strip_caller(resource.get("devices", []))

            strip_caller(resources)
            attach_declarations(resources, declarations)
            return {
                "bot_id": bot.id,
                "name": bot.name,
                "bot_type": bot.bot_type,
                "platform": normalize_platform_name(bot.platform),
                "health": lease.health or bot.status,
                "captured_at": captured_at.isoformat(),
                "age_seconds": 0,
                "resources": resources,
                "usage_declarations": declarations,
                "usage_page": {
                    "offset": usage_offset,
                    "limit": usage_limit,
                    "has_more": has_more,
                    "next_offset": usage_offset + usage_limit if has_more else None,
                },
                "usage_complete": usage_offset == 0 and not has_more,
                "supported_op_operations": ["opkick"]
                + (["kicklock"] if bot.bot_type in {"QUEUE", "QUEUE_DEVICE"} else []),
            }
    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise errors.admission_http_error(exc, bot_id) from None


class KicklockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_keys: list[str] = Field(
        min_length=1,
        max_length=100,
        description=(
            "Explicit QUEUE node IDs or QUEUE_DEVICE card resource IDs (node1:dev0); no all-resource default."
        ),
    )
    reason: str = Field(
        min_length=1, max_length=512, description="Reason shown to affected users and in the audit log."
    )

    @field_validator("node_keys")
    @classmethod
    def resource_ids(cls, values):
        if any(not v.strip() or len(v) > 128 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Use nonempty resource IDs without control characters")
        if len(set(values)) != len(values):
            raise ValueError("Resource IDs must be unique")
        return values

    @field_validator("reason")
    @classmethod
    def reason_text(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Use a nonempty, single-line reason")
        return value.strip()


class KicklockResponse(BaseModel):
    ok: bool
    freed: list[str]
    affected_users: list[str]


@router.post("/{bot_id}/kicklock", response_model=KicklockResponse)
@limiter.limit("30/minute")
def operator_kicklock(bot_id: int, request: Request, body: KicklockRequest, db: Session = Depends(get_db)):
    """Release all current holders of explicitly listed QUEUE resources, retaining bookings.

    Requires this bot's OP API Key. Validate all resource IDs before changing any state.
    Idle resources are a no-op. After a timeout, read OP resources before retrying: a new
    holder may have acquired a resource. This operation does not stop machine processes.
    """
    _require_operator(db, request, bot_id)
    try:
        with bot_manager.admit(bot_id) as lease:
            bot = _require_operator(db, request, bot_id, fresh=True)
            if bot.bot_type not in {"QUEUE", "QUEUE_DEVICE"}:
                raise errors.command_not_supported("kicklock", bot.bot_type)
            try:
                with operation_attempt(bot_id, "operator", "kicklock"):
                    result = lease.bot.do_kicklock(body.node_keys, reason=body.reason)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            except Exception:
                raise errors.may_have_executed(bot_id) from None
            finally:
                snapshot_cache.invalidate(bot_id)
            # Attribute this to the bot's service credential, not to an invented human.
            write_audit_log(
                db,
                _anon_operator(f"OP API · bot {bot.id}", "service_key"),
                "bot.kicklock",
                target_type="bot",
                target_id=bot.id,
                target_name=bot.name,
                detail={
                    "source": "service_key",
                    "node_keys": body.node_keys,
                    "reason": body.reason,
                    "freed": result["freed"],
                    "affected_users": result["affected_users"],
                },
                ip=request.client.host if request.client else None,
            )
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise errors.may_have_executed(bot_id) from None
            return result
    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise errors.admission_http_error(exc, bot_id) from None
