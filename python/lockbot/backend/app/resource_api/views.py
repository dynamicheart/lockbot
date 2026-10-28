"""Narrow read-only snapshots for the per-bot browser page; no account or operation access."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.datasource import public_datasource
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
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_api.snapshot import build_resources, scrub_private
from lockbot.backend.app.resource_api.usage import active_declarations, attach_declarations, declarations_for_state
from lockbot.backend.app.resource_api.view_links import compact_view_path, verify_view
from lockbot.core.platforms.registry import normalize_platform_name

router = APIRouter(prefix="/api/bot-views", tags=["Read-only resource view"])
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization", "Referrer-Policy": "no-referrer"}


def _authorized(db, bot_id, request, *, fresh=False):
    if fresh:
        db.rollback()
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    bot = db.get(Bot, bot_id)
    window = verify_view(db, bot, token)
    if window is None:
        raise HTTPException(
            403,
            {"code": "view_link_invalid", "message": "Get a new link from your group or Agent."},
            headers=PRIVATE_HEADERS,
        )
    return bot, window


def _strip_caller_fields(resources):
    for resource in resources:
        for key in ("caller_queue_position", "caller_is_queue_head", "claimable_by_you", "claim_command"):
            resource.pop(key, None)
        _strip_caller_fields(resource.get("devices", []))


def _display_details(resources, state, aliases):
    for resource in resources:
        raw = state.get(resource["id"], {})
        resource["alias"] = aliases.get(resource["id"], "")
        if isinstance(raw, list):
            by_id = {entry.get("dev_id"): entry for entry in raw}
            pairs = [(device, by_id.get(device["id"], {})) for device in resource.get("devices", [])]
        else:
            pairs = [(resource, raw)]
        for target, entry in pairs:
            if "dev_model" in entry:
                target["dev_model"] = entry["dev_model"]
            waiting = max((holder["remaining_seconds"] for holder in target["holders"]), default=0)
            for booking, original in zip(target.get("booking_list", []), entry.get("booking_list", []), strict=True):
                duration = max(0, int(original.get("duration", 0)))
                booking["duration_seconds"] = duration
                booking["estimated_wait_seconds"] = waiting
                waiting += duration


@router.get("/{bot_id}/state")
@limiter.limit("300/minute")
def view_state(bot_id: int, request: Request, response: Response, db: Session = Depends(get_db)):
    bot, window = _authorized(db, bot_id, request)
    response.headers.update(PRIVATE_HEADERS)
    from lockbot.backend.app.resource_api.view_links import _iso, utcnow

    metadata = {
        "bot_id": bot.id,
        "name": bot.name,
        "bot_type": bot.bot_type,
        "server_time": _iso(utcnow()),
        "view_expires_at": _iso(window.expires_at),
        "view_path": compact_view_path(db, window),
    }
    try:
        with bot_manager.admit(bot_id, for_read=True) as lease:
            bot, window = _authorized(db, bot_id, request, fresh=True)
            captured_at, state, from_cache = snapshot_cache.get_or_compute(
                bot_id, lease.generation, datetime.now(timezone.utc), lease.bot.state_snapshot
            )
            declarations = []
            offset = 0
            while True:
                rows = active_declarations(db, bot, datetime.utcnow(), limit=500, offset=offset)
                if rows and from_cache:
                    captured_at, state = lease.bot.state_snapshot()
                    from_cache = False
                declarations.extend(declarations_for_state(rows, state, captured_at))
                if len(rows) < 500:
                    break
                offset += 500
            resources = scrub_private(
                build_resources(
                    captured_at=captured_at,
                    state=state,
                    bot_type=bot.bot_type,
                    platform=normalize_platform_name(bot.platform),
                    caller_id="",
                )
            )
            _strip_caller_fields(resources)
            # The shared page shows task context, never enrollment identifiers or credential notes.
            for declaration in declarations:
                declaration.pop("binding_id", None)
                declaration.pop("authorization_label", None)
            attach_declarations(resources, declarations)
            _display_details(resources, state, lease.bot.config.get_val("NODE_ALIASES", {}))
            # Re-check expiry after assembling the payload; cached state is never cached authorization.
            bot, window = _authorized(db, bot_id, request, fresh=True)
            from lockbot.core.resource_policy import public_policy

            return {
                "utilization_config": public_datasource(bot),
                "resource_policy": public_policy(state, lease.bot.config, now=captured_at.timestamp()),
                **metadata,
                "name": bot.name,
                "health": lease.health or bot.status,
                "server_time": _iso(utcnow()),
                "view_expires_at": _iso(window.expires_at),
                "view_path": compact_view_path(db, window),
                "captured_at": captured_at.isoformat(),
                "age_seconds": max(0, (datetime.now(timezone.utc) - captured_at).total_seconds()),
                "resources": resources,
            }
    except BotNotRunning:
        bot, window = _authorized(db, bot_id, request, fresh=True)
        return {**metadata, "health": bot.status, "captured_at": None, "resources": [], "age_seconds": None}
    except (AdmissionFull, InstanceRetiring, MayHaveExecuted) as exc:
        raise errors.admission_http_error(exc, bot_id) from None
