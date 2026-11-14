"""Persist an in-progress platform migration independently of the bot's runtime status."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class BotMigration(Base):
    __tablename__ = "bot_migrations"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(32), unique=True)
    bot_uuid: Mapped[str] = mapped_column(String(36))
    source_revision: Mapped[int] = mapped_column(Integer)
    target_application_id: Mapped[int] = mapped_column(ForeignKey("feishu_applications.id"))
    target_chat_id: Mapped[str] = mapped_column(String(128))
    review_digest: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime)


def migration_fence(db, bot_id):
    """A journal row fences manual transitions and process startup until reconciliation."""
    return db.get(BotMigration, bot_id)


def record_migration_freeze(db, bot, target, review_digest):
    """Stage a durable fence. The transition owner commits it before beginning retirement."""
    row = BotMigration(
        bot_id=bot.id,
        operation_id=uuid4().hex,
        bot_uuid=bot.uuid,
        source_revision=bot.identity_revision,
        target_application_id=target.application_id,
        target_chat_id=target.chat_id,
        review_digest=review_digest,
        frozen_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    return row
