"""
AuditLog model — records who did what, when, and to which resource.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Operator info (denormalized so records survive user deletion)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operator_username: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_role: Mapped[str] = mapped_column(String(16), nullable=False)

    # Action: namespace.verb, e.g. "auth.login", "bot.start", "user.create"
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Target resource (optional)
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "user" | "bot"
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_name: Mapped[str | None] = mapped_column(String(128), nullable=True)  # denormalized

    # Extra context (JSON string)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="success")  # success | failure

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
