"""Retire and recheck a reviewed source before destructive migration work."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.manager import BotNotRunning, MayHaveExecuted
from lockbot.backend.app.bots.migration_journal import migration_fence, record_migration_freeze
from lockbot.backend.app.bots.migration_preflight import (
    MigrationPreflight,
    observe_migration,
    verify_migration_review_receipt,
)
from lockbot.backend.app.bots.models import Bot


def freeze_migration(db, manager, bot_id, review, actor):
    """Persist the selected scope, then stop admission and drain the exact source generation."""
    with manager.transition(bot_id):
        db.rollback()
        current_actor = db.get(User, actor.id)
        if current_actor is None or current_actor.role != "super_admin":
            raise HTTPException(403, "Super admin required")
        if migration_fence(db, bot_id) is not None:
            raise HTTPException(409, "Use the existing migration operation")
        target = MigrationPreflight(application_id=review.application_id, chat_id=review.chat_id)
        observed = observe_migration(db, manager, bot_id, target)
        if not verify_migration_review_receipt(review.review_receipt, current_actor, observed):
            raise HTTPException(409, "The migration review changed or expired. Open a new preflight.")
        row = record_migration_freeze(
            db, db.get(Bot, bot_id), target, observed["review_digest"], requested_by=current_actor.id
        )
        operation_id = row.operation_id
        db.commit()
        return continue_source_drain(db, manager, bot_id, operation_id, current_actor)


def continue_source_drain(db, manager, bot_id, operation_id, actor):
    """Continue the same operation after a timed-out drain without replaying admitted work."""
    with manager.transition(bot_id):
        db.rollback()
        if db.scalar(select(User.role).where(User.id == actor.id)) != "super_admin":
            raise HTTPException(403, "Super admin required")
        row = migration_fence(db, bot_id)
        if row is None or row.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if row.phase != "frozen":
            return row
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted or (bot.uuid, bot.identity_revision) != (row.bot_uuid, row.source_revision):
            row.phase = "source_changed"
        else:
            try:
                retired = manager.retire_bot(bot_id)
            except MayHaveExecuted:
                return row
            except BotNotRunning:
                # A process restart has already closed admission. Reconcile from
                # durable state before authorizing any reset.
                row.phase = "recovery_required"
            else:
                target = MigrationPreflight(application_id=row.target_application_id, chat_id=row.target_chat_id)
                observed = observe_migration(db, manager, bot_id, target, retired=retired)
                row = migration_fence(db, bot_id)
                row.phase = "drained" if observed["review_digest"] == row.review_digest else "source_changed"
                row.drained_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return row
