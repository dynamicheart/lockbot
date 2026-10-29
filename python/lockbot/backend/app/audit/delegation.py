"""Explicit, revocable task-note delegation. Never an extension of an ordinary OP key."""

import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app import config
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.rate_limit import limiter

SCOPE = "task_notes:read"
MAX_TTL = 7 * 86400
_TOKEN = re.compile(r"^lba1\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$")
_guard = threading.RLock()
router = APIRouter(tags=["Audit delegation"])


class AuditCredential(Base):
    __tablename__ = "audit_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    issuer_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    issuer_token_version: Mapped[int] = mapped_column(Integer)
    bots: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    bot_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    expires_in_seconds: int = Field(default=86400, ge=60, le=MAX_TTL)

    @field_validator("bot_ids")
    @classmethod
    def explicit_bots(cls, values):
        if any(value <= 0 for value in values) or len(set(values)) != len(values):
            raise ValueError("Use explicit, unique positive bot IDs")
        return values


class GrantRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_seconds: int = Field(default=86400, ge=60, le=MAX_TTL)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _deny(status, detail):
    raise HTTPException(status, detail, headers={"Cache-Control": "no-store"})


def _audit(db, user, action, *, grant, bot_id=None, count=None, ip=None, result="success", reason=None, surface=None):
    # This audit is a disclosure precondition: unlike ordinary best-effort logs it fails closed.
    detail = {"principal": grant.public_id, "scope": SCOPE}
    if reason is not None:
        detail["reason"] = reason
    if count is not None:
        detail.update(surface=surface or "audit.task_notes", returned_count=count)
    db.add(
        AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role=user.role,
            action=action,
            target_type="bot" if bot_id is not None else "audit_credential",
            target_id=bot_id if bot_id is not None else grant.id,
            target_name=f"#{bot_id}" if bot_id is not None else grant.public_id,
            detail=json.dumps(detail),
            ip_address=ip,
            result=result,
        )
    )
    db.flush()


def _admin(db, user_id, token_version):
    db.rollback()
    user = db.get(User, user_id)
    if (
        user is None
        or user.role != "super_admin"
        or user.must_change_password
        or user.effective_token_version != token_version
    ):
        _deny(403, "Current super administrator authority required")
    return user


def _bot_identity(bot):
    return {"id": bot.id, "uuid": bot.uuid, "revision": bot.identity_revision, "owner": bot.user_id}


def _bot_scope(db, bot_ids):
    values = []
    for bot_id in bot_ids:
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted:
            _deny(422, "Every permitted bot must exist and be active in the service")
        values.append(_bot_identity(bot))
    return values


def _metadata(row):
    return {
        "id": row.public_id,
        "name": row.name,
        "scope": row.scope,
        "bot_ids": [item["id"] for item in json.loads(row.bots)],
        "generation": row.generation,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "issuer_id": row.issuer_id,
    }


def _secret(row):
    token = f"lba1.{row.public_id}.{secrets.token_urlsafe(32)}"
    row.digest = hashlib.sha256(token.encode()).hexdigest()
    return token


