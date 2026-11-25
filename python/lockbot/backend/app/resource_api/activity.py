"""Verified-agent traffic accounting. No token, command body or purpose text is stored here."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    and_,
    case,
    delete,
    func,
    literal,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app.auth.dependencies import get_current_user, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.resource_api.privacy import audit_private_read, private_task_view
from lockbot.backend.app.resource_auth.models import ResourceBinding

logger = logging.getLogger(__name__)
router = APIRouter(tags=["bots"])
AGENT_ACTIVITY_WINDOW_SECONDS = 3600
AGENT_ACTIVITY_RETENTION = timedelta(days=30)
MAX_TRACKED_LABELS = 32
_COUNTERS = ("read_requests", "write_requests", "refused_requests", "limited_requests")


class AgentActivity(Base):
    __tablename__ = "agent_activity"

    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("resource_bindings.id"), primary_key=True)
    read_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    write_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refused_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_status: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class V2AgentActivity(Base):
    __tablename__ = "agent_v2_activity"

    principal_id: Mapped[str] = mapped_column(ForeignKey("agent_v2_credentials.public_id"), primary_key=True)
    read_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    write_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    refused_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    limited_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    last_status: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class V2ActivityRecorder:
    """Write completed requests in batches and keep database I/O off the v2 request path."""

    def __init__(self, engine):
        self.engine = engine
        self._pending = {}
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = asyncio.Event()

    def record(self, principal_id, method, status):
        row = {
            "principal_id": principal_id,
            "read_requests": int(method in {"GET", "HEAD"}),
            "write_requests": int(method not in {"GET", "HEAD"}),
            "refused_requests": int(status >= 400),
            "limited_requests": int(status == 429),
            "last_status": status,
            "last_seen_at": _utcnow(),
        }
        with self._lock:
            self._merge(row)

    def _merge(self, row):
        previous = self._pending.setdefault(row["principal_id"], row)
        if previous is row:
            return
        for field in _COUNTERS:
            previous[field] += row[field]
        if previous["last_seen_at"] <= row["last_seen_at"]:
            previous.update(last_seen_at=row["last_seen_at"], last_status=row["last_status"])

    def flush(self):
        with self._flush_lock:
            with self._lock:
                pending, self._pending = self._pending, {}
            if not pending:
                return
            statement = insert(V2AgentActivity)
            incoming = statement.excluded
            statement = statement.on_conflict_do_update(
                index_elements=[V2AgentActivity.principal_id],
                set_={
                    **{key: getattr(V2AgentActivity, key) + getattr(incoming, key) for key in _COUNTERS},
                    "last_status": case(
                        (V2AgentActivity.last_seen_at <= incoming.last_seen_at, incoming.last_status),
                        else_=V2AgentActivity.last_status,
                    ),
                    "last_seen_at": func.max(V2AgentActivity.last_seen_at, incoming.last_seen_at),
                },
            )
            try:
                with self.engine.begin() as conn:
                    conn.execute(statement, list(pending.values()))
            except SQLAlchemyError:
                with self._lock:
                    for row in pending.values():
                        self._merge(row)
                logger.warning("Could not persist batched Agent v2 counters")

    async def run(self):
        while True:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            await asyncio.to_thread(self.flush)
            if self._stop.is_set():
                return

    def stop(self):
        self._stop.set()


class AgentLabelActivity(Base):
    __tablename__ = "agent_label_activity"
    __table_args__ = (
        Index("ix_agent_label_activity_binding_seen", "binding_id", "last_seen_at", "agent_label"),
        Index("ix_agent_label_activity_seen", "last_seen_at"),
    )

    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("resource_bindings.id"), primary_key=True)
    # Empty is the internal legacy bucket and cannot be a valid declared label.
    agent_label: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_observed_ip: Mapped[str | None] = mapped_column(String(45))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record(engine, actor, kind, status, observed_at):
    values = {
        "binding_id": actor.binding_record_id,
        "read_requests": int(kind == "read"),
        "write_requests": int(kind == "write"),
        "refused_requests": int(status >= 400),
        "limited_requests": int(status == 429),
        "last_status": status,
        "last_seen_at": observed_at,
    }
    try:
        with Session(bind=engine) as db:
            # One atomic upsert, so concurrent responses cannot lose counter increments.
            statement = insert(AgentActivity).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[AgentActivity.binding_id],
                set_={
                    **{
                        key: getattr(AgentActivity, key) + values[key]
                        for key in ("read_requests", "write_requests", "refused_requests", "limited_requests")
                    },
                    "last_status": case(
                        (AgentActivity.last_seen_at <= observed_at, status), else_=AgentActivity.last_status
                    ),
                    "last_seen_at": func.max(AgentActivity.last_seen_at, observed_at),
                },
            )
            db.execute(statement)
            db.commit()
    except Exception:
        logger.exception("Agent activity write failed for binding %s", actor.binding_record_id)


def install_activity_middleware(app):
    @app.middleware("http")
    async def activity(request: Request, call_next):
        status = 500
        started = time.monotonic()
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            principal = getattr(request.state, "v2_activity_principal", None)
            recorder = getattr(request.app.state, "v2_agent_activity", None)
            if principal is not None and recorder is not None:
                recorder.record(principal, request.method, status)
            actor = getattr(request.state, "resource_actor", None)
            engine = getattr(request.state, "resource_activity_engine", None)
            if actor is not None:
                from lockbot.backend.app.resource_api.event_log import event_for_request, write_event

                event = event_for_request(request)
                if engine is not None:
                    kind = getattr(
                        request.state, "resource_activity_kind", "read" if request.method == "GET" else "write"
                    )
                    await asyncio.to_thread(
                        _record,
                        engine,
                        actor,
                        kind,
                        status,
                        _utcnow(),
                    )
                if event is not None:
                    await asyncio.to_thread(
                        write_event, actor=actor, event=event, status=status, duration_ms=duration_ms
                    )


def valid_binding_filter(bot, now):
    from lockbot.core.platforms.registry import DEFAULT_PLATFORM, normalize_platform_name

    # The service-wide inventory compares against joined bot rows.
    platform = (
        func.coalesce(func.nullif(func.lower(func.trim(Bot.platform)), ""), DEFAULT_PLATFORM)
        if bot is Bot
        else normalize_platform_name(bot.platform)
    )
    return func.coalesce(
        and_(
            ResourceBinding.platform == platform,
            ResourceBinding.bound_bot_id == bot.id,
            ResourceBinding.bound_bot_uuid == bot.uuid,
            ResourceBinding.bound_identity_revision == bot.identity_revision,
            ResourceBinding.revoked_at.is_(None),
            or_(ResourceBinding.credential_expires_at.is_(None), ResourceBinding.credential_expires_at > now),
        ),
        False,
    )


def binding_label_activity(db, binding_id, *, include_items=False):
    now = _utcnow()
    since = now - timedelta(seconds=AGENT_ACTIVITY_WINDOW_SECONDS)
    query = db.query(AgentLabelActivity).filter(
        AgentLabelActivity.binding_id == binding_id, AgentLabelActivity.last_seen_at > now - AGENT_ACTIVITY_RETENTION
    )
    payload = {"window_seconds": AGENT_ACTIVITY_WINDOW_SECONDS}
    if include_items:
        rows = query.order_by(AgentLabelActivity.last_seen_at.desc(), AgentLabelActivity.agent_label).all()
        payload["recent_active_agents"] = sum(bool(row.agent_label) and row.last_seen_at > since for row in rows)
        payload["items"] = [
            {
                "agent_label": row.agent_label or None,
                "last_observed_ip": row.last_observed_ip,
                "last_seen_at": row.last_seen_at.replace(tzinfo=timezone.utc).isoformat(),
            }
            for row in rows
        ]
    else:
        payload["recent_active_agents"] = query.filter(
            AgentLabelActivity.agent_label != "", AgentLabelActivity.last_seen_at > since
        ).count()
    return payload


def recent_label_activity(db, bot_id=None):
    from lockbot.core.platforms.registry import DEFAULT_PLATFORM

    now = _utcnow()
    since = now - timedelta(seconds=AGENT_ACTIVITY_WINDOW_SECONDS)
    query = (
        db.query(AgentLabelActivity)
        .join(ResourceBinding, AgentLabelActivity.binding_id == ResourceBinding.id)
        .join(Bot, ResourceBinding.bound_bot_id == Bot.id)
        .filter(
            Bot.is_deleted.is_(False),
            Bot.resource_auth_enabled.is_(True),
            ResourceBinding.platform
            == func.coalesce(func.nullif(func.lower(func.trim(Bot.platform)), ""), DEFAULT_PLATFORM),
            ResourceBinding.bound_bot_uuid == Bot.uuid,
            ResourceBinding.bound_identity_revision == Bot.identity_revision,
            ResourceBinding.revoked_at.is_(None),
            or_(ResourceBinding.credential_expires_at.is_(None), ResourceBinding.credential_expires_at > now),
            AgentLabelActivity.agent_label != "",
            AgentLabelActivity.last_seen_at > since,
        )
    )
    if bot_id is not None:
        query = query.filter(Bot.id == bot_id)
    return {"window_seconds": AGENT_ACTIVITY_WINDOW_SECONDS, "recent_active_agents": query.count()}


def cleanup_label_activity(db, batch_size=500):
    cutoff = _utcnow() - AGENT_ACTIVITY_RETENTION
    obsolete = (
        select(AgentLabelActivity.binding_id, AgentLabelActivity.agent_label)
        .where(AgentLabelActivity.last_seen_at <= cutoff)
        .order_by(AgentLabelActivity.last_seen_at)
        .limit(batch_size)
        .cte()
    )
    db.execute(
        delete(AgentLabelActivity).where(
            AgentLabelActivity.last_seen_at <= cutoff,
            tuple_(AgentLabelActivity.binding_id, AgentLabelActivity.agent_label).in_(
                select(obsolete.c.binding_id, obsolete.c.agent_label)
            ),
        )
    )
    db.commit()


def agent_summary(db, bots):
    from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
    from lockbot.backend.app.resource_api.usage import active_declarations, declarations_for_state

    now = datetime.utcnow()
    totals = dict(
        authorizations=0, usage_registrations=0, reads=0, writes=0, refused=0, limited=0, usage_unavailable_bots=0
    )
    for bot in bots:
        totals["authorizations"] += db.query(ResourceBinding).filter(valid_binding_filter(bot, now)).count()
        counters = (
            db.query(
                *[
                    func.coalesce(func.sum(getattr(AgentActivity, key)), 0)
                    for key in ("read_requests", "write_requests", "refused_requests", "limited_requests")
                ]
            )
            .join(ResourceBinding, AgentActivity.binding_id == ResourceBinding.id)
            .filter(ResourceBinding.bound_bot_id == bot.id)
            .one()
        )
        for key, value in zip(("reads", "writes", "refused", "limited"), counters, strict=True):
            totals[key] += value
        if bot.status != "running":
            continue
        try:
            with bot_manager.admit(bot.id) as lease:
                state = lease.bot.state_snapshot()[1]
                totals["usage_registrations"] += len(
                    declarations_for_state(
                        active_declarations(db, bot, now, limit=None), state, now.replace(tzinfo=timezone.utc)
                    )
                )
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            totals["usage_unavailable_bots"] += 1
    return totals


@router.get("/api/bots/{bot_id}/agent-summary")
def get_agent_summary(
    bot_id: int, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from lockbot.backend.app.bots.router import _get_user_bot

    response.headers["Cache-Control"] = "no-store"
    bot = _get_user_bot(bot_id, user, db)
    return agent_summary(db, [bot])


def v2_agent_grants(now):
    from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
    from lockbot.backend.app.resource_api.v2.binding import PersonalCredential, PersonalGrant

    platform = func.lower(Bot.platform)
    grant_valid = func.coalesce(
        and_(
            Bot.is_deleted.is_(False),
            Bot.resource_auth_enabled.is_(True),
            PersonalGrant.bot_uuid == Bot.uuid,
            PersonalGrant.bot_revision == Bot.identity_revision,
            or_(Bot.binding_accept_after.is_(None), PersonalGrant.confirmed_at > Bot.binding_accept_after),
            platform == PersonalCredential.platform,
            or_(
                and_(platform == "infoflow", PersonalCredential.namespace == "infoflow"),
                and_(platform == "feishu", PersonalCredential.namespace == "feishu:" + FeishuApplication.app_id),
            ),
        ),
        False,
    )
    expired = and_(
        PersonalCredential.credential_expires_at.is_not(None), PersonalCredential.credential_expires_at <= now
    )
    query = (
        select(
            PersonalGrant.principal_id,
            and_(grant_valid, ~expired).label("active"),
        )
        .select_from(PersonalGrant)
        .join(PersonalCredential, PersonalCredential.public_id == PersonalGrant.principal_id)
        .join(Bot, Bot.id == PersonalGrant.bot_id)
        .outerjoin(FeishuGroupRoute, and_(FeishuGroupRoute.bot_id == Bot.id, FeishuGroupRoute.chat_id == Bot.group_id))
        .outerjoin(FeishuApplication, FeishuApplication.id == FeishuGroupRoute.application_id)
    )
    return query


def count_v2_agent_tokens(db, now):
    grants = v2_agent_grants(now).subquery()
    return db.scalar(select(func.count(func.distinct(grants.c.principal_id))).where(grants.c.active)) or 0


@router.get("/api/bots/{bot_id}/agent-activity")
def get_agent_activity(
    bot_id: int,
    response: Response,
    include_private: bool = False,
    status: str = Query("active", pattern="^(active|history|all)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.router import _get_user_bot

    response.headers["Cache-Control"] = "no-store"
    bot = _get_user_bot(bot_id, user, db)
    now = datetime.utcnow()
    valid = valid_binding_filter(bot, now)
    query = (
        db.query(ResourceBinding, AgentActivity)
        .outerjoin(AgentActivity, AgentActivity.binding_id == ResourceBinding.id)
        .filter(ResourceBinding.bound_bot_id == bot_id)
    )
    if status == "active":
        query = query.filter(valid)
    elif status == "history":
        query = query.filter(~valid)
    total = query.count()
    rows = (
        query.order_by(AgentActivity.last_seen_at.desc(), ResourceBinding.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    private = private_task_view(db, user, include_private)
    items = []
    for index, (binding, activity) in enumerate(rows):
        active = (
            binding.revoked_at is None
            and binding.bound_bot_uuid == bot.uuid
            and binding.bound_identity_revision == bot.identity_revision
            and (binding.credential_expires_at is None or binding.credential_expires_at > now)
        )
        items.append(
            dict(
                row_key=f"{page}:{index}",
                binding_id=binding.public_id if private else None,
                label=binding.label if private else None,
                platform=binding.platform,
                external_user_id=binding.external_user_id,
                revoked=binding.revoked_at is not None,
                active=active,
                **{
                    key: getattr(activity, key) if activity else (None if key in ("last_status", "last_seen_at") else 0)
                    for key in (
                        "read_requests",
                        "write_requests",
                        "refused_requests",
                        "limited_requests",
                        "last_status",
                        "last_seen_at",
                    )
                },
                agent_activity=binding_label_activity(db, binding.id, include_items=private)
                if active and bot.resource_auth_enabled
                else {"window_seconds": AGENT_ACTIVITY_WINDOW_SECONDS, "recent_active_agents": 0},
            )
        )
    if private and items:
        audit_private_read(db, user, bot_id=bot_id, surface="management.agent_activity")
    return dict(bot_id=bot_id, total=total, page=page, limit=limit, items=items)


@router.get("/api/admin/agent-activity")
def get_service_agent_activity(
    response: Response,
    status: str = Query("active", pattern="^(active|history|all)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    include_legacy: bool = False,
    _user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.resource_api.v2.binding import PersonalCredential

    response.headers["Cache-Control"] = "no-store"
    now = _utcnow()
    grants = v2_agent_grants(now).subquery()
    active = (
        select(grants.c.principal_id)
        .where(grants.c.active, grants.c.principal_id == PersonalCredential.public_id)
        .exists()
    )
    query = (
        select(
            literal("v2").label("version"),
            literal(None, type_=String).label("bot_name"),
            PersonalCredential.public_id.label("sort_key"),
            PersonalCredential.platform,
            PersonalCredential.external_user_id,
            active.label("active"),
            and_(
                PersonalCredential.credential_expires_at.is_not(None), PersonalCredential.credential_expires_at <= now
            ).label("expired"),
            *(func.coalesce(getattr(V2AgentActivity, key), 0).label(key) for key in _COUNTERS),
            V2AgentActivity.last_seen_at,
        )
        .select_from(PersonalCredential)
        .outerjoin(V2AgentActivity, V2AgentActivity.principal_id == PersonalCredential.public_id)
    )
    if include_legacy:
        legacy = (
            select(
                literal("v1").label("version"),
                Bot.name.label("bot_name"),
                ResourceBinding.public_id.label("sort_key"),
                ResourceBinding.platform,
                ResourceBinding.external_user_id,
                valid_binding_filter(Bot, now).label("active"),
                and_(
                    ResourceBinding.credential_expires_at.is_not(None),
                    ResourceBinding.credential_expires_at <= now,
                ).label("expired"),
                *(func.coalesce(getattr(AgentActivity, key), 0).label(key) for key in _COUNTERS),
                AgentActivity.last_seen_at,
            )
            .select_from(ResourceBinding)
            .outerjoin(Bot, Bot.id == ResourceBinding.bound_bot_id)
            .outerjoin(AgentActivity, AgentActivity.binding_id == ResourceBinding.id)
        )
        query = union_all(query, legacy)
    tokens = query.subquery()
    filtered = select(tokens)
    if status != "all":
        filtered = filtered.where(tokens.c.active if status == "active" else ~tokens.c.active)
    total = db.scalar(select(func.count()).select_from(filtered.subquery()))
    rows = db.execute(
        filtered.order_by(tokens.c.last_seen_at.desc().nulls_last(), tokens.c.version, tokens.c.sort_key)
        .offset((page - 1) * limit)
        .limit(limit)
    ).mappings()
    items = []
    for index, row in enumerate(rows):
        item = dict(row, row_key=f"{page}:{index}")
        item.pop("sort_key")
        if not include_legacy:
            item.pop("bot_name")
        items.append(item)
    return {"total": total, "page": page, "limit": limit, "items": items}
