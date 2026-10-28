"""Identify this implementation/deployment and resolve a personal key's bot scope."""

from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.traffic import credential_budget
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.dependencies import _bearer_token
from lockbot.backend.app.resource_auth.http_errors import ResourceAuthenticationError
from lockbot.backend.app.settings.models import SiteSetting

SERVICE_ID = "lockbot.resource-api"
API_VERSION = "1"
_INSTANCE_KEY = "resource_api_instance_id"
router = APIRouter(prefix="/api/agent", tags=["Public API"])


def ensure_instance_id(db: Session) -> str:
    """One persisted, non-secret identifier; old clients/databases need no new table."""
    row = db.get(SiteSetting, _INSTANCE_KEY)
    if row is None:
        row = SiteSetting(key=_INSTANCE_KEY, value=str(uuid4()))
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(SiteSetting, _INSTANCE_KEY)
    if row is None or not row.value:
        raise RuntimeError("Missing resource API instance identity")
    return row.value


def server_metadata(request: Request, db: Session) -> dict:
    return {
        "service_id": SERVICE_ID,
        "instance_id": ensure_instance_id(db),
        "server_version": request.app.version,
        "api_version": API_VERSION,
        "credential_scheme": "lbr_bot_scoped",
        "capabilities": ["credential_context", "namespaced_skill", "skill_version_headers"],
        "context_url": "/api/agent/context",
        "enrollment_url": "/api/resource-auth/bindings",
        "openapi_url": "/api/public-openapi.json",
    }


class ServerIdentity(BaseModel):
    service_id: Literal["lockbot.resource-api"]
    instance_id: str
    server_version: str
    api_version: Literal["1"]
    credential_scheme: Literal["lbr_bot_scoped"]
    capabilities: list[str]
    context_url: str
    enrollment_url: str
    openapi_url: str


class BoundBot(BaseModel):
    id: int
    uuid: str
    name: str
    type: str


class AgentContextResponse(BaseModel):
    server: ServerIdentity
    actor: dict[str, str]
    bot: BoundBot
    agent_access_enabled: bool
    credential_scope: Literal["one_user_one_bot"]
    links: dict[str, str]
    skill: dict[str, Any]


@router.get("/context", response_model=AgentContextResponse)
@limiter.limit("60/minute")
def agent_context(request: Request, response: Response, db: Session = Depends(get_db)):
    """Discover only the identity/bot bound to this key. Never scan or list other bots.

    A valid suspended key can inspect its own context but cannot access resources. No
    running bot instance is needed. Revocation/expiry/identity changes still reject it.
    """
    row = service.authenticate_for_any_bot(db, token=_bearer_token(request))
    if row is None:
        raise ResourceAuthenticationError("Invalid credential", "credential_invalid")
    allowed, retry = credential_budget.check(row.id, "read")
    if not allowed:
        raise errors.rate_limited(retry)
    bot = db.get(Bot, row.bound_bot_id)
    from lockbot.backend.app.resource_api.skill import skill_artifacts

    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    return {
        "server": server_metadata(request, db),
        "actor": {"platform": row.platform, "external_user_id": row.external_user_id},
        "bot": {"id": bot.id, "uuid": bot.uuid, "name": bot.name, "type": bot.bot_type},
        "agent_access_enabled": bot.resource_auth_enabled,
        "credential_scope": "one_user_one_bot",
        "links": {
            "resources": f"/api/bots/{bot.id}/resources",
            "commands": f"/api/bots/{bot.id}/commands",
            "usage_declarations": f"/api/bots/{bot.id}/usage-declarations",
            "skill_manifest": "/api/agent-skill",
        },
        "skill": skill_artifacts()[2],
    }
