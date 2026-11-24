"""Chat-confirmed personal credentials and per-bot grants for Agent API v2."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Literal, NamedTuple

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, or_, select
from sqlalchemy.orm import Mapped, Session, load_only, mapped_column

from lockbot.backend.app import config
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.v2.skill import SKILL_PATH, SkillZip, download_skill, skill_release
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.resource_auth.pending import PendingCapExceeded, PersonalBindingReceipt, pending_store
from lockbot.core.platforms.registry import normalize_platform_name

router = APIRouter(prefix="/api/agent/v2", tags=["Agent API v2"])
KEY_PREFIX = "lbv2_"


class PersonalCredential(Base):
    __tablename__ = "agent_v2_credentials"
    __table_args__ = (UniqueConstraint("namespace", "external_user_id"),)

    public_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(256))
    platform: Mapped[str] = mapped_column(String(32))
    external_user_id: Mapped[str] = mapped_column(String(256))
    credential_digest: Mapped[str] = mapped_column(String(64), unique=True)
    credential_encrypted: Mapped[str] = mapped_column(String)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    generation: Mapped[int] = mapped_column(default=0)


class PersonalGrant(Base):
    __tablename__ = "agent_v2_grants"
    __table_args__ = (UniqueConstraint("principal_id", "bot_id"),)

    public_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("agent_v2_credentials.public_id"), index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    bot_uuid: Mapped[str] = mapped_column(String(36))
    bot_revision: Mapped[int]
    chat_id: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime)


def api_error(code: str, status: int) -> HTTPException:
    return HTTPException(status, detail={"code": code}, headers={"Cache-Control": "no-store"})


def authenticate(db: Session, authorization: str) -> PersonalCredential | None:
    if not authorization.startswith("Bearer " + KEY_PREFIX) or len(authorization) > 100:
        return None
    token = authorization.removeprefix("Bearer ")
    row = db.scalar(
        select(PersonalCredential).where(PersonalCredential.credential_digest == credentials.digest_credential(token))
    )
    if row is None or is_expired(row):
        return None
    return row


def is_expired(principal: PersonalCredential) -> bool:
    return principal.credential_expires_at is not None and principal.credential_expires_at <= service._naive_utc(
        service._utcnow()
    )


def require_principal(request: Request, db: Session) -> PersonalCredential:
    principal = authenticate(db, request.headers.get("Authorization", ""))
    if principal is None:
        raise api_error("credential_invalid", 401)
    request.state.v2_activity_principal = principal.public_id
    return principal


def _platform_namespace(platform: str, app_id: str | None = None) -> str | None:
    if platform == "infoflow":
        return platform
    if platform == "feishu" and app_id is not None:
        return f"feishu:{app_id}"
    return None


def _namespace(db: Session, bot: Bot, platform: str) -> str | None:
    if normalize_platform_name(bot.platform) != platform:
        return None
    if platform == "feishu":
        route = db.scalar(select(FeishuGroupRoute).where(FeishuGroupRoute.bot_id == bot.id))
        if route is not None and route.chat_id == bot.group_id:
            app = db.get(FeishuApplication, route.application_id)
            if app is not None:
                return _platform_namespace(platform, app.app_id)
    return _platform_namespace(platform)


def _current_grant(principal: PersonalCredential, grant: PersonalGrant, bot: Bot | None) -> bool:
    return bool(
        bot
        and not bot.is_deleted
        and bot.resource_auth_enabled
        and grant.principal_id == principal.public_id
        and grant.bot_uuid == bot.uuid
        and grant.bot_revision == bot.identity_revision
        and (bot.binding_accept_after is None or grant.confirmed_at > bot.binding_accept_after)
    )


def grant_active(db: Session, principal: PersonalCredential, grant: PersonalGrant, bot: Bot | None) -> bool:
    return _current_grant(principal, grant, bot) and _namespace(db, bot, principal.platform) == principal.namespace


def _resource_grant_query(principal, columns, *, grant_ref, bot_id):
    query = (
        select(*columns, Bot.platform, FeishuApplication.app_id)
        .select_from(PersonalGrant)
        .join(Bot, Bot.id == PersonalGrant.bot_id)
        .outerjoin(FeishuGroupRoute, (FeishuGroupRoute.bot_id == Bot.id) & (FeishuGroupRoute.chat_id == Bot.group_id))
        .outerjoin(FeishuApplication, FeishuApplication.id == FeishuGroupRoute.application_id)
        .where(
            PersonalGrant.principal_id == principal.public_id,
            Bot.is_deleted.is_(False),
            Bot.resource_auth_enabled.is_(True),
            PersonalGrant.bot_uuid == Bot.uuid,
            PersonalGrant.bot_revision == Bot.identity_revision,
            or_(Bot.binding_accept_after.is_(None), PersonalGrant.confirmed_at > Bot.binding_accept_after),
        )
    )
    if grant_ref is not None:
        query = query.where(PersonalGrant.public_id == grant_ref)
    if bot_id is not None:
        query = query.where(PersonalGrant.bot_id == bot_id)
    return query


def _matches_read_namespace(principal, platform_name, app_id):
    platform = normalize_platform_name(platform_name)
    return platform == principal.platform and _platform_namespace(platform, app_id) == principal.namespace


class ResourceReadGrant(NamedTuple):
    public_id: str
    confirmed_at: datetime


class ResourceReadBot(NamedTuple):
    id: int
    uuid: str | None
    identity_revision: int
    name: str
    bot_type: str
    status: str
    cluster_configs: str
    config_overrides: str | None
    node_hardware_config: str | None
    utilization_config: str | None


def _packed_grant_rows(db, query):
    """Decode one SQLite row instead of converting each column in the driver."""
    columns = query.subquery().c
    packed = func.json_group_array(func.json_array(*columns))
    return json.loads(db.scalar(select(packed)))


def active_resource_grants(db: Session, principal: PersonalCredential, *, grant_ref=None, bot_id=None):
    """Load the groups visible to a resource query, including Feishu routing, in one read."""
    if grant_ref is None and bot_id is None:
        columns = (
            PersonalGrant.public_id,
            PersonalGrant.confirmed_at,
            Bot.id,
            Bot.uuid,
            Bot.identity_revision,
            Bot.name,
            Bot.bot_type,
            Bot.status,
            Bot.cluster_configs,
            Bot.config_overrides,
            Bot.node_hardware_config,
            Bot.utilization_config,
        )
        query = _resource_grant_query(principal, columns, grant_ref=None, bot_id=None)
        return [
            (ResourceReadGrant(ref, datetime.fromisoformat(at)), ResourceReadBot(*bot))
            for ref, at, *bot, platform, app_id in _packed_grant_rows(db, query.order_by(PersonalGrant.public_id))
            if _matches_read_namespace(principal, platform, app_id)
        ]
    query = _resource_grant_query(principal, (PersonalGrant, Bot), grant_ref=grant_ref, bot_id=bot_id).options(
        load_only(PersonalGrant.public_id, PersonalGrant.confirmed_at, raiseload=True),
        load_only(
            Bot.id,
            Bot.uuid,
            Bot.identity_revision,
            Bot.name,
            Bot.bot_type,
            Bot.status,
            Bot.cluster_configs,
            Bot.config_overrides,
            Bot.node_hardware_config,
            Bot.utilization_config,
            raiseload=True,
        ),
    )
    return [
        (grant, bot)
        for grant, bot, platform, app_id in db.execute(query.order_by(PersonalGrant.public_id))
        if _matches_read_namespace(principal, platform, app_id)
    ]


def active_resource_versions(db: Session, principal: PersonalCredential, *, grant_ref=None, bot_id=None):
    """Recheck authorization without loading bot or grant objects a second time."""
    columns = (
        PersonalGrant.public_id,
        PersonalGrant.confirmed_at,
        Bot.uuid,
        Bot.identity_revision,
        Bot.bot_type,
        Bot.cluster_configs,
    )
    query = _resource_grant_query(principal, columns, grant_ref=grant_ref, bot_id=bot_id)
    if grant_ref is None and bot_id is None:
        # A broad read only needs the version columns. Return them to the driver
        # as one row to keep per-column conversion out of concurrent SQLite scans.
        rows = [
            (ref, datetime.fromisoformat(at), uuid, revision, kind, configs, platform, app_id)
            for ref, at, uuid, revision, kind, configs, platform, app_id in _packed_grant_rows(db, query)
        ]
    else:
        rows = db.execute(query)
    return {
        ref: (at, uuid, revision, kind, configs)
        for ref, at, uuid, revision, kind, configs, platform, app_id in rows
        if _matches_read_namespace(principal, platform, app_id)
    }


def _replace_key(principal: PersonalCredential, now: datetime) -> None:
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    principal.credential_digest = credentials.digest_credential(token)
    principal.credential_encrypted = credentials.seal_credential(principal.public_id, token)
    principal.credential_expires_at = service._credential_expiry(now)


def _grant(db: Session, principal: PersonalCredential, bot: Bot, *, chat_id, now: datetime) -> PersonalGrant:
    grant = db.scalar(
        select(PersonalGrant).where(PersonalGrant.principal_id == principal.public_id, PersonalGrant.bot_id == bot.id)
    )
    if grant is None:
        grant = PersonalGrant(
            public_id=credentials.generate_public_id(), principal_id=principal.public_id, bot_id=bot.id
        )
        db.add(grant)
    grant.bot_uuid = bot.uuid
    grant.bot_revision = bot.identity_revision
    grant.chat_id = chat_id
    grant.confirmed_at = service._naive_utc(now)
    return grant


def _migrate_legacy(db: Session, principal: PersonalCredential, now: datetime) -> None:
    """An explicit reset carries current grants over and revokes only keys for this identity."""
    bindings = db.execute(
        select(ResourceBinding, Bot)
        .join(Bot, Bot.id == ResourceBinding.bound_bot_id)
        .where(
            ResourceBinding.platform == principal.platform,
            ResourceBinding.external_user_id == principal.external_user_id,
            ResourceBinding.revoked_at.is_(None),
            Bot.is_deleted.is_(False),
        )
    )
    for row, bot in bindings:
        if not service._binding_is_current(row, bot, now):
            continue
        if _namespace(db, bot, principal.platform) != principal.namespace:
            continue
        _grant(db, principal, bot, chat_id=row.confirmed_chat_id, now=now)
        row.revoked_at = service._naive_utc(now)


def confirm(
    db: Session, bot: Bot, platform: str, external_user_id: str, record, chat_id
) -> PersonalBindingReceipt | None:
    """Called only in the verified chat confirmation under its authority guard."""
    namespace = _namespace(db, bot, platform)
    if not namespace or not bot.uuid or (bot.group_id and chat_id is None):
        return None
    principal = db.scalar(
        select(PersonalCredential).where(
            PersonalCredential.namespace == namespace, PersonalCredential.external_user_id == external_user_id
        )
    )
    if record.principal_id is not None and (
        principal is None
        or principal.public_id != record.principal_id
        or principal.generation != record.principal_generation
        or is_expired(principal)
    ):
        return None
    now = service._utcnow()
    reset = record.purpose == "personal_reset"
    if principal is not None and record.principal_id is None and record.purpose == "personal":
        return None
    if principal is None:
        principal = PersonalCredential(
            public_id=credentials.generate_public_id(),
            namespace=namespace,
            platform=platform,
            external_user_id=external_user_id,
            generation=0,
        )
        _replace_key(principal, now)
        db.add(principal)
        db.flush()
    elif reset or is_expired(principal):
        principal.generation += 1
        _replace_key(principal, now)
    if reset:
        _migrate_legacy(db, principal, now)
        db.flush()
    grant = _grant(db, principal, bot, chat_id=chat_id, now=now)
    return PersonalBindingReceipt(principal.public_id, grant.public_id, principal.generation)


class BindingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["bind", "recover", "reset"] = "bind"


class BindingProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proof: str = Field(min_length=1, max_length=128)


def grant_summary(grant: PersonalGrant, bot: Bot, principal: PersonalCredential) -> dict:
    return {"grant_ref": grant.public_id, "platform": principal.platform, "name": bot.name, "chat_id": grant.chat_id}


@router.post("/bindings", status_code=201)
@limiter.limit(config.RESOURCE_AUTH_V2_BIND_START_RATE)
def start_binding(
    request: Request,
    response: Response,
    body: BindingStart | None = Body(
        default=None,
        openapi_examples={
            "bind": {"summary": "Add a group", "value": {"purpose": "bind"}},
            "recover": {"summary": "Recover the existing personal Key", "value": {"purpose": "recover"}},
            "reset": {"summary": "Replace the personal Key", "value": {"purpose": "reset"}},
        },
    ),
    db: Session = Depends(get_db),
):
    """Issue a chat code and a private proof. Confirm in the target group, then call complete.

    Use `recover` without a Key when you have lost it. Confirm with the same chat identity.
    A Bearer key pins new grants to its identity. Use `reset` only to replace the existing Key.
    """
    response.headers["Cache-Control"] = "no-store"
    principal = require_principal(request, db) if request.headers.get("Authorization") else None
    purpose = {"bind": "personal", "recover": "personal_recover", "reset": "personal_reset"}[
        body.purpose if body else "bind"
    ]
    try:
        issued = service.create_binding(
            source=request.client.host if request.client else "",
            purpose=purpose,
            principal_id=principal.public_id if principal else None,
            principal_generation=principal.generation if principal else None,
        )
    except PendingCapExceeded:
        raise HTTPException(
            429,
            detail={"code": "too_many_pending_bindings"},
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "600",
            },
        ) from None
    return {
        "binding_id": issued["binding_id"],
        "proof": issued["credential"],
        "bind_code": issued["bind_code"],
        "expires_at": issued["bind_expires_at"],
    }


@router.post("/bindings/{binding_id}/complete")
@limiter.limit("60/minute")
def complete_binding(
    binding_id: str, request: Request, response: Response, body: BindingProof, db: Session = Depends(get_db)
):
    """Read a confirmed grant and its Key, when applicable, using the initiating client's proof."""
    response.headers["Cache-Control"] = "no-store"
    record = pending_store.get(binding_id)
    if (
        record is None
        or record.purpose not in {"personal", "personal_recover", "personal_reset"}
        or not credentials.credential_matches(record.credential_digest, body.proof)
    ):
        raise api_error("binding_unavailable", 404)
    receipt = record.receipt
    if not isinstance(receipt, PersonalBindingReceipt):
        return {"status": "pending"}
    principal = db.get(PersonalCredential, receipt.principal_id)
    grant = db.get(PersonalGrant, receipt.grant_id)
    if principal is None or grant is None:
        raise api_error("binding_unavailable", 404)
    with authority_guard(grant.bot_id, platform=principal.platform, external_user_id=principal.external_user_id):
        db.rollback()
        principal = db.get(PersonalCredential, receipt.principal_id)
        grant = db.get(PersonalGrant, receipt.grant_id)
        if principal is None or grant is None:
            raise api_error("binding_unavailable", 404)
        bot = db.get(Bot, grant.bot_id)
        if (
            is_expired(principal)
            or principal.generation != receipt.credential_generation
            or not grant_active(db, principal, grant, bot)
        ):
            raise api_error("binding_unavailable", 404)
        result = {"status": "confirmed", "grant": grant_summary(grant, bot, principal)}
        if record.principal_id is None or record.purpose == "personal_reset":
            token = credentials.recover_credential(principal)
            if token is None:
                raise api_error("credential_unavailable", 409)
            result["credential"] = token
        return result


