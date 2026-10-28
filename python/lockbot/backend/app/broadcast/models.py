from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class GroupActivity(Base):
    __tablename__ = "bot_group_activity"
    bot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    identity_revision: Mapped[int] = mapped_column(Integer)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class BroadcastJob(Base):
    __tablename__ = "broadcast_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    creator_id: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    mention_all: Mapped[bool] = mapped_column(Boolean, default=False)
    recent_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BroadcastTarget(Base):
    __tablename__ = "broadcast_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    bot_id: Mapped[int] = mapped_column(Integer)
    bot_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    identity_revision: Mapped[int] = mapped_column(Integer)
    bot_name: Mapped[str] = mapped_column(String(128))
    group_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
