"""Inventory and holder-only SSH metadata for an exact resource reference."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.bots.node_access import _active_holding, saved_node_access
from lockbot.backend.app.bots.node_inventory import saved_node_observation
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.snapshot import build_resources
from lockbot.backend.app.resource_api.v2.binding import PersonalGrant, api_error, require_principal, router
from lockbot.backend.app.resource_api.v2.identity import NODE, ResourceIdentity, config_units
from lockbot.backend.app.resource_api.v2.resources import _resource_state, check_budget, fresh_grant
from lockbot.backend.app.resource_auth.authority import authority_guard


@router.get("/resources/{resource_ref}/details")
@limiter.limit("60/minute")
def resource_details(
    request: Request,
    response: Response,
    resource_ref: Annotated[str, Path(pattern=r"^res_[0-9a-f]{32}$")],
    db: Session = Depends(get_db),
):
    """Read registered host inventory and, for an active holding, its SSH address."""
    response.headers["Cache-Control"] = "no-store"
    principal = require_principal(request, db)
    principal_id, platform, user_id = principal.public_id, principal.platform, principal.external_user_id
    check_budget(principal_id, "read")
    target = db.get(ResourceIdentity, resource_ref)
    if target is None:
        raise api_error("resource_unavailable", 404)
    bot_id = target.bot_id
    grant_id = db.scalar(
        select(PersonalGrant.public_id).where(
            PersonalGrant.principal_id == principal_id, PersonalGrant.bot_id == bot_id
        )
    )
    if grant_id is None:
        raise api_error("resource_unavailable", 404)
    # Reject invalid grants before acquiring a lease and recheck again under the authority guard.
    fresh_grant(db, request, principal_id, grant_id)
    try:
        with (
            bot_manager.admit(bot_id, for_read=True, separate_read_capacity=True) as lease,
            authority_guard(bot_id, platform=platform, external_user_id=user_id),
        ):
            _, _, bot = fresh_grant(db, request, principal_id, grant_id)
            if (lease.health or bot.status) != "running":
                raise api_error("resource_unavailable", 404)
            target = db.get(ResourceIdentity, resource_ref)
            if target is None or target.bot_id != bot_id:
                raise api_error("resource_unavailable", 404)
            node, slot = target.node, target.slot
            if config_units(bot, node=node, slot=slot).get((node, slot)) != target.fingerprint:
                raise api_error("resource_unavailable", 404)
            now = datetime.now(timezone.utc)
            state = _resource_state(lease.bot.published_snapshot().state, node, slot)
            resources = build_resources(
                captured_at=now, state=state, bot_type=bot.bot_type, platform=platform, caller_id=user_id
            )
            if not resources:
                raise api_error("resource_unavailable", 404)
            resource = resources[0]
            units = [resource] if slot == NODE else resource.get("devices", [])
            result = {
                "resource_ref": resource_ref,
                "node": node,
                "source": bot.name,
                "card": None if slot == NODE else slot,
                "ssh_status": "requires_hold",
            }
            if slot == NODE and (observation := saved_node_observation(bot, node)):
                result["inventory"] = observation.model_dump(mode="json", exclude_none=True)
            if any(_active_holding(unit, user_id, now) for unit in units):
                address = saved_node_access(bot).get(node)
                result["ssh_status"] = "available" if address else "unregistered"
                if address:
                    result["ssh_address"] = {key: address[key] for key in ("host", "ip", "port") if key in address}
            if not bot_manager.lease_is_current(lease):
                raise api_error("resource_unavailable", 404)
            return result
    except (BotNotRunning, AdmissionFull, InstanceRetiring) as exc:
        raise errors.admission_http_error(exc, bot_id) from None
