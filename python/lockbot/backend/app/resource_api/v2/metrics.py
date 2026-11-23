"""Read-only telemetry grants for resources visible to a v2 identity."""

from typing import Annotated

from fastapi import Depends, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.datasource import agent_metrics_source
from lockbot.backend.app.bots.metrics_access import issue_metrics_access
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.v2.binding import PersonalGrant, api_error, require_principal, router
from lockbot.backend.app.resource_api.v2.identity import NODE, ResourceIdentity, config_units
from lockbot.backend.app.resource_api.v2.resources import check_budget, fresh_grant
from lockbot.backend.app.resource_auth.authority import authority_guard


@router.get("/resources/{resource_ref}/metrics")
@limiter.limit("60/minute")
def resource_metrics(
    request: Request,
    response: Response,
    resource_ref: Annotated[str, Path(pattern=r"^res_[0-9a-f]{32}$")],
    db: Session = Depends(get_db),
):
    """Get temporary read-only metrics access to an authorized node or card.

    Send a v2 personal Key with an active grant for the target bot.
    Use the returned metrics:read grant only with the specified data source.
    """
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
        principal, _, bot = fresh_grant(db, request, principal_id, grant_id)
        target = db.get(ResourceIdentity, resource_ref)
        if target is None or target.bot_id != bot_id:
            raise api_error("resource_unavailable", 404)
        node, slot = target.node, target.slot
        if config_units(bot, node=node, slot=slot).get((node, slot)) != target.fingerprint:
            raise api_error("resource_unavailable", 404)
        source = agent_metrics_source(bot)
        if not source:
            raise api_error("metrics_not_configured", 404)
        source_node = source["nodeMapping"].get(node)
        source_device = None if slot == NODE else source["deviceMapping"].get(f"{node}:dev{slot}")
        if source_node is None or (slot != NODE and source_device is None):
            raise api_error("metrics_not_configured", 404)
        access = issue_metrics_access(
            db,
            bot,
            source,
            expires_at=principal.credential_expires_at,
            holdings={node: None if slot == NODE else {str(slot)}},
        )
        if access is None:
            raise api_error("metrics_access_unavailable", 503)
        return {
            "resource_ref": resource_ref,
            "node": node,
            "card": None if slot == NODE else slot,
            "source": {
                "endpoint": source["endpoint"],
                "node": source_node,
                "device": source_device,
                "stale_seconds": source["staleSeconds"],
            },
            "access": access,
        }
