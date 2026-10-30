"""Signed delivery validator with an atomic source-to-target epoch handoff.

No provider calls or route changes happen here. A current signed target-epoch
nonce is still required after the switch; the durable handoff never substitutes
for fresh delivery or grants any user ownership.
"""

from lockbot.backend.app.bot_migrations import target_proof
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.models import (
    BotMigration,
    MigrationDeliveryHandoff,
    MigrationProofEpoch,
    MigrationTargetProof,
)


class SignedDeliveryValidator:
    def __init__(self, operation_id, *, actor_id, token_version):
        self.operation_id = operation_id
        self.actor_id = actor_id
        self.token_version = token_version

    def _latest(self, db, operation, stage):
        target_proof._reviewer(db, self.actor_id, self.token_version)
        rows = (
            db.query(MigrationTargetProof)
            .join(MigrationProofEpoch, MigrationProofEpoch.proof_id == MigrationTargetProof.id)
            .filter(
                MigrationTargetProof.operation_id == operation.id,
                MigrationTargetProof.status == "consumed",
                MigrationTargetProof.reviewer_id == self.actor_id,
                MigrationTargetProof.reviewer_token_version == self.token_version,
                MigrationProofEpoch.stage == stage,
            )
            .order_by(MigrationTargetProof.created_at.desc(), MigrationTargetProof.id.desc())
            .limit(32)
            .all()
        )
        for row in rows:
            try:
                target_proof._receipt_scope(db, row, target_proof._now())
            except MigrationBlocked:
                continue
            return row
        raise MigrationBlocked(
            "fresh_target_epoch_proof_required" if stage == "target" else "fresh_source_epoch_proof_required"
        )

    def __call__(self, db, app, chat_id):
        operation = db.get(BotMigration, self.operation_id)
        if operation is None or (operation.target_application_id, operation.target_chat_id) != (app.id, chat_id):
            raise MigrationBlocked("migration_proof_scope_changed")
        stage = "target" if operation.phase in {"switched", "activating"} else "source"
        self._latest(db, operation, stage)
        return True

    def handoff(self, db, operation, bot):
        """Must run inside the same transaction as revision/route/key invalidation."""
        if operation.id != self.operation_id or operation.phase != "checkpointed":
            raise MigrationBlocked("migration_handoff_scope_changed")
        row = self._latest(db, operation, "source")
        if db.get(MigrationDeliveryHandoff, operation.id) is not None:
            raise MigrationBlocked("migration_handoff_conflict")
        db.add(
            MigrationDeliveryHandoff(
                operation_id=operation.id,
                source_proof_id=row.id,
                source_digest=operation.source_digest,
                target_digest=operation.target_digest,
                target_revision=bot.identity_revision + 1,
                created_at=target_proof._now(),
            )
        )
