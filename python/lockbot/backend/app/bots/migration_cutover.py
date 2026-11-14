"""Retire and recheck a reviewed source before destructive migration work."""

import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.manager import BotNotRunning, MayHaveExecuted
from lockbot.backend.app.bots.migration_journal import (
    BotMigration,
    migration_fence,
    read_destination_checkpoint,
    record_migration_freeze,
    stage_destination_checkpoint,
)
from lockbot.backend.app.bots.migration_preflight import (
    MigrationPreflight,
    _empty_destination_state,
    _reset_authorizations,
    _review_digest,
    destination_identity_digest,
    observe_migration,
    verify_migration_review_receipt,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.resource_api.lease_contract import MAX_STATE_FILE_BYTES
from lockbot.backend.app.resource_api.lease_history import LeaseJournal
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot
from lockbot.core.config import Config
from lockbot.core.io import _save_bot_state_atomically


def freeze_migration(db, manager, bot_id, review, actor, *, verify_before_freeze=None):
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
        bot = db.get(Bot, bot_id)
        if verify_before_freeze is not None:
            verify_before_freeze(bot, db.get(FeishuApplication, target.application_id), target.chat_id)
        row = record_migration_freeze(db, bot, target, observed["review_digest"], requested_by=current_actor.id)
        operation_id = row.operation_id
        db.commit()
        return continue_source_drain(db, manager, bot_id, operation_id, current_actor)


def continue_source_drain(db, manager, bot_id, operation_id, actor, *, build_config=None):
    """Continue the same operation after a timed-out drain without replaying admitted work."""
    with manager.transition(bot_id):
        db.rollback()
        if db.scalar(select(User.role).where(User.id == actor.id)) != "super_admin":
            raise HTTPException(403, "Super admin required")
        row = migration_fence(db, bot_id)
        if row is None or row.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if row.phase not in {"frozen", "recovery_required"}:
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
                try:
                    retired = _recover_retired_source(bot, db, build_config)
                except (OSError, ValueError):
                    row.phase = "recovery_required"
                    db.commit()
                    return row
            if retired is not None:
                target = MigrationPreflight(application_id=row.target_application_id, chat_id=row.target_chat_id)
                checkpoint = {}
                observed = observe_migration(db, manager, bot_id, target, retired=retired, checkpoint=checkpoint)
                row = migration_fence(db, bot_id)
                row.phase = "source_changed"
                if observed["review_digest"] == row.review_digest:
                    _, state = retired.bot.state_snapshot()
                    destination = _empty_destination_state(state, bot.bot_type, json.loads(bot.cluster_configs))
                    row.target_identity_digest = checkpoint["target_identity_digest"]
                    row.source_authority_digest = checkpoint["source_authority_digest"]
                    try:
                        observer = getattr(retired.bot.config, "_lifecycle_observer", None)
                        if observer is not None:
                            observer.outbox.flush()
                        stage_destination_checkpoint(row, destination)
                    except (OSError, ValueError):
                        row.phase = "recovery_required"
                    else:
                        row.phase = "drained"
                row.drained_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return row


def _read_migration_state(bot_id):
    path = get_managed_paths().state_file(bot_id)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_FILE_BYTES:
            raise ValueError("Source state is not bounded private regular storage")
        payload = json.load(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("bot_state"), dict):
        raise ValueError("Source state is unavailable")
    return payload


def _recover_retired_source(bot, db, build_config):
    """Read the canonical file strictly and settle any old-revision outbox."""
    if build_config is None:
        raise ValueError("Source recovery configuration unavailable")
    payload = _read_migration_state(bot.id)
    state = payload["bot_state"]
    config = Config(build_config(bot, db))
    recovered = SimpleNamespace(
        _lock=threading.Lock(),
        adapter=SimpleNamespace(platform_name=bot.platform),
        config=config,
        state=SimpleNamespace(bot_state=state),
        state_snapshot=lambda: (datetime.now(timezone.utc), state),
    )
    if "lifecycle_outbox" in payload:
        journal = LeaseJournal(bot.id, recovered, recover_only=True)
        if not journal.outbox.recovered:
            raise ValueError("Source lifecycle evidence does not match the saved state")
        journal.outbox.flush()
    return SimpleNamespace(bot=recovered, generation=None, health=None)


def switch_drained_migration(db, manager, bot_id, operation_id, actor, *, verify_destination):
    """Commit the destination route and authorization reset with the runtime still fenced.

    The supplied verifier checks live Feishu identity and deployment-specific metrics expiry policy.
    """
    with manager.transition(bot_id), authority_guard(bot_id):
        db.rollback()
        operator = db.get(User, actor.id)
        if operator is None or operator.role != "super_admin":
            raise HTTPException(403, "Super admin required")
        migration = db.get(BotMigration, bot_id)
        if migration is None or migration.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if migration.phase in {"switched", "installed", "completed"}:
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
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _, revisions = _reset_authorizations(db, bot, now)
        if _review_digest(revisions) != migration.source_authority_digest:
            migration.phase = "source_changed"
            db.commit()
            raise HTTPException(409, "Source authorizations changed after the drain")
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


def activate_switched_migration(db, manager, bot_id, operation_id, actor, *, build_config, verify_destination):
    """Install the reviewed idle state once, then stage and activate the Feishu generation."""
    with manager.transition(bot_id):
        db.rollback()
        if db.scalar(select(User.role).where(User.id == actor.id)) != "super_admin":
            raise HTTPException(403, "Super admin required")
        migration = db.get(BotMigration, bot_id)
        if migration is None or migration.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if migration.phase == "completed":
            return migration
        if migration.phase not in {"switched", "installed"}:
            raise HTTPException(409, "The destination route has not been switched")
        bot = db.get(Bot, bot_id)
        route = db.scalar(select(FeishuGroupRoute).where(FeishuGroupRoute.bot_id == bot_id))
        if (
            bot is None
            or bot.is_deleted
            or (bot.uuid, bot.identity_revision) != (migration.bot_uuid, migration.source_revision + 1)
            or str(bot.platform).lower() != "feishu"
            or bot.group_id != migration.target_chat_id
            or route is None
            or (route.application_id, route.chat_id) != (migration.target_application_id, migration.target_chat_id)
        ):
            raise HTTPException(409, "The Feishu route changed since switch")

        application = db.get(FeishuApplication, migration.target_application_id)
        membership = db.get(FeishuGroupMembership, (migration.target_application_id, migration.target_chat_id))
        if (
            application is None
            or membership is None
            or destination_identity_digest(application, membership) != migration.target_identity_digest
        ):
            raise HTTPException(409, "The reviewed Feishu destination changed")
        verify_destination(application, migration.target_chat_id, migration.drained_at)

        if migration.phase == "switched":
            if manager.get_instance(bot_id) is not None or manager.is_retiring(bot_id):
                raise HTTPException(409, "The previous bot generation has not retired")
            try:
                snapshot = read_destination_checkpoint(migration)
            except ValueError:
                raise HTTPException(409, "The migration checkpoint could not be verified") from None
            paths = get_managed_paths()
            state_config = Config({"BOT_ID": bot_id, "_MANAGED_BOT_DIR": lambda: paths.core_directory(bot_id)})
            _save_bot_state_atomically(snapshot, state_config)
            migration.phase = "installed"
            db.commit()

        # After installation, a retry must start from disk even if activation
        # already admitted work before its final database update failed.
        pid = manager.get_pid(bot_id)
        if pid is None:
            try:
                state = _read_migration_state(bot_id)["bot_state"]
                _empty_destination_state(state, bot.bot_type, json.loads(bot.cluster_configs))
            except (OSError, TypeError, ValueError):
                raise HTTPException(409, "The installed destination state could not be verified") from None
            pid = manager.start_bot(bot_id, build_config(bot, db), activate=False)
        bot.status = "running"
        bot.pid = pid
        db.commit()
        manager.activate_bot(bot_id)
        migration.phase = "completed"
        migration.destination_payload = None
        migration.destination_digest = None
        db.commit()
        return migration


def cancel_source_migration(db, manager, bot_id, operation_id, actor, *, build_config):
    """Resume the original writer only while the migration still owns the source revision."""
    with manager.transition(bot_id):
        db.rollback()
        operator = db.get(User, actor.id)
        if operator is None or operator.role != "super_admin":
            raise HTTPException(403, "Super admin required")
        migration = db.get(BotMigration, bot_id)
        if migration is None or migration.operation_id != operation_id:
            raise HTTPException(404, "Migration operation not found")
        if migration.phase == "cancelled":
            return migration
        if migration.phase not in {"frozen", "drained", "source_changed", "recovery_required", "cancelling"}:
            raise HTTPException(409, "The destination route has already been switched")
        bot = db.get(Bot, bot_id)
        route = db.scalar(select(FeishuGroupRoute.id).where(FeishuGroupRoute.bot_id == bot_id))
        if (
            bot is None
            or bot.is_deleted
            or str(bot.platform).lower() != "infoflow"
            or (bot.uuid, bot.identity_revision) != (migration.bot_uuid, migration.source_revision)
            or route is not None
        ):
            raise HTTPException(409, "The original source identity changed")

        if migration.phase != "cancelling" or manager.is_retiring(bot_id):
            try:
                manager.retire_bot(bot_id)
            except BotNotRunning:
                pass
            except MayHaveExecuted:
                raise HTTPException(409, "The source is still draining. Retry cancellation.") from None
            migration.phase = "cancelling"
            db.commit()

        pid = manager.get_pid(bot_id)
        if pid is None:
            # A normal start can fall back to idle if its state is invalid.
            try:
                _recover_retired_source(bot, db, build_config)
            except (OSError, ValueError):
                raise HTTPException(409, "The saved source state could not be verified") from None
            pid = manager.start_bot(bot_id, build_config(bot, db), activate=False)
        bot.status = "running"
        bot.pid = pid
        db.commit()
        manager.activate_bot(bot_id)
        migration.phase = "cancelled"
        migration.destination_payload = None
        migration.destination_digest = None
        write_audit_log(
            db,
            operator,
            "bot.migration_cancel",
            target_type="bot",
            target_id=bot_id,
            detail={"operation_id": operation_id},
        )
        db.commit()
        return migration
