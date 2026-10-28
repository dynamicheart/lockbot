"""Bounded housekeeping for task declarations; resource ownership is never modified."""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import or_

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.resource_api.usage import UsageDeclaration, holding_proof
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.models import ResourceBinding

logger = logging.getLogger(__name__)


def cleanup_usage_once(cursor=0, batch_size=500):
    now = datetime.utcnow()
    removed = 0
    # Close the initial DB transaction before taking a bot authority guard.
    with SessionLocal() as db:
        valid = (
            db.query(ResourceBinding.id)
            .join(Bot, ResourceBinding.bound_bot_id == Bot.id)
            .filter(
                Bot.is_deleted.is_(False),
                ResourceBinding.revoked_at.is_(None),
                ResourceBinding.bound_bot_uuid == Bot.uuid,
                ResourceBinding.bound_identity_revision == Bot.identity_revision,
                or_(ResourceBinding.credential_expires_at.is_(None), ResourceBinding.credential_expires_at > now),
            )
        )
        obsolete = (
            db.query(UsageDeclaration.id)
            .filter(or_(UsageDeclaration.expires_at <= now, UsageDeclaration.binding_id.not_in(valid)))
            .order_by(UsageDeclaration.id)
            .limit(batch_size)
            .all()
        )
        if obsolete:
            removed += (
                db.query(UsageDeclaration)
                .filter(
                    UsageDeclaration.id.in_([row[0] for row in obsolete]),
                    or_(UsageDeclaration.expires_at <= now, UsageDeclaration.binding_id.not_in(valid)),
                )
                .delete(synchronize_session=False)
            )
            db.commit()
        rows = (
            db.query(UsageDeclaration.id, UsageDeclaration.bot_id)
            .filter(UsageDeclaration.id > cursor)
            .order_by(UsageDeclaration.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            rows = (
                db.query(UsageDeclaration.id, UsageDeclaration.bot_id)
                .order_by(UsageDeclaration.id)
                .limit(batch_size)
                .all()
            )
    by_bot = {}
    for row_id, bot_id in rows:
        by_bot.setdefault(bot_id, []).append(row_id)
    for bot_id, ids in by_bot.items():
        try:
            with bot_manager.admit(bot_id) as lease, authority_guard(bot_id), SessionLocal() as db:
                captured_at, state = lease.bot.state_snapshot()
                pairs = (
                    db.query(UsageDeclaration, ResourceBinding)
                    .join(ResourceBinding, UsageDeclaration.binding_id == ResourceBinding.id)
                    .filter(UsageDeclaration.id.in_(ids))
                    .all()
                )
                for usage, binding in pairs:
                    if usage.holding_proof != holding_proof(
                        state, usage.resource_id, binding.external_user_id, now=captured_at.timestamp()
                    ):
                        db.delete(usage)
                        removed += 1
                db.commit()
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            # An unavailable bot is not proof that a holding disappeared. TTL/revocation
            # cleanup still applies; its live-state check will resume after recovery.
            continue
    return {"removed": removed, "cursor": max((row[0] for row in rows), default=0)}


async def usage_cleanup_loop():
    cursor = 0
    while True:
        await asyncio.sleep(60)
        try:
            result = await asyncio.to_thread(cleanup_usage_once, cursor)
            cursor = result["cursor"]
        except Exception:
            logger.exception("Task declaration cleanup failed; retrying next interval")
