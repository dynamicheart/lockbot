"""Revocable multi-bot configuration delegation with separate write scopes."""

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app import config
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.datasource import UtilizationConfig, public_datasource, serialized_datasource
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.node_access import (
    _unique_object,
    mapping_version,
    node_ids,
    parse_access_mapping,
    saved_node_access,
    serialize_node_access,
)
from lockbot.backend.app.bots.router import _create_bot_for_user, _owned_bot, _validate_bot_creation
from lockbot.backend.app.bots.schemas import BotCreate
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_auth.authority import authority_guard

Scope = Literal["node_access:write", "datasource:write"]
ProvisioningScope = Literal["bot:create"]
MAX_TTL = 7 * 86400
MAX_CONFIGURATION_BYTES = 256 * 1024
_TOKEN = re.compile(r"lbc1\.([A-Za-z0-9_-]{22})\.[A-Za-z0-9_-]{43}")
_PREVIEW = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")
_PROVISIONING_PREVIEW = re.compile(r"([0-9a-f-]{36})\.([0-9]{1,12})\.([0-9a-f]{64})")
_guard = threading.RLock()


class PrivateConfigurationRoute(APIRoute):
    """Preserve duplicate-key rejection and keep submitted values out of validation errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            if self.body_field is not None:
                raw = await request.body()
                if len(raw) > MAX_CONFIGURATION_BYTES + 4096:
                    _deny(413, "configuration_too_large")
                if raw:
                    try:
                        json.loads(raw, object_pairs_hook=_unique_object)
                    except (ValueError, UnicodeError, RecursionError):
                        _deny(422, "configuration_invalid")
            try:
                return await handler(request)
            except RequestValidationError:
                _deny(422, "configuration_invalid")

        return protected


router = APIRouter(tags=["Bot configuration delegation"], route_class=PrivateConfigurationRoute)


class ConfigurationCredential(Base):
    __tablename__ = "configuration_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(22), unique=True)
    label: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    issuer_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    issuer_token_version: Mapped[int] = mapped_column(Integer)
    scopes: Mapped[str] = mapped_column(Text)
    bots: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class Lifetime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expires_in_seconds: int = Field(default=86400, ge=60, le=MAX_TTL)


class CreateCredential(Lifetime):
    label: str = Field(min_length=1, max_length=64)
    bot_ids: list[StrictInt] = Field(max_length=100)
    scopes: list[Scope | ProvisioningScope] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def separate_creation_scope(self):
        if "bot:create" in self.scopes:
            if self.bot_ids or self.scopes != ["bot:create"]:
                raise ValueError("Bot creation requires a separate credential")
        elif not self.bot_ids:
            raise ValueError("Select at least one bot")
        return self

    @field_validator("label")
    @classmethod
    def public_label(cls, value):
        if value != value.strip() or not value.isprintable() or re.search(r"\b(?:lbr_|lbk_|lb[a-z][0-9]+\.)", value):
            raise ValueError("Use a short label without credential material")
        return value

    @field_validator("bot_ids")
    @classmethod
    def explicit_bots(cls, values):
        if any(value <= 0 for value in values) or len(set(values)) != len(values):
            raise ValueError("Use explicit, distinct positive bot IDs")
        return values

    @field_validator("scopes")
    @classmethod
    def explicit_scopes(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Select each scope once")
        return values


class PreviewChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Scope
    configuration: dict | None
    remove_node_ids: list[str] = Field(default_factory=list, max_length=512)


class ApplyChange(PreviewChange):
    receipt: str = Field(max_length=80)


class ProvisionedBot(BotCreate):
    model_config = ConfigDict(extra="forbid")


class ApplyProvisioning(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bot: ProvisionedBot
    receipt: str = Field(max_length=114)
    confirmed: Literal[True]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _deny(status, code):
    raise HTTPException(status, {"code": code}, headers={"Cache-Control": "no-store"})


def _issuer(db, user_id, token_version, *, status=401):
    user = db.get(User, user_id)
    if (
        user is None
        or user.effective_token_version != token_version
        or user.must_change_password
        or user.role not in ("user", "admin", "super_admin")
    ):
        _deny(status, "configuration_issuer_unavailable")
    return user


def _can_edit(bot, issuer):
    return bool(bot and not bot.is_deleted and (bot.user_id == issuer.id or issuer.role == "super_admin"))


def _pin(bot):
    return {"id": bot.id, "uuid": bot.uuid, "revision": bot.identity_revision, "owner": bot.user_id}


def _pins(db, issuer, ids):
    entries = []
    for bot_id in ids:
        bot = db.get(Bot, bot_id)
        if not _can_edit(bot, issuer):
            _deny(403, "configuration_bot_permission")
        entries.append(_pin(bot))
    return entries


def _metadata(row):
    return {
        "id": row.public_id,
        "label": row.label,
        "scopes": json.loads(row.scopes),
        "bot_ids": [entry["id"] for entry in json.loads(row.bots)],
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "revoked_at": _iso(row.revoked_at),
    }


def _secret(row):
    token = f"lbc1.{row.public_id}.{secrets.token_urlsafe(32)}"
    row.digest = hashlib.sha256(token.encode()).hexdigest()
    return token


def _audit(db, user, row, request, action, *, bot=None, detail=None):
    db.add(
        AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role=user.role,
            action=action,
            target_type="bot" if bot else "configuration_credential",
            target_id=bot.id if bot else row.id,
            target_name=f"#{bot.id}" if bot else row.public_id,
            detail=json.dumps({"credential_id": row.public_id, **(detail or {})}),
            ip_address=request.client.host if request.client else None,
            result="success",
        )
    )


def _row(db, public_id, issuer=None):
    query = db.query(ConfigurationCredential).filter_by(public_id=public_id)
    if issuer is not None:
        query = query.filter_by(issuer_id=issuer.id)
    return query.one_or_none()


@router.post("/api/configuration-credentials", status_code=201)
@limiter.limit("10/minute")
def create(
    request: Request,
    response: Response,
    body: CreateCredential,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    ident = (user.id, user.effective_token_version)
    with _guard:
        db.rollback()
        user = _issuer(db, *ident, status=403)
        now = _now()
        if (
            db.query(ConfigurationCredential)
            .filter(
                ConfigurationCredential.issuer_id == user.id,
                ConfigurationCredential.revoked_at.is_(None),
                ConfigurationCredential.expires_at > now,
            )
            .count()
            >= 32
        ):
            _deny(409, "configuration_credential_limit")
        row = ConfigurationCredential(
            public_id=secrets.token_urlsafe(16),
            label=body.label,
            issuer_id=user.id,
            issuer_token_version=user.effective_token_version,
            scopes=json.dumps(body.scopes),
            bots=json.dumps(_pins(db, user, body.bot_ids)),
            created_at=now,
            expires_at=now + timedelta(seconds=body.expires_in_seconds),
        )
        token = _secret(row)
        db.add(row)
        db.flush()
        _audit(db, user, row, request, "configuration_credential.create")
        result = {**_metadata(row), "credential": token, "shown_once": True}
        db.commit()
        return result


@router.get("/api/configuration-credentials")
def credentials(
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    _issuer(db, user.id, user.effective_token_version, status=403)
    rows = (
        db.query(ConfigurationCredential)
        .filter_by(issuer_id=user.id)
        .order_by(ConfigurationCredential.id.desc())
        .offset(offset)
        .limit(limit)
    )
    bots = db.query(Bot.id, Bot.name).filter(Bot.is_deleted.is_(False)).order_by(Bot.name, Bot.id)
    if user.role != "super_admin":
        bots = bots.filter(Bot.user_id == user.id)
    return {
        "items": [_metadata(row) for row in rows],
        "bots": [{"id": bot.id, "name": bot.name} for bot in bots],
        "limit": limit,
        "offset": offset,
    }


@router.post("/api/configuration-credentials/{public_id}/rotate")
@limiter.limit("10/minute")
def rotate(
    public_id: str,
    request: Request,
    response: Response,
    body: Lifetime,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    ident = (user.id, user.effective_token_version)
    with _guard:
        db.rollback()
        user = _issuer(db, *ident, status=403)
        row = _row(db, public_id, user)
        if row is None:
            _deny(404, "configuration_credential_not_found")
        if row.revoked_at is not None or row.expires_at <= _now():
            _deny(409, "configuration_credential_inactive")
        pinned = json.loads(row.bots)
        if _pins(db, user, [item["id"] for item in pinned]) != pinned:
            _deny(403, "configuration_bot_changed")
        row.issuer_token_version = user.effective_token_version
        row.expires_at = _now() + timedelta(seconds=body.expires_in_seconds)
        token = _secret(row)
        _audit(db, user, row, request, "configuration_credential.rotate")
        result = {**_metadata(row), "credential": token, "shown_once": True}
        db.commit()
        return result


@router.delete("/api/configuration-credentials/{public_id}", status_code=204)
def revoke(public_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ident = (user.id, user.effective_token_version)
    with _guard:
        db.rollback()
        user = _issuer(db, *ident, status=403)
        row = _row(db, public_id, user)
        if row is None:
            _deny(404, "configuration_credential_not_found")
        if row.revoked_at is None:
            row.revoked_at = _now()
            _audit(db, user, row, request, "configuration_credential.revoke")
            db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _authenticated_credential(db, request):
    bearer = request.headers.get("Authorization", "")
    token = bearer[7:] if bearer.startswith("Bearer ") else ""
    match = _TOKEN.fullmatch(token) if len(token) == 71 else None
    row = _row(db, match[1]) if match else None
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= _now()
        or not secrets.compare_digest(row.digest, hashlib.sha256(token.encode()).hexdigest())
    ):
        _deny(401, "configuration_credential_invalid")
    issuer = _issuer(db, row.issuer_id, row.issuer_token_version)
    return row, issuer


def _authorized(db, request, bot_id, scope=None):
    row, issuer = _authenticated_credential(db, request)
    bot = db.get(Bot, bot_id)
    if not _can_edit(bot, issuer) or _pin(bot) not in json.loads(row.bots):
        _deny(403, "configuration_bot_permission")
    scopes = json.loads(row.scopes)
    if scope is not None and scope not in scopes:
        _deny(403, "configuration_scope_missing")
    return row, issuer, bot, scopes


def _provisioning_issuer(db, request):
    row, issuer = _authenticated_credential(db, request)
    if json.loads(row.scopes) != ["bot:create"]:
        _deny(403, "configuration_scope_missing")
    return row, issuer


def _provisioning_signature(row, bot, key, expires_at):
    data = json.dumps([row.digest, key, expires_at, bot.model_dump_json()]).encode()
    return hmac.digest(config.JWT_SECRET.encode(), b"bot-provisioning-preview\0" + data, "sha256").hex()


def _safe_provisioning_error(exc):
    if isinstance(exc.detail, dict) and exc.detail.get("code") == "bot_creation_result_unavailable":
        _deny(409, "bot_creation_result_unavailable")
    _deny(exc.status_code, f"bot_provisioning_rejected_{exc.status_code}")


@router.post("/api/bot-provisioning/preview", tags=["Public API"])
@limiter.limit("60/minute")
def preview_provisioning(request: Request, response: Response, bot: ProvisionedBot, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with _guard:
        row, issuer = _provisioning_issuer(db, request)
        if bot.platform.strip().lower() not in {"infoflow", "feishu"}:
            _deny(422, "bot_provisioning_platform_invalid")
        try:
            platform = _validate_bot_creation(bot, issuer, db)
        except HTTPException as exc:
            _safe_provisioning_error(exc)
        key = str(uuid4())
        expires_at = int(datetime.now(timezone.utc).timestamp()) + 300
        receipt = f"{key}.{expires_at}.{_provisioning_signature(row, bot, key, expires_at)}"
        row.last_used_at = _now()
        db.commit()
        return {
            "receipt": receipt,
            "expires_at": expires_at,
            "name": bot.name,
            "bot_type": bot.bot_type.upper(),
            "platform": platform,
            "group_id": bot.group_id,
            "resource_nodes": len(bot.cluster_configs),
            "has_datasource": bot.utilization_config is not None,
        }


@router.post("/api/bot-provisioning/apply", status_code=201, tags=["Public API"])
@limiter.limit("30/minute")
def apply_provisioning(request: Request, response: Response, body: ApplyProvisioning, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with _guard:
        row, issuer = _provisioning_issuer(db, request)
        match = _PROVISIONING_PREVIEW.fullmatch(body.receipt)
        if (
            match is None
            or int(match[2]) <= datetime.now(timezone.utc).timestamp()
            or not hmac.compare_digest(match[3], _provisioning_signature(row, body.bot, match[1], int(match[2])))
        ):
            _deny(409, "bot_provisioning_preview_stale")
        try:
            bot = _create_bot_for_user(request, body.bot, issuer, db, match[1], credential_id=row.public_id)
        except HTTPException as exc:
            _safe_provisioning_error(exc)
        row.last_used_at = _now()
        db.commit()
        remaining_setup = []
        if bot.platform.strip().lower() == "infoflow":
            remaining_setup.append("configure_infoflow_callback")
        if bot.status != "running":
            remaining_setup.append("check_bot_start")
        return {
            "bot_id": bot.id,
            "console_path": f"/bots/{bot.id}",
            "status": bot.status,
            "remaining_setup": remaining_setup,
        }


def _inventory(bot):
    cluster = json.loads(bot.cluster_configs)
    nodes = node_ids(cluster)
    devices = (
        [
            f"{node}:dev{index}"
            for node, models in cluster.items()
            if isinstance(models, list)
            for index in range(len(models))
        ]
        if isinstance(cluster, dict) and bot.bot_type in {"DEVICE", "QUEUE_DEVICE"}
        else []
    )
    return {"node_ids": nodes, "device_ids": devices}


def _current(bot, scope):
    if scope == "datasource:write":
        return public_datasource(bot)
    nodes = saved_node_access(bot)
    return {"version": mapping_version(nodes), "nodes": nodes}


def _proposed(bot, change, current):
    proposed = change.configuration
    if len(json.dumps(proposed).encode()) > MAX_CONFIGURATION_BYTES:
        _deny(413, "configuration_too_large")
    inventory = _inventory(bot)
    if change.scope == "datasource:write":
        if change.remove_node_ids:
            _deny(422, "configuration_invalid")
        if proposed is None:
            return None
        try:
            source = UtilizationConfig.model_validate(proposed)
        except (ValidationError, ValueError):
            _deny(422, "configuration_invalid")
        if not set(source.nodeMapping) <= set(inventory["node_ids"]) or not set(source.deviceMapping) <= set(
            inventory["device_ids"]
        ):
            _deny(422, "configuration_resource_unknown")
        return source.model_dump()
    try:
        updates = parse_access_mapping(json.dumps(proposed), set(inventory["node_ids"]))
    except (ValueError, TypeError, RecursionError):
        _deny(422, "configuration_invalid")
    removed = set(change.remove_node_ids)
    if removed.intersection(updates) or not removed <= set(inventory["node_ids"]):
        _deny(422, "configuration_resource_unknown")
    nodes = {node: entry for node, entry in current["nodes"].items() if node not in removed}
    nodes.update(updates)
    return {"version": mapping_version(nodes), "nodes": nodes}


def _signature(row, bot, scope, expiry, before, after):
    binding = [row.digest, _pin(bot), bot.updated_at.isoformat(), _inventory(bot), scope, expiry, before, after]
    payload = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(config.JWT_SECRET.encode(), b"bot-configuration-preview\0" + payload, hashlib.sha256).hexdigest()


def _change_counts(before, after):
    return {
        "added": len(after.keys() - before.keys()),
        "removed": len(before.keys() - after.keys()),
        "updated": sum(before[key] != after[key] for key in before.keys() & after.keys()),
    }


@router.get("/api/bots/{bot_id}/configuration-access", tags=["Public API"])
@limiter.limit("120/minute")
def inspect_configuration(bot_id: int, request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with _guard:
        row, _, bot, scopes = _authorized(db, request, bot_id)
        settings = {scope: _current(bot, scope) for scope in scopes}
        payload = {"bot_id": bot.id, "scopes": scopes, **_inventory(bot), "configuration": settings}
        row.last_used_at = _now()
        db.commit()
        return payload


@router.post("/api/bots/{bot_id}/configuration-access/preview", tags=["Public API"])
@limiter.limit("60/minute")
def preview(bot_id: int, request: Request, response: Response, body: PreviewChange, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with _guard:
        row, _, bot, _ = _authorized(db, request, bot_id, body.scope)
        before = _current(bot, body.scope)
        after = _proposed(bot, body, before)
        deadline = int(datetime.now(timezone.utc).timestamp()) + 300
        receipt = f"{deadline}.{_signature(row, bot, body.scope, deadline, before, after)}"
        row.last_used_at = _now()
        db.commit()
        return {"scope": body.scope, "before": before, "after": after, "receipt": receipt, "changed": before != after}


@router.put("/api/bots/{bot_id}/configuration-access", tags=["Public API"])
@limiter.limit("30/minute")
def apply(bot_id: int, request: Request, response: Response, body: ApplyChange, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with _guard:
        _, issuer, _, _ = _authorized(db, request, bot_id, body.scope)
        with _owned_bot(bot_id, issuer, db), authority_guard(bot_id):
            db.rollback()
            row, issuer, bot, _ = _authorized(db, request, bot_id, body.scope)
            before = _current(bot, body.scope)
            after = _proposed(bot, body, before)
            match = _PREVIEW.fullmatch(body.receipt)
            if (
                not match
                or int(match[1]) <= datetime.now(timezone.utc).timestamp()
                or not hmac.compare_digest(match[2], _signature(row, bot, body.scope, int(match[1]), before, after))
            ):
                _deny(409, "configuration_preview_stale")
            changed = before != after
            if changed and body.scope == "datasource:write":
                bot.utilization_config = serialized_datasource(
                    UtilizationConfig.model_validate(after) if after else None
                )
            elif changed:
                bot.node_access_config = serialize_node_access(after["nodes"])
            row.last_used_at = _now()
            counts = (
                _change_counts(before["nodes"], after["nodes"])
                if body.scope == "node_access:write"
                else _change_counts(before or {}, after or {})
            )
            _audit(
                db,
                issuer,
                row,
                request,
                "configuration.apply",
                bot=bot,
                detail={"scope": body.scope, **counts},
            )
            db.commit()
            return {"scope": body.scope, "changed": changed, "configuration": after}
