"""Identify this implementation/deployment and resolve a personal key's bot scope."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.rules import BotRules, current_bot_rules
from lockbot.backend.app.resource_api.traffic import credential_budget
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.dependencies import _bearer_token
from lockbot.backend.app.resource_auth.http_errors import ResourceAuthenticationError
from lockbot.backend.app.resource_auth.models import ResourceBinding
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
        "capabilities": [
            "credential_context",
            "credential_recovery_health",
            "own_binding_directory",
            "own_binding_recovery",
            "namespaced_skill",
            "skill_version_headers",
            "resource_view_link",
        ],
        "context_url": "/api/agent/context",
        "bindings_url": "/api/agent/bindings",
        "binding_recovery_url": "/api/agent/bindings/recover",
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
    bindings_url: str
    binding_recovery_url: str
    enrollment_url: str
    openapi_url: str


class BoundBot(BaseModel):
    id: int
    uuid: str
    name: str
    type: str


class OwnBinding(BaseModel):
    binding_id: str
    bot: BoundBot
    current: bool
    agent_access_enabled: bool
    expires_at: datetime | None
    credential_recoverable: bool


class BindingDirectoryResponse(BaseModel):
    server: ServerIdentity
    actor: dict[str, str]
    bindings: list[OwnBinding]
    discovery_only: Literal[True] = True
    credential_retrieval_supported: Literal[True] = True


class RecoveredBinding(OwnBinding):
    credential: str | None


class BindingRecoveryResponse(BaseModel):
    server: ServerIdentity
    actor: dict[str, str]
    bindings: list[RecoveredBinding]


class AgentContextResponse(BaseModel):
    server: ServerIdentity
    actor: dict[str, str]
    bot: BoundBot
    bot_rules: BotRules | None = None
    agent_access_enabled: bool
    credential_scope: Literal["one_user_one_bot"]
    credential_recovery_health: credentials.RecoveryHealth
    utilization: dict[str, Any] | None = None
    usage_privacy: dict[str, Any]
    skill_update: dict[str, Any]
    links: dict[str, str]
    skill: dict[str, Any]
    view_url: str | None = None
    view_expires_at: str | None = None


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
    request.state.traffic_bot_id = row.bound_bot_id
    request.state.traffic_credential = {
        "id": row.public_id,
        "kind": "agent",
        "user": row.external_user_id,
        "platform": row.platform,
    }
    allowed, retry = credential_budget.check(row.id, "read")
    if not allowed:
        raise errors.rate_limited(retry)
    from lockbot.backend.app.resource_auth.authority import authority_guard

    bot_id, platform, user_id = row.bound_bot_id, row.platform, row.external_user_id
    with authority_guard(bot_id, platform=platform, external_user_id=user_id):
        db.rollback()
        row = service.authenticate_for_any_bot(db, token=_bearer_token(request))
        if row is None or row.bound_bot_id != bot_id:
            raise ResourceAuthenticationError("Invalid credential", "credential_invalid")
        return _context_payload(request, response, db, row)


def _context_payload(request, response, db, row):
    bot = db.get(Bot, row.bound_bot_id)
    # Refresh even if another request retained this ORM identity in a reused Session.
    db.refresh(bot)
    from lockbot.backend.app.bots.datasource import public_datasource
    from lockbot.backend.app.bots.metrics_access import issue_metrics_access
    from lockbot.backend.app.resource_api.skill import client_skill_notice, skill_artifacts
    from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES
    from lockbot.backend.app.resource_api.view_links import view_link

    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    view = view_link(db, bot, str(request.base_url)) if bot.resource_auth_enabled else {}
    return {
        "server": server_metadata(request, db),
        "actor": {"platform": row.platform, "external_user_id": row.external_user_id},
        "bot": {"id": bot.id, "uuid": bot.uuid, "name": bot.name, "type": bot.bot_type},
        "bot_rules": current_bot_rules(bot, row.external_user_id),
        "agent_access_enabled": bot.resource_auth_enabled,
        "credential_scope": "one_user_one_bot",
        "credential_recovery_health": credentials.recovery_health(row),
        "utilization": {
            "best_effort": True,
            "configuration": public_datasource(bot),
            "access": issue_metrics_access(db, bot, expires_at=row.credential_expires_at)
            if bot.resource_auth_enabled
            else None,
            "guide_url": "https://github.com/dynamicheart/lockbot/blob/main/docs/usage/browser-utilization.md",
        }
        if public_datasource(bot)
        else None,
        "usage_privacy": {
            "conditional_cleanup": {
                "header": "If-Match",
                "revision_field": "revision",
                "mismatch_status": 412,
                "lease_required": True,
            },
            "private_fields": ["purpose", "agent_label", "authorization_label", "binding_id", "revision"],
            "policy_version": 2,
            "private_readers": ["same_verified_actor", "super_administrator"],
            "management_private_capability": "task_private:read",
            "management_private_requires_opt_in": True,
            "ordinary_operator_private_access": False,
            "delegated_private_access_supported": False,
            "public_task_categories": TASK_CATEGORIES,
            "legacy_purpose_visibility": "private",
        },
        "links": {
            "bindings": "/api/agent/bindings",
            "binding_recovery": "/api/agent/bindings/recover",
            "resources": f"/api/bots/{bot.id}/resources",
            "commands": f"/api/bots/{bot.id}/commands",
            "usage_declarations": f"/api/bots/{bot.id}/usage-declarations",
            "skill_manifest": "/api/agent-skill",
        },
        "skill": skill_artifacts()[2],
        "skill_update": client_skill_notice(request),
        **view,
    }


@router.get("/bindings", response_model=BindingDirectoryResponse)
@limiter.limit("60/minute")
def own_bindings(request: Request, response: Response, db: Session = Depends(get_db)):
    """List live bindings for this verified principal on this instance, without keys.

    Use the recovery endpoint to obtain the matching per-bot credentials. Resource
    operations continue to require each bot's own credential.
    """
    return _own_bindings(request, response, db, recover=False)


@router.post("/bindings/recover", response_model=BindingRecoveryResponse)
@limiter.limit("60/minute")
def recover_own_bindings(request: Request, response: Response, db: Session = Depends(get_db)):
    """Recover this principal's live per-bot keys and backfill the supplied legacy key.

    No user/bot selector is accepted. The identity comes from the presented key.
    Missing historical copies remain null; recovery never rotates or rebinds a key.
    """
    return _own_bindings(request, response, db, recover=True)


def _binding_namespace(db, bot_id, platform):
    # Feishu Open IDs are application scoped. Missing routing evidence must not
    # merge two users merely because their platform ID strings are equal.
    if platform == "infoflow":
        return ("infoflow",)
    if platform == "feishu":
        from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute

        route = (
            db.query(FeishuApplication)
            .join(FeishuGroupRoute, FeishuGroupRoute.application_id == FeishuApplication.id)
            .filter(FeishuGroupRoute.bot_id == bot_id)
            .one_or_none()
        )
        if route is not None and route.app_id and route.tenant_key:
            return ("feishu", route.app_id, route.tenant_key)
    return ("bot", bot_id)


def _backfill_presented_key(db, row, token):
    if credentials.recover_credential(row) is not None:
        return
    previous = row.credential_encrypted
    try:
        sealed = credentials.seal_credential(row.public_id, token)
    except Exception:
        # Authentication remains digest-based. Never rotate or erase the caller's
        # valid key to compensate for an unavailable encryption service/key.
        raise HTTPException(status_code=503, detail={"code": "credential_recovery_unavailable"}) from None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # A competing request may have backfilled, revoked, or replaced this binding
    # while encryption ran. Change only the recovery column of the exact live
    # snapshot; the final fresh authentication below still fences the response.
    db.query(ResourceBinding).filter(
        ResourceBinding.id == row.id,
        ResourceBinding.public_id == row.public_id,
        ResourceBinding.credential_digest == row.credential_digest,
        ResourceBinding.credential_encrypted == previous,
        ResourceBinding.bound_bot_id == row.bound_bot_id,
        ResourceBinding.bound_bot_uuid == row.bound_bot_uuid,
        ResourceBinding.bound_identity_revision == row.bound_identity_revision,
        ResourceBinding.platform == row.platform,
        ResourceBinding.external_user_id == row.external_user_id,
        ResourceBinding.revoked_at.is_(None),
        (ResourceBinding.credential_expires_at.is_(None) | (ResourceBinding.credential_expires_at > now)),
        db.query(Bot.id)
        .filter(
            Bot.id == ResourceBinding.bound_bot_id,
            Bot.uuid == ResourceBinding.bound_bot_uuid,
            Bot.identity_revision == ResourceBinding.bound_identity_revision,
            Bot.is_deleted.is_(False),
        )
        .exists(),
    ).update({ResourceBinding.credential_encrypted: sealed}, synchronize_session=False)
    db.expire_all()


def _own_bindings(request, response, db, *, recover):
    token = _bearer_token(request)
    row = service.authenticate_for_any_bot(db, token=token)
    if row is None:
        raise ResourceAuthenticationError("Invalid credential", "credential_invalid")
    request.state.traffic_bot_id = row.bound_bot_id
    request.state.traffic_credential = {
        "id": row.public_id,
        "kind": "agent",
        "user": row.external_user_id,
        "platform": row.platform,
    }
    allowed, retry = credential_budget.check(row.id, "read")
    if not allowed:
        raise errors.rate_limited(retry)
    from lockbot.backend.app.resource_auth.authority import authority_guard

    bot_id, platform, subject = row.bound_bot_id, row.platform, row.external_user_id
    with authority_guard(bot_id, platform=platform, external_user_id=subject):
        db.rollback()
        row = service.authenticate_for_any_bot(db, token=token)
        if row is None or (row.bound_bot_id, row.platform, row.external_user_id) != (bot_id, platform, subject):
            raise ResourceAuthenticationError("Invalid credential", "credential_invalid")
        if recover:
            _backfill_presented_key(db, row, token)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        candidates = (
            db.query(ResourceBinding, Bot)
            .join(Bot, Bot.id == ResourceBinding.bound_bot_id)
            .filter(
                ResourceBinding.platform == platform,
                ResourceBinding.external_user_id == subject,
                ResourceBinding.revoked_at.is_(None),
                Bot.is_deleted.is_(False),
                ResourceBinding.bound_bot_uuid == Bot.uuid,
                ResourceBinding.bound_identity_revision == Bot.identity_revision,
            )
            .order_by(Bot.id, ResourceBinding.id)
            .all()
        )
        namespace = _binding_namespace(db, bot_id, platform)
        source_id = row.id
        selected = {}
        bindings = []
        for binding, bot in candidates:
            if _binding_namespace(db, bot.id, platform) != namespace:
                continue
            if binding.credential_expires_at is not None and binding.credential_expires_at <= now:
                continue
            key = credentials.recover_credential(binding)
            selected[binding.id] = (
                binding.bound_bot_id,
                binding.bound_bot_uuid,
                binding.bound_identity_revision,
                binding.platform,
                binding.external_user_id,
                binding.public_id,
                binding.credential_digest,
            )
            item = dict(
                binding_id=binding.public_id,
                bot=BoundBot(id=bot.id, uuid=bot.uuid, name=bot.name, type=bot.bot_type),
                current=binding.id == row.id,
                agent_access_enabled=bot.resource_auth_enabled,
                expires_at=binding.credential_expires_at.replace(tzinfo=timezone.utc)
                if binding.credential_expires_at
                else None,
                credential_recoverable=key is not None,
            )
            bindings.append(RecoveredBinding(**item, credential=key) if recover else OwnBinding(**item))
        # Finish an idempotent legacy-copy write, then establish a fresh read
        # transaction. Do not release decrypted keys from a stale ORM snapshot
        # when another writer revoked a binding or changed its IM route meanwhile.
        if recover:
            db.commit()
        db.rollback()
        db.expire_all()
        current = service.authenticate_for_any_bot(db, token=token)
        if (
            current is None
            or current.id != source_id
            or (current.bound_bot_id, current.platform, current.external_user_id) != (bot_id, platform, subject)
            or _binding_namespace(db, bot_id, platform) != namespace
        ):
            raise ResourceAuthenticationError("Binding changed", "credential_invalid")
        fresh = {
            binding.id: (binding, bot)
            for binding, bot in db.query(ResourceBinding, Bot)
            .join(Bot, Bot.id == ResourceBinding.bound_bot_id)
            .filter(ResourceBinding.id.in_(selected))
            .all()
        }
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for identifier, expected in selected.items():
            pair = fresh.get(identifier)
            if pair is None:
                raise ResourceAuthenticationError("Binding changed", "credential_invalid")
            binding, bot = pair
            actual = (
                binding.bound_bot_id,
                binding.bound_bot_uuid,
                binding.bound_identity_revision,
                binding.platform,
                binding.external_user_id,
                binding.public_id,
                binding.credential_digest,
            )
            if (
                actual != expected
                or binding.revoked_at is not None
                or bot.is_deleted
                or binding.bound_bot_uuid != bot.uuid
                or binding.bound_identity_revision != bot.identity_revision
                or (binding.credential_expires_at is not None and binding.credential_expires_at <= now)
                or _binding_namespace(db, bot.id, platform) != namespace
            ):
                raise ResourceAuthenticationError("Binding changed", "credential_invalid")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Authorization"
        payload = dict(
            server=ServerIdentity(**server_metadata(request, db)),
            actor={"platform": platform, "external_user_id": subject},
            bindings=bindings,
        )
        if recover:
            return BindingRecoveryResponse(**payload)
        return BindingDirectoryResponse(**payload)
