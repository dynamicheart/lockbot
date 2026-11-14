"""Verified-agent traffic accounting. No token, command body or purpose text is stored here."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, and_, case, delete, func, or_, select, tuple_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.resource_api.privacy import audit_private_read, private_task_view
from lockbot.backend.app.resource_auth.models import ResourceBinding

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bots", tags=["bots"])
AGENT_ACTIVITY_WINDOW_SECONDS = 3600
AGENT_ACTIVITY_RETENTION = timedelta(days=30)
MAX_TRACKED_LABELS = 32
_MIN_ACTIVITY_UPDATE = timedelta(seconds=30)
_SECRET_LABEL = re.compile(
    r"\b(?:lbr_|lbk_|lba1\.|lbv1\.|Bearer\s+)|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|/#/r/[0-9]+/[A-Za-z0-9_-]{22}",
    re.IGNORECASE,
)


class AgentActivity(Base):
    __tablename__ = "agent_activity"

    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("resource_bindings.id"), primary_key=True)
    read_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    write_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refused_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_status: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


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


def _normalized_label(value):
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value):
        return None
    value = value.strip()
    return value if 1 <= len(value) <= 64 and not _SECRET_LABEL.search(value) else None


def _activity_label(request, event):
    headers = request.headers.getlist("X-Lockbot-Agent-Label")
    if headers:
        if len(headers) != 1:
            return None
        value = headers[0]
        if not value or len(value) > 768 or not value.isascii() or re.search(r"%(?![0-9A-Fa-f]{2})", value):
            return None
        try:
            return _normalized_label(unquote(value, errors="strict"))
        except UnicodeError:
            return None
    label = event.get("agent_label") if event else None
    return "" if label is None else _normalized_label(label)


def _client_ip(request):
    # ASGI supplies the peer (or the server's explicitly trusted proxy result).
    # Untrusted forwarding headers do not supply a second observation.
    try:
        host = request.client.host if request.client else ""
        return str(ipaddress.ip_address(host)) if "%" not in host else None
    except ValueError:
        return None


def _write_label_activity(db, actor, label, client_ip, observed_at):
    # The aggregate write already holds SQLite's write lock. Recheck the exact
    # binding generation before touching detail and keep the bound in this transaction.
    binding = db.get(ResourceBinding, actor.binding_record_id)
    if binding is None or binding.public_id != actor.binding_public_id:
        return
    bot = db.get(Bot, actor.bot_id)
    if (
        bot is None
        or bot.is_deleted
        or not bot.resource_auth_enabled
        or binding.credential_generation != actor.credential_generation
        or not db.scalar(select(valid_binding_filter(bot, _utcnow())).where(ResourceBinding.id == binding.id))
    ):
        return
    record = AgentLabelActivity
    current = db.get(record, (binding.id, label))
    if current is not None and (
        current.last_seen_at > observed_at
        or (current.last_observed_ip == client_ip and current.last_seen_at > observed_at - _MIN_ACTIVITY_UPDATE)
    ):
        return
    if current is None:
        db.add(record(binding_id=binding.id, agent_label=label, last_observed_ip=client_ip, last_seen_at=observed_at))
        if label:
            db.flush()
            overflow = (
                select(record.agent_label)
                .where(record.binding_id == binding.id, record.agent_label != "")
                .order_by(record.last_seen_at.desc(), record.agent_label)
                .offset(MAX_TRACKED_LABELS)
            )
            db.execute(delete(record).where(record.binding_id == binding.id, record.agent_label.in_(overflow)))
    else:
        current.last_observed_ip = client_ip
        current.last_seen_at = observed_at


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record(engine, actor, kind, status, label, client_ip, observed_at):
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
            if label is not None and 200 <= status < 400:
                try:
                    with db.begin_nested():
                        _write_label_activity(db, actor, label, client_ip, observed_at)
                except Exception:
                    logger.exception("Agent label activity write failed for binding %s", actor.binding_record_id)
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
                        _activity_label(request, event),
                        _client_ip(request),
                        _utcnow(),
                    )
                if event is not None:
                    await asyncio.to_thread(
                        write_event, actor=actor, event=event, status=status, duration_ms=duration_ms
                    )


def valid_binding_filter(bot, now):
    from lockbot.core.platforms.registry import normalize_platform_name

    return func.coalesce(
        and_(
            ResourceBinding.platform == normalize_platform_name(bot.platform),
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


@router.get("/{bot_id}/agent-summary")
def get_agent_summary(
    bot_id: int, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from lockbot.backend.app.bots.router import _get_user_bot

    response.headers["Cache-Control"] = "no-store"
    bot = _get_user_bot(bot_id, user, db)
    return agent_summary(db, [bot])


@router.get("/{bot_id}/agent-activity")
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
