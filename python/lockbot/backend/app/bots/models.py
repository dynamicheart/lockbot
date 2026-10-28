"""
Bot and BotLog models.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_type: Mapped[str] = mapped_column(String(16), nullable=False)  # NODE / DEVICE / QUEUE
    platform: Mapped[str] = mapped_column(String(16), default="Infoflow")
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    binding_accept_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_group_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Sensitive fields (Fernet encrypted)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    aes_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cluster_configs: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="stopped")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config_overrides: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    api_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Whether Agent API access and enrollment are enabled. Disabling suspends existing
    # credentials without revoking them. New bots default to enabled.
    resource_auth_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="1")
    resource_api_notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="1")

    # Shared non-secret configuration; metrics are still queried directly by browsers.
    utilization_config: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # One bot per uuid across the WHOLE table, tombstones included: the uuid is the stable
        # identity a restore matches on, so a duplicate (concurrent create, or a recreate over a
        # soft-deleted row) must be impossible, not merely unlikely. SQLite treats NULLs as
        # distinct, so pre-backfill rows with a NULL uuid do not collide.
        Index("ix_bots_uuid_unique", "uuid", unique=True),
        # Unique bot name per user
        {"sqlite_autoincrement": True},
    )


class BotLog(Base):
    __tablename__ = "bot_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="system")  # system | command
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
