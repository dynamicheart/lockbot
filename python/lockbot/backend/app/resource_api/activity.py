"""Verified-agent traffic accounting. No token, command body or purpose text is stored here."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, and_, func, or_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.resource_auth.models import ResourceBinding

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bots", tags=["bots"])


class AgentActivity(Base):
    __tablename__ = "agent_activity"

    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("resource_bindings.id"), primary_key=True)
    read_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    write_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refused_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_status: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _record(engine, actor, kind, status):
    values = {
        "binding_id": actor.binding_record_id,
        "read_requests": int(kind == "read"),
        "write_requests": int(kind == "write"),
        "refused_requests": int(status >= 400),
        "limited_requests": int(status == 429),
        "last_status": status,
        "last_seen_at": datetime.utcnow(),
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
                    "last_status": status,
                    "last_seen_at": values["last_seen_at"],
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
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            actor = getattr(request.state, "resource_actor", None)
            engine = getattr(request.state, "resource_activity_engine", None)
            if actor is not None and engine is not None:
                kind = getattr(request.state, "resource_activity_kind", "read" if request.method == "GET" else "write")
                await asyncio.to_thread(_record, engine, actor, kind, status)


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
def get_agent_summary(bot_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from lockbot.backend.app.bots.router import _get_user_bot

    bot = _get_user_bot(bot_id, user, db)
    return agent_summary(db, [bot])


@router.get("/{bot_id}/agent-activity")
def get_agent_activity(
    bot_id: int,
    status: str = Query("active", pattern="^(active|history|all)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.router import _get_user_bot

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
    items = []
    for binding, activity in rows:
        active = (
            binding.revoked_at is None
            and binding.bound_bot_uuid == bot.uuid
            and binding.bound_identity_revision == bot.identity_revision
            and (binding.credential_expires_at is None or binding.credential_expires_at > now)
        )
        items.append(
            dict(
                binding_id=binding.public_id,
                label=binding.label,
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
            )
        )
    return dict(bot_id=bot_id, total=total, page=page, limit=limit, items=items)
