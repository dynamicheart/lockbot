"""
Bot and BotLog models.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_type: Mapped[str] = mapped_column(String(16), nullable=False)  # NODE / DEVICE / QUEUE
    platform: Mapped[str] = mapped_column(String(16), default="Infoflow")
    group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Sensitive fields (Fernet encrypted)
    # credentials: unified encrypted JSON with platform-specific keys
    # webhook_url/token/aes_key: legacy columns, kept for migration compatibility
    credentials: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON, encrypted
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aes_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cluster_configs: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="stopped")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config_overrides: Mapped[str] = mapped_column(Text, default="{}")  # JSON

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
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
