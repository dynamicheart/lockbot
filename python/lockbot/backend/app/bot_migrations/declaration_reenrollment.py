"""Pointer-only declaration comparison: no note copying or destination authorization."""

from datetime import timezone

from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.models import BotMigration, MigrationPrincipalPair
from lockbot.backend.app.bot_migrations.principal_pairing import load_reviewed_links
from lockbot.backend.app.resource_api.usage import UsageDeclaration, declaration_revision, holding_proof


def declaration_reenrollment_plan(db, operation_id, source_state, mapped_state, *, actor_id, token_version, now):
    """Private super-admin comparison. Destination users must enroll and redeclare.

    Source detailed notes stay owned by the original binding and original expiry.
    Their text is never copied into this plan, a public summary or a new key scope.
    """
    links = load_reviewed_links(db, operation_id, actor_id=actor_id, token_version=token_version)
    pairs = db.query(MigrationPrincipalPair).filter_by(operation_id=operation_id, status="reviewed").all()
    mapped = {link.source_user_id: link.target_open_id for link in links}
    binding_owners = {pair.source_binding_id: pair.source_user_id for pair in pairs}
    if len(mapped) != len(links) or len(set(mapped.values())) != len(links):
        raise MigrationBlocked("reviewed_pairing_conflicts")
    pointers, skipped_expired, skipped_stale = [], 0, 0
    if not binding_owners:
        return {"items": [], "expired": 0, "stale": 0, "automatic_copy": False, "can_commit": False}
    rows = (
        db.query(UsageDeclaration)
        .filter(
            UsageDeclaration.bot_id == db.get(BotMigration, operation_id).bot_id,
            UsageDeclaration.binding_id.in_(binding_owners),
        )
        .all()
    )
    for row in rows:
        owner = binding_owners[row.binding_id]
        expiry = row.expires_at.replace(tzinfo=timezone.utc).timestamp()
        if expiry <= now:
            skipped_expired += 1
            continue
        source_proof = holding_proof(source_state, row.resource_id, owner, now=now)
        target_proof = holding_proof(mapped_state, row.resource_id, mapped[owner], now=now)
        if source_proof is None or source_proof != row.holding_proof or target_proof != source_proof:
            skipped_stale += 1
            continue
        pointers.append(
            {
                "source_declaration_id": row.id,
                "resource_id": row.resource_id,
                "source_revision": declaration_revision(row),
                "original_expires_at": expiry,
                "requires_destination_reenrollment": True,
                "automatic_copy": False,
            }
        )
    return {
        "items": pointers,
        "expired": skipped_expired,
        "stale": skipped_stale,
        "automatic_copy": False,
        "can_commit": False,
    }
