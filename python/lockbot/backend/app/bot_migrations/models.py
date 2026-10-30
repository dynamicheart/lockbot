"""Migration journal references; no credentials, raw messages or identity proofs."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base

JOURNAL_VERSION = 1

TERMINAL = ("completed", "aborted")
FROZEN = ("freezing", "checkpointed", "switched", "activating", "aborting")


class BotMigration(Base):
    __tablename__ = "bot_migrations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=JOURNAL_VERSION, server_default="1", nullable=False)
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    last_actor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bot_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metrics_not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkpoint_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_file_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_bot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_state_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_bot_active_migration",
            "bot_id",
            unique=True,
            sqlite_where=text("phase NOT IN ('completed', 'aborted')"),
            postgresql_where=text("phase NOT IN ('completed', 'aborted')"),
        ),
    )


class MigrationTargetProof(Base):
    """Private nonce receipts; no nonce plaintext, message text or user task details."""

    __tablename__ = "bot_migration_target_proofs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(32), ForeignKey("bot_migrations.id"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    reviewer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_evidence_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    publication_valid_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MigrationPrincipalPair(Base):
    """Private account-link proofs; a link never grants destination resource authority."""

    __tablename__ = "bot_migration_principal_pairs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(32), ForeignKey("bot_migrations.id"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_binding_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_target_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_proof_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    target_proof_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer_token_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_migration_reviewed_source",
            "operation_id",
            "source_user_id",
            unique=True,
            sqlite_where=text("status = 'reviewed'"),
            postgresql_where=text("status = 'reviewed'"),
        ),
        Index(
            "uq_migration_reviewed_target",
            "operation_id",
            "expected_target_open_id",
            unique=True,
            sqlite_where=text("status = 'reviewed'"),
            postgresql_where=text("status = 'reviewed'"),
        ),
    )


class MigrationProofEpoch(Base):
    """Additive proof epoch marker; legacy receipts remain source-epoch only."""

    __tablename__ = "bot_migration_proof_epochs"
    proof_id: Mapped[str] = mapped_column(String(32), ForeignKey("bot_migration_target_proofs.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class MigrationDeliveryHandoff(Base):
    """Atomic identity-switch receipt, never reusable as fresh target delivery proof."""

    __tablename__ = "bot_migration_delivery_handoffs"
    operation_id: Mapped[str] = mapped_column(String(32), ForeignKey("bot_migrations.id"), primary_key=True)
    source_proof_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bot_migration_target_proofs.id"), nullable=False
    )
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
