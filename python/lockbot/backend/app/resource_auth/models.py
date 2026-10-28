"""Durable confirmed-binding model.

This table describes a **confirmed** binding and nothing else. There is deliberately
no bind-code digest, no bind deadline and no pending/unconfirmed state here: an
unconfirmed binding lives only in the in-process pending store
(:mod:`lockbot.backend.app.resource_auth.pending`) and is never written to the database,
so an unauthenticated caller cannot create rows.

Only the digest of the credential is stored. The plaintext credential is returned exactly
once, in the binding-creation response, and is not recoverable afterwards.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base


class ResourceBinding(Base):
    """A verified IM identity authorized to drive one bot through the HTTP API."""

    __tablename__ = "resource_bindings"
    __table_args__ = (
        Index(
            "uq_resource_binding_live_principal_bot",
            "platform",
            "external_user_id",
            "bound_bot_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Unguessable identifier handed to clients. Never the primary key, so a row id
    #: cannot be probed or guessed from outside.
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    #: Opaque normalized platform name (e.g. "infoflow"). This layer never interprets it.
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Opaque platform-native user id, taken from a signature-verified decrypted callback.
    external_user_id: Mapped[str] = mapped_column(String(256), nullable=False)

    #: The single bot this credential is scoped to.
    bound_bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    #: Snapshot of that bot's stable uuid at confirmation time. Checked on every request so
    #: that reuse of a database row id cannot transfer this credential to a different bot.
    bound_identity_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bound_bot_uuid: Mapped[str] = mapped_column(String(36), nullable=False)

    #: Hex sha256 of the credential. The plaintext is never stored.
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Agent-supplied free-text label, shown by the chat `bindings` command.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    #: Nullable and unset by default: a credential lives until it is explicitly revoked.
    #: A deployment may configure a bounded lifetime, and when set it is enforced.
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
