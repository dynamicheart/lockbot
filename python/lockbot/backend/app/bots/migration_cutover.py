"""Retire and recheck a reviewed source before destructive migration work."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.manager import BotNotRunning, MayHaveExecuted
from lockbot.backend.app.bots.migration_journal import (
    migration_fence,
    read_destination_checkpoint,
    record_migration_freeze,
    stage_destination_checkpoint,
)
from lockbot.backend.app.bots.migration_preflight import (
    MigrationPreflight,
    _empty_destination_state,
    destination_identity_digest,
    observe_migration,
    verify_migration_review_receipt,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot


def freeze_migration(db, manager, bot_id, review, actor):
    """Persist the selected scope, then stop admission and drain the exact source generation."""
    with manager.transition(bot_id):
        db.rollback()
        current_actor = db.get(User, actor.id)
        if current_actor is None or current_actor.role != "super_admin":
            raise HTTPException(403, "Super admin required")
        if migration_fence(db, bot_id) is not None:
            raise HTTPException(409, "Use the existing migration operation")
        target = MigrationPreflight(application_id=review.application_id, chat_id=review.chat_id)
        observed = observe_migration(db, manager, bot_id, target)
        if not verify_migration_review_receipt(review.review_receipt, current_actor, observed):
            raise HTTPException(409, "The migration review changed or expired. Open a new preflight.")
        row = record_migration_freeze(
            db, db.get(Bot, bot_id), target, observed["review_digest"], requested_by=current_actor.id
        )
        operation_id = row.operation_id
        db.commit()
        return continue_source_drain(db, manager, bot_id, operation_id, current_actor)


def continue_source_drain(db, manager, bot_id, operation_id, actor):
    """Continue the same operation after a timed-out drain without replaying admitted work."""
    with manager.transition(bot_id):
        db.rollback()
        if db.scalar(select(User.role).where(User.id == actor.id)) != "super_admin":
            raise HTTPException(403, "Super admin required")
        row = migration_fence(db, bot_id)
        if row is None or row.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if row.phase != "frozen":
            return row
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted or (bot.uuid, bot.identity_revision) != (row.bot_uuid, row.source_revision):
            row.phase = "source_changed"
        else:
            try:
                retired = manager.retire_bot(bot_id)
            except MayHaveExecuted:
                return row
            except BotNotRunning:
                # A process restart has already closed admission. Reconcile from
                # durable state before authorizing any reset.
                row.phase = "recovery_required"
            else:
                target = MigrationPreflight(application_id=row.target_application_id, chat_id=row.target_chat_id)
                checkpoint = {}
                observed = observe_migration(db, manager, bot_id, target, retired=retired, checkpoint=checkpoint)
                row = migration_fence(db, bot_id)
                row.phase = "source_changed"
                if observed["review_digest"] == row.review_digest:
                    _, state = retired.bot.state_snapshot()
                    destination = _empty_destination_state(state, bot.bot_type, json.loads(bot.cluster_configs))
                    row.target_identity_digest = checkpoint["target_identity_digest"]
                    try:
                        stage_destination_checkpoint(row, destination)
                    except ValueError:
                        row.phase = "recovery_required"
                    else:
                        row.phase = "drained"
                row.drained_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return row


def switch_drained_migration(db, manager, bot_id, operation_id, actor, *, verify_destination):
    """Commit the destination route and authorization reset with the runtime still fenced.

    The supplied verifier checks live Feishu identity and deployment-specific metrics expiry policy.
    """
    with manager.transition(bot_id), authority_guard(bot_id):
        db.rollback()
        operator = db.get(User, actor.id)
        if operator is None or operator.role != "super_admin":
            raise HTTPException(403, "Super admin required")
        migration = migration_fence(db, bot_id)
        if migration is None or migration.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if migration.phase == "switched":
            return migration
        if migration.phase != "drained" or manager.get_instance(bot_id) is not None or manager.is_retiring(bot_id):
            raise HTTPException(409, "The migration source is not drained")
        try:
            read_destination_checkpoint(migration)
        except ValueError:
            raise HTTPException(409, "The migration checkpoint could not be verified") from None

        bot = db.get(Bot, bot_id)
        if (
            bot is None
            or bot.is_deleted
            or str(bot.platform).lower() != "infoflow"
            or (bot.uuid, bot.identity_revision) != (migration.bot_uuid, migration.source_revision)
        ):
            raise HTTPException(409, "The migration source identity changed")
        source = public_datasource(bot)
        if (
            source
            and source.get("accessMode") == "short_lived"
            and (
                migration.drained_at is None
                or datetime.now(timezone.utc).replace(tzinfo=None) < migration.drained_at + timedelta(seconds=315)
            )
        ):
            raise HTTPException(409, "Wait for the previous metrics grants to expire")
        application = db.get(FeishuApplication, migration.target_application_id)
        membership = db.get(FeishuGroupMembership, (migration.target_application_id, migration.target_chat_id))
        if (
            application is None
            or membership is None
            or membership.status != "present"
            or (membership.app_id, membership.tenant_key) != (application.app_id, application.tenant_key)
            or destination_identity_digest(application, membership) != migration.target_identity_digest
        ):
            raise HTTPException(409, "The reviewed Feishu destination changed")
        verify_destination(application, migration.target_chat_id, migration.drained_at)

        config = json.loads(bot.config_overrides or "{}")
        # Override any inherited source allowlist with an empty destination list.
        config["DURATION_WHITELIST"] = []
        revoked = revoke_bindings_for_bot(db, bot.id)
        db.execute(delete(UsageDeclaration).where(UsageDeclaration.bot_id == bot.id))
        bot.platform = "Feishu"
        bot.identity_revision += 1
        bot.group_id = migration.target_chat_id
        bot.observed_group_ids = bot.last_request_at = bot.last_user_id = None
        bot.binding_accept_after = datetime.now(timezone.utc).replace(tzinfo=None)
        bot.webhook_url = bot.aes_key = bot.token = encryption.encrypt("")
        bot.api_key = None
        bot.config_overrides = json.dumps(config, ensure_ascii=False)
        bot.status = "stopped"
        bot.pid = None
        bot.consecutive_failures = 0
        db.add(FeishuGroupRoute(application_id=application.id, chat_id=migration.target_chat_id, bot_id=bot.id))
        migration.phase = "switched"
        try:
            db.flush()
            write_audit_log(
                db,
                operator,
                "bot.migration_switch",
                target_type="bot",
                target_id=bot.id,
                target_name=bot.name,
                detail={"operation_id": operation_id, "revoked_agent_bindings": revoked},
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "The Feishu destination is already bound") from None
        return migration