@router.get("/bindings")
@limiter.limit("120/minute")
def list_bindings(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    principal = require_principal(request, db)
    rows = db.execute(
        select(PersonalGrant, Bot)
        .join(Bot, Bot.id == PersonalGrant.bot_id)
        .where(
            PersonalGrant.principal_id == principal.public_id,
        )
        .order_by(PersonalGrant.public_id)
    )
    return {
        "grants": [
            {**grant_summary(grant, bot, principal), "bot_id": bot.id}
            for grant, bot in rows
            if grant_active(db, principal, grant, bot)
        ]
    }


@router.delete("/bindings/{grant_ref}", status_code=204)
@limiter.limit("60/minute")
def remove_binding(grant_ref: str, request: Request, db: Session = Depends(get_db)):
    """Revoke this personal Key's authorization to the selected group."""
    principal = require_principal(request, db)
    grant = db.get(PersonalGrant, grant_ref)
    if grant is None or grant.principal_id != principal.public_id:
        raise api_error("binding_unavailable", 404)
    with authority_guard(grant.bot_id, platform=principal.platform, external_user_id=principal.external_user_id):
        db.rollback()
        principal = require_principal(request, db)
        grant = db.get(PersonalGrant, grant_ref)
        if grant is None or grant.principal_id != principal.public_id:
            raise api_error("binding_unavailable", 404)
        db.delete(grant)
        db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/manifest")
def manifest(db: Session = Depends(get_db)):
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id
    from lockbot.backend.app.resource_api.task_categories import TASK_CATEGORIES

    return {
        "service_id": "lockbot.resource-api",
        "api_version": "2",
        "credential_scheme": "lbv2_personal",
        "instance_id": ensure_instance_id(db),
        "openapi_url": "/api/agent/v2/openapi.json",
        "capabilities": ["resource_events"],
        "task_categories": TASK_CATEGORIES,
        "resource_events_url": "/api/agent/v2/resources/events",
        "skill": skill_release(),
    }


router.add_api_route(SKILL_PATH, download_skill, methods=["GET"], response_class=SkillZip)


@router.get("/openapi.json", include_in_schema=False)
def openapi_v2():
    from fastapi.openapi.utils import get_openapi

    from lockbot.backend.app.resource_api.rate_contract import describe_agent_rate
    from lockbot.backend.app.resource_api.v2.docs import describe_v2_operation

    schema = get_openapi(
        title="LockBot Agent API v2",
        version="2",
        description=(
            "可直接调用 HTTP。先发起绑定，请用户在目标群确认，再提交 proof。用返回的 lbv2_ Key 作为 "
            "Bearer 凭证查询资源，再用返回的 resource_ref 提交操作。每个接口标出独立和共享限流。\n\n"
            "示例中的 `LOCKBOT_URL` 是服务地址（末尾去掉斜杠），`KEY` 是绑定后取得的个人凭证。"
            "示例里的 ID 和 Key 仅用于展示格式。"
        ),
        routes=router.routes,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "PersonalKey": {"type": "http", "scheme": "bearer", "bearerFormat": KEY_PREFIX + "…"},
    }
    for path, methods in schema["paths"].items():
        for method, details in methods.items():
            describe_v2_operation(path, method, details)
            describe_agent_rate(path, method, details)
            if path.endswith(("/manifest", "/complete", SKILL_PATH)):
                continue
            details["security"] = [{"PersonalKey": []}]
            if path.endswith("/bindings") and method == "post":
                details["security"].append({})
    return schema
