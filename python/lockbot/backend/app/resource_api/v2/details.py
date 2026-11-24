"""Authorized inventory and SSH metadata for an exact resource reference."""

from typing import Annotated

from fastapi import Depends, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.node_access import resource_ssh_address
from lockbot.backend.app.bots.node_inventory import saved_node_observation
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.v2.binding import PersonalGrant, api_error, require_principal, router
from lockbot.backend.app.resource_api.v2.identity import NODE, ResourceIdentity, config_units
from lockbot.backend.app.resource_api.v2.resources import check_budget, fresh_grant
from lockbot.backend.app.resource_auth.authority import authority_guard


@router.get("/resources/{resource_ref}/details")
@limiter.limit("60/minute")
def resource_details(
    request: Request,
    response: Response,
    resource_ref: Annotated[str, Path(pattern=r"^res_[0-9a-f]{32}$")],
    db: Session = Depends(get_db),
):
    """Read registered host inventory and SSH metadata within the caller's bot grant."""
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
    with authority_guard(bot_id, platform=platform, external_user_id=user_id):
        _, _, bot = fresh_grant(db, request, principal_id, grant_id)
        target = db.get(ResourceIdentity, resource_ref)
        if target is None or target.bot_id != bot_id:
            raise api_error("resource_unavailable", 404)
        node, slot = target.node, target.slot
        if config_units(bot, node=node, slot=slot).get((node, slot)) != target.fingerprint:
            raise api_error("resource_unavailable", 404)
        address = resource_ssh_address(bot, node)
        result = {
            "resource_ref": resource_ref,
            "node": node,
            "source": bot.name,
            "card": None if slot == NODE else slot,
            "ssh_status": "available" if address else "unregistered",
        }
        if slot == NODE and (observation := saved_node_observation(bot, node)):
            result["inventory"] = observation.model_dump(mode="json", exclude_none=True)
        if address:
            result["ssh_address"] = address
        return result