@router.post("/api/admin/audit-credentials", status_code=201)
@limiter.limit("10/minute")
def create_credential(
    request: Request,
    response: Response,
    body: GrantCreate,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    user_id, version = user.id, user.effective_token_version
    with _guard:
        user = _admin(db, user_id, version)
        active = (
            db.query(AuditCredential)
            .filter(AuditCredential.revoked_at.is_(None), AuditCredential.expires_at > _now())
            .count()
        )
        if active >= 128:
            _deny(409, "At most 128 active audit credentials are permitted")
        now = _now()
        row = AuditCredential(
            public_id=secrets.token_urlsafe(16),
            name=body.name,
            scope=SCOPE,
            generation=1,
            issuer_id=user.id,
            issuer_token_version=version,
            bots=json.dumps(_bot_scope(db, body.bot_ids)),
            created_at=now,
            expires_at=now + timedelta(seconds=body.expires_in_seconds),
        )
        token = _secret(row)
        db.add(row)
        db.flush()
        _audit(db, user, "audit_credential.create", grant=row, ip=request.client.host if request.client else None)
        db.commit()
        return {**_metadata(row), "credential": token, "shown_once": True}


@router.get("/api/admin/audit-credentials")
def credentials(
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    _admin(db, user.id, user.effective_token_version)
    rows = db.query(AuditCredential).order_by(AuditCredential.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_metadata(row) for row in rows], "limit": limit, "offset": offset}


def _managed_grant(db, public_id):
    row = db.execute(select(AuditCredential).where(AuditCredential.public_id == public_id)).scalar_one_or_none()
    if row is None:
        _deny(404, "Audit credential not found")
    return row


@router.post("/api/admin/audit-credentials/{public_id}/rotate")
@limiter.limit("10/minute")
def rotate_credential(
    public_id: str,
    request: Request,
    response: Response,
    body: GrantRotate,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    user_id, version = user.id, user.effective_token_version
    with _guard:
        user = _admin(db, user_id, version)
        row = _managed_grant(db, public_id)
        if row.revoked_at is not None:
            _deny(409, "Revoked credentials cannot be reactivated")
        row.bots = json.dumps(_bot_scope(db, [item["id"] for item in json.loads(row.bots)]))
        row.generation += 1
        row.issuer_id, row.issuer_token_version = user.id, version
        row.expires_at = _now() + timedelta(seconds=body.expires_in_seconds)
        token = _secret(row)
        _audit(db, user, "audit_credential.rotate", grant=row, ip=request.client.host if request.client else None)
        db.commit()
        return {**_metadata(row), "credential": token, "shown_once": True}


@router.delete("/api/admin/audit-credentials/{public_id}", status_code=204)
def revoke_credential(
    public_id: str,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    user_id, version = user.id, user.effective_token_version
    with _guard:
        user = _admin(db, user_id, version)
        row = _managed_grant(db, public_id)
        if row.revoked_at is None:
            row.revoked_at = _now()
            row.generation += 1
            _audit(db, user, "audit_credential.revoke", grant=row, ip=request.client.host if request.client else None)
            db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


class ReadBudget:
    def __init__(self):
        self.hits = OrderedDict()
        self.lock = threading.Lock()

    def check(self, public_id):
        now = time.monotonic()
        with self.lock:
            hits = [value for value in self.hits.pop(public_id, []) if now - value < 60]
            if len(hits) >= 30:
                self.hits[public_id] = hits
                raise HTTPException(
                    429, "Audit read budget exceeded", headers={"Retry-After": "60", "Cache-Control": "no-store"}
                )
            self.hits[public_id] = [*hits, now]
            while len(self.hits) > 256:
                self.hits.popitem(last=False)


read_budget = ReadBudget()


def _authorize(db, request, bot_id):
    db.rollback()
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    match = _TOKEN.fullmatch(token)
    if match is None:
        _deny(401, "A dedicated audit credential is required")
    row = db.execute(select(AuditCredential).where(AuditCredential.public_id == match[1])).scalar_one_or_none()
    if row is None or not secrets.compare_digest(row.digest, hashlib.sha256(token.encode()).hexdigest()):
        _deny(401, "Audit credential is invalid or expired")
    issuer = db.get(User, row.issuer_id)

    def reject(status, detail, reason):
        # Only proven credentials have an attributable principal. Never log submitted headers.
        if issuer is not None:
            _audit(
                db,
                issuer,
                "task_notes.denied",
                grant=row,
                bot_id=bot_id,
                count=0,
                result="denied",
                reason=reason,
                ip=request.client.host if request.client else None,
            )
            db.commit()
        _deny(status, detail)

    if row.scope != SCOPE or row.revoked_at is not None or row.expires_at <= _now():
        reject(401, "Audit credential is invalid or expired", "inactive_credential")
    if (
        issuer is None
        or issuer.role != "super_admin"
        or issuer.must_change_password
        or issuer.effective_token_version != row.issuer_token_version
    ):
        reject(403, "Audit delegation issuer no longer has authority", "issuer_authority")
    bot = db.get(Bot, bot_id)
    expected = next((item for item in json.loads(row.bots) if item["id"] == bot_id), None)
    if bot is None or bot.is_deleted or expected != _bot_identity(bot):
        reject(403, "Bot is outside this audit delegation or its identity changed", "bot_scope")
    request.state.traffic_bot_id = bot.id
    return row, issuer, bot


def _notes_projection(raw):
    from lockbot.backend.app.resource_api.privacy import public_event

    result = public_event(raw)
    for key in ("agent_label", "purpose"):
        if isinstance(raw.get(key), str):
            result[key] = raw[key]
    result["untrusted_text"] = True
    return result


def _cursor_decode(cursor, row, bot, query, limit):
    try:
        value = jwt.decode(cursor, config.JWT_SECRET, algorithms=["HS256"], audience="audit-notes-page")
        expected = [row.public_id, row.generation, _bot_identity(bot), query, limit]
        if value["scope"] != expected:
            raise ValueError()
        return value["cursor"]
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        _deny(400, "Audit cursor is invalid, expired or belongs to another scope")


def _finish_read(db, request, bot_id, ref, generation, count):
    row, issuer, bot = _authorize(db, request, bot_id)
    if row.public_id != ref or row.generation != generation:
        _deny(401, "Audit delegation changed during the read")
    _audit(
        db,
        issuer,
        "task_notes.read",
        grant=row,
        bot_id=bot_id,
        count=count,
        surface="agent_history" if request.url.path.endswith("/agent-history") else "current_task_notes",
        ip=request.client.host if request.client else None,
    )
    db.commit()
    # Role/password changes and identity transfers can occur outside this API's guard.
    _authorize(db, request, bot_id)
    return row, bot


@router.get("/api/audit/bots/{bot_id}/agent-history")
@limiter.limit("60/minute")
def task_note_history(
    bot_id: int,
    request: Request,
    response: Response,
    cursor: str | None = Query(None, max_length=2048),
    query: str = Query("", max_length=128),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.resource_api.event_log import READ_BUDGET, read_history

    response.headers["Cache-Control"] = "no-store"
    row, _, bot = _authorize(db, request, bot_id)
    ref, generation = row.public_id, row.generation
    expiry = min(row.expires_at, _now() + timedelta(minutes=15)).replace(tzinfo=timezone.utc).timestamp()
    read_budget.check(ref)
    raw_cursor = _cursor_decode(cursor, row, bot, query, limit) if cursor else None
    db.rollback()  # File I/O must not hold a stale authority snapshot or SQLite read transaction.
    result = read_history(bot_id, limit=limit, cursor=raw_cursor, query=query, private=True, project=_notes_projection)
    with _guard:
        row, issuer, bot = _authorize(db, request, bot_id)
        if row.public_id != ref or row.generation != generation:
            _deny(401, "Audit delegation changed during the read")
        next_cursor = result.get("next_cursor")
        if next_cursor:
            result["next_cursor"] = jwt.encode(
                {
                    "aud": "audit-notes-page",
                    "exp": expiry,
                    "scope": [ref, generation, _bot_identity(bot), query, limit],
                    "cursor": next_cursor,
                },
                config.JWT_SECRET,
                algorithm="HS256",
            )
        _finish_read(db, request, bot_id, ref, generation, len(result["items"]))
    return {
        "bot_id": bot_id,
        "scope": SCOPE,
        "audit_principal": ref,
        **result,
        "coverage": {"source": "agent_operation_notes", "complete_history": False, "read_budget_bytes": READ_BUDGET},
    }


@router.get("/api/audit/bots/{bot_id}/task-notes")
@limiter.limit("60/minute")
def current_task_notes(
    bot_id: int,
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
    from lockbot.backend.app.resource_api.usage import active_declarations, declarations_for_state

    response.headers["Cache-Control"] = "no-store"
    row, _, bot = _authorize(db, request, bot_id)
    ref, generation = row.public_id, row.generation
    read_budget.check(ref)
    rows = active_declarations(db, bot, _now(), limit=limit + 1)
    try:
        with bot_manager.admit(bot_id, for_read=True) as lease:
            captured_at, state = lease.bot.state_snapshot()
            values = declarations_for_state(rows[:limit], state, captured_at)
    except (BotNotRunning, InstanceRetiring, AdmissionFull):
        _deny(503, "Current task notes require an available resource-state snapshot; use history for past notes")
    # Personal declaration keys, holding proofs and authorization labels are not delegated.
    allowed = {
        "resource_id",
        "actor",
        "task_category",
        "public_task",
        "agent_label",
        "purpose",
        "updated_at",
        "expires_at",
        "untrusted_text",
    }
    from lockbot.backend.app.resource_api.event_log import _safe

    items = [_safe({key: value for key, value in item.items() if key in allowed}) for item in values]
    with _guard:
        _finish_read(db, request, bot_id, ref, generation, len(items))
    return {
        "bot_id": bot_id,
        "scope": SCOPE,
        "audit_principal": ref,
        "captured_at": captured_at.isoformat(),
        "items": items,
        "truncated": len(rows) > limit,
        "limit": limit,
        "coverage": {"source": "live_task_declarations", "complete_history": False},
    }
