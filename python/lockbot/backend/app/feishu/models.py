from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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


class FeishuQueryPanel(Base):
    """Only verified own-message references; no query text, notes or credentials."""

    __tablename__ = "feishu_query_panels"

    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(32), default="")
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
