"""Persist an in-progress platform migration independently of the bot's runtime status."""

import hmac
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.bots.migration_preflight import _review_digest
from lockbot.backend.app.database import Base
from lockbot.backend.app.resource_api.lease_contract import MAX_STATE_FILE_BYTES


class BotMigration(Base):
    __tablename__ = "bot_migrations"

    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(32), unique=True)
    bot_uuid: Mapped[str] = mapped_column(String(36))
    source_revision: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    target_application_id: Mapped[int] = mapped_column(Integer, index=True)
    target_chat_id: Mapped[str] = mapped_column(String(128))
    review_digest: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime)
    phase: Mapped[str] = mapped_column(String(24), default="frozen")
    drained_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    destination_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_identity_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_authority_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


def migration_fence(db, bot_id):
    """A journal row fences manual transitions and process startup until reconciliation."""
    row = db.get(BotMigration, bot_id)
    return row if row is not None and row.phase not in {"completed", "cancelled"} else None


def record_migration_freeze(db, bot, target, review_digest, *, requested_by=None):
    """Stage a durable fence. The transition owner commits it before beginning retirement."""
    row = BotMigration(
        bot_id=bot.id,
        operation_id=uuid4().hex,
        bot_uuid=bot.uuid,
        source_revision=bot.identity_revision,
        requested_by=requested_by,
        target_application_id=target.application_id,
        target_chat_id=target.chat_id,
        review_digest=review_digest,
        frozen_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    previous = db.get(BotMigration, bot.id)
    if previous is not None and previous.phase == "cancelled":
        db.delete(previous)
        db.flush()
    db.add(row)
    return row


def _destination_scope(row, payload):
    return {
        "operation": row.operation_id,
        "bot": row.bot_uuid,
        "source_revision": row.source_revision,
        "application": row.target_application_id,
        "chat": row.target_chat_id,
        "review": row.review_digest,
        "target_identity": row.target_identity_digest,
        "source_authority": row.source_authority_digest,
        "payload": payload,
    }


def stage_destination_checkpoint(row, state):
    """Store only the clean destination runtime in the same transaction as the phase."""
    payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
    if len(payload.encode()) > MAX_STATE_FILE_BYTES:
        raise ValueError("Destination state exceeds the persisted state limit")
    row.destination_payload = payload
    row.destination_digest = _review_digest(_destination_scope(row, payload))


def read_destination_checkpoint(row):
    """Validate the persisted checkpoint before a later operation can publish it."""
    payload = row.destination_payload
    if row.phase not in {"drained", "switched", "installed"} or not payload or not row.destination_digest:
        raise ValueError("Destination checkpoint is unavailable")
    if len(payload.encode()) > MAX_STATE_FILE_BYTES or not hmac.compare_digest(
        row.destination_digest, _review_digest(_destination_scope(row, payload))
    ):
        raise ValueError("Destination checkpoint does not match the migration")
    return json.loads(payload)
