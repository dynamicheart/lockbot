from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class FeishuApplication(Base):
    __tablename__ = "feishu_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    tenant_key: Mapped[str] = mapped_column(String(128))
    bot_open_id: Mapped[str] = mapped_column(String(128))
    app_secret: Mapped[str] = mapped_column(Text)
    verification_token: Mapped[str] = mapped_column(Text)
    encrypt_key: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(16), default="http")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FeishuGroupRoute(Base):
    __tablename__ = "feishu_group_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(Integer, ForeignKey("feishu_applications.id"))
    chat_id: Mapped[str] = mapped_column(String(128))
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), unique=True)
    __table_args__ = (UniqueConstraint("application_id", "chat_id", name="uq_feishu_application_chat"),)


class FeishuReceivedMessage(Base):
    """Minimal durable claim; never persist raw chat text or binding proof codes."""

    __tablename__ = "feishu_received_messages"

    application_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer)
    identity_revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class FeishuMembershipPolicy(Base):
    """Absent rows are disabled; adding this table does not alter legacy app columns."""

    __tablename__ = "feishu_membership_policies"
    application_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unbound_greeting_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FeishuMembershipEvent(Base):
    """One-day replay claims. Never retain operator/contact or raw event fields."""

    __tablename__ = "feishu_membership_events"
    application_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class FeishuGroupMembership(Base):
    """Latest verified observation; it never authorizes or removes a group route."""

    __tablename__ = "feishu_group_memberships"
    application_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(128))
    tenant_key: Mapped[str] = mapped_column(String(128))
    bot_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    identity_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    event_time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    event_id: Mapped[str] = mapped_column(String(128))
