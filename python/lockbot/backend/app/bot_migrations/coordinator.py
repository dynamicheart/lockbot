"""Draft empty-state cutover saga; no HTTP commit route is exposed yet.

A production target validator and clock-bound evidence are mandatory dependencies.
Failures retain the durable freeze; retries resume the same operation, never commands.
"""

import contextlib
import hashlib
import json
import math
import os
import stat
import tempfile
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations.models import FROZEN, JOURNAL_VERSION, BotMigration
from lockbot.backend.app.bots.manager import BotNotRunning
from lockbot.backend.app.bots.migration_preflight import MigrationPreflight, _counts, _inventory_matches
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.connectors import connectors
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.platform_applications.service import fingerprint
from lockbot.backend.app.resource_api.lease_contract import MAX_STATE_FILE_BYTES, contract_frame
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot


class MigrationBlocked(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_digest(bot):
    fields = (
        "uuid",
        "user_id",
        "bot_type",
        "platform",
        "identity_revision",
        "group_id",
        "name",
        "cluster_configs",
        "config_overrides",
        "utilization_config",
        "webhook_url",
        "token",
        "aes_key",
        "resource_auth_enabled",
        "resource_api_notify_enabled",
        "api_key",
    )
    return digest({field: getattr(bot, field) for field in fields})


def target_digest(app, chat):
    return digest(
        [app.id, app.app_id, app.tenant_key, app.bot_open_id, app.transport, fingerprint(app, "feishu"), chat]
    )


def read_state(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_FILE_BYTES:
            raise MigrationBlocked("state_storage_unverified")
        data = source.read(MAX_STATE_FILE_BYTES + 1)
    if len(data) > MAX_STATE_FILE_BYTES:
        raise MigrationBlocked("state_storage_unverified")
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("bot_state"), dict):
        raise MigrationBlocked("state_storage_unverified")
    return data, value


def empty_state(state, bot):
    try:
        counts = _counts(state, bot.bot_type)
    except (TypeError, ValueError):
        raise MigrationBlocked("source_state_unrecognized") from None
    if not _inventory_matches(state, bot):
        raise MigrationBlocked("source_inventory_changed")
    if any(counts[field] for field in ("holders", "reservations", "offers", "non_idle")):
        raise MigrationBlocked("source_has_outstanding_allocations")


def _write_checkpoint(path, data):
    """Publish a fully fsynced private checkpoint under the held per-bot transition owner."""
    if path.exists():
        existing, _ = read_state(path)
        if existing != data:
            raise MigrationBlocked("checkpoint_conflict")
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".migration-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        # Transition ownership + the deployment state-store owner exclude another publisher.
        os.rename(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class CutoverCoordinator:
    def __init__(
        self,
        factory,
        manager,
        *,
        clock,
        target_validator=None,
        metrics_validator=None,
        maximum_clock_offset=None,
        fault=None,
        switch_handoff=None,
    ):
        self.factory, self.manager, self.clock = factory, manager, clock
        self.target_validator, self.maximum_clock_offset = target_validator, maximum_clock_offset
        self.metrics_validator = metrics_validator
        self.switch_handoff = switch_handoff
        self.fault = fault or (lambda phase: None)

    def _now(self):
        value = self.clock()
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise MigrationBlocked("clock_unverified")
        return datetime.utcfromtimestamp(value)

    def _target(self, db, operation, *, live=False):
        app = db.get(FeishuApplication, operation.target_application_id)
        if app is None or not app.tenant_key or not app.bot_open_id:
            raise MigrationBlocked("target_application_identity_unknown")
        if target_digest(app, operation.target_chat_id) != operation.target_digest:
            raise MigrationBlocked("target_changed")
        route = db.query(FeishuGroupRoute).filter_by(application_id=app.id, chat_id=operation.target_chat_id).first()
        if route is not None and (
            operation.phase not in ("switched", "activating", "completed") or route.bot_id != operation.bot_id
        ):
            raise MigrationBlocked("target_group_already_bound")
        if operation.phase in ("switched", "activating") and route is None:
            raise MigrationBlocked("target_route_changed")
        if live and (
            self.target_validator is None or self.target_validator(db, app, operation.target_chat_id) is not True
        ):
            raise MigrationBlocked("target_live_validation_unavailable")
        return app

    @staticmethod
    def _source(db, operation, *, switched=False):
        bot = db.get(Bot, operation.bot_id)
        if bot is None or bot.is_deleted or bot.uuid != operation.bot_uuid:
            raise MigrationBlocked("source_identity_changed")
        expected_revision = operation.source_revision + int(switched)
        expected_platform = "feishu" if switched else "infoflow"
        if bot.identity_revision != expected_revision or str(bot.platform).lower() != expected_platform:
            raise MigrationBlocked("source_identity_changed")
        if switched and (not operation.target_bot_digest or source_digest(bot) != operation.target_bot_digest):
            raise MigrationBlocked("target_configuration_changed")
        if not switched and source_digest(bot) != operation.source_digest:
            raise MigrationBlocked("source_configuration_changed")
        if not switched and db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first() is not None:
            raise MigrationBlocked("source_already_has_feishu_route")
        return bot

    def plan(self, bot_id, application_id, chat_id, *, actor_id, token_version, operation_id=None):
        body = MigrationPreflight(application_id=application_id, chat_id=chat_id)
        operation_id = operation_id or uuid.uuid4().hex
        if len(operation_id) != 32 or any(char not in "0123456789abcdef" for char in operation_id):
            raise MigrationBlocked("invalid_operation_id")
        with connectors.application_guard(), self.factory() as db:
            self._actor(db, actor_id, token_version)
            existing = db.get(BotMigration, operation_id)
            if existing:
                if existing.schema_version != JOURNAL_VERSION:
                    raise MigrationBlocked("journal_version_unsupported")
                if (existing.bot_id, existing.target_application_id, existing.target_chat_id) != (
                    bot_id,
                    body.application_id,
                    body.chat_id,
                ):
                    raise MigrationBlocked("operation_target_conflict")
                return self._result(existing)
            with self.manager.transition(bot_id):
                db.rollback()
                self._actor(db, actor_id, token_version)
                bot, app = db.get(Bot, bot_id), db.get(FeishuApplication, body.application_id)
                if bot is None or bot.is_deleted or not bot.uuid or str(bot.platform).lower() != "infoflow":
                    raise MigrationBlocked("source_identity_unsupported")
                if app is None:
                    raise MigrationBlocked("target_application_missing")
                if db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first() is not None:
                    raise MigrationBlocked("source_already_has_feishu_route")
                with self.manager.admit(bot_id, for_read=True) as lease, lease.bot._lock:
                    empty_state(lease.bot.state.bot_state, bot)
                    if (
                        lease.health != "running"
                        or lease.bot.config.get_val("_IDENTITY_REVISION", 0) != bot.identity_revision
                    ):
                        raise MigrationBlocked("source_generation_unverified")
                    if lease.bot.config.get_val("DURATION_WHITELIST", []):
                        raise MigrationBlocked("source_identity_rules_require_mapping")
                now = self._now()
                operation = BotMigration(
                    id=operation_id,
                    bot_id=bot.id,
                    bot_uuid=bot.uuid,
                    source_revision=bot.identity_revision,
                    created_by=actor_id,
                    last_actor_id=actor_id,
                    target_application_id=app.id,
                    target_chat_id=body.chat_id,
                    source_group_id=bot.group_id,
                    source_digest=source_digest(bot),
                    target_digest=target_digest(app, body.chat_id),
                    phase="planned",
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
                self._target(db, operation)
                self._actor(db, actor_id, token_version)
                db.add(operation)
                try:
                    db.commit()
                except IntegrityError:
                    raise MigrationBlocked("another_migration_is_active") from None
                return self._result(operation)

    def _checkpoint(self, db, operation, bot):
        from lockbot.backend.app.bots.router import _build_config_dict

        paths = get_managed_paths()
        data, body = read_state(paths.state_file(bot.id))
        empty_state(body["bot_state"], bot)
        envelope = body.get("lifecycle_outbox")
        if envelope is not None and (
            not isinstance(envelope, dict)
            or envelope.get("version") != 2
            or envelope.get("bot_uuid") != bot.uuid
            or envelope.get("identity_revision") != bot.identity_revision
            or envelope.get("bot_id") != bot.id
            or not isinstance(envelope.get("pending"), list)
            or any(contract_frame(frame) is None for frame in envelope["pending"])
        ):
            raise MigrationBlocked("source_outbox_unverified")
        instance = self.manager.get_instance(bot.id)
        if instance is None:
            # After a crash, reconstruct only a proven-empty source generation, still fenced and staged.
            self.manager.start_bot(bot.id, _build_config_dict(bot, db), activate=False)
            instance = self.manager.get_instance(bot.id)
        self.manager.stop_bot(bot.id)
        with instance.bot._lock:
            empty_state(instance.bot.state.bot_state, bot)
            journal = getattr(instance.config, "_lifecycle_observer", None)
            if journal is None or journal._prepared is not None:
                raise MigrationBlocked("source_outbox_unavailable")
            with journal.lock:
                journal.outbox.flush()
                if journal.outbox.pending or journal.gap not in (None, "session_start"):
                    raise MigrationBlocked("source_outbox_unsettled")
            # Compare the drained runtime state with the durable allocator state, without loading/migrating it.
            data, body = read_state(paths.state_file(bot.id))
            if body["bot_state"] != instance.bot.state.bot_state:
                raise MigrationBlocked("source_state_not_durable")
            with journal.lock:
                self._confirm_source_envelope(journal, body)
        checkpoint = paths.bot_file(bot.id, "migration-" + operation.id + ".json")
        checkpoint_data = data
        if checkpoint.exists():
            checkpoint_data, checkpoint_body = read_state(checkpoint)
            if checkpoint_body["bot_state"] != body["bot_state"]:
                raise MigrationBlocked("checkpoint_conflict")
            with journal.lock:
                self._confirm_source_envelope(journal, checkpoint_body)
        _write_checkpoint(checkpoint, checkpoint_data)
        self.fault("checkpoint_published")
        operation.checkpoint_digest = hashlib.sha256(checkpoint_data).hexdigest()
        operation.source_file_digest = hashlib.sha256(data).hexdigest()
        operation.metrics_not_before = self._now() + timedelta(
            seconds=(315 + self.maximum_clock_offset) if bot.utilization_config else 0
        )
        operation.phase = "checkpointed"
        bot.status, bot.pid = "stopped", None
        db.commit()
        self.fault("checkpoint_committed")

    @staticmethod
    def _confirm_source_envelope(journal, body):
        envelope = body.get("lifecycle_outbox")
        if envelope is None:
            return
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != 2
            or envelope.get("bot_id") != journal.bot_id
            or envelope.get("bot_uuid") != journal.bot.config.get_val("_LIFECYCLE_BOT_UUID", "")
            or envelope.get("identity_revision") != journal.bot.config.get_val("_IDENTITY_REVISION", 0)
            or not isinstance(envelope.get("pending"), list)
        ):
            raise MigrationBlocked("source_outbox_unverified")
        frames = [contract_frame(frame) for frame in envelope["pending"]]
        if not frames or any(frame is None for frame in frames):
            raise MigrationBlocked("source_outbox_unverified")
        for before, after in zip(frames, frames[1:], strict=False):
            if (
                before["contract"]["journal_id"] != after["contract"]["journal_id"]
                or before["contract"]["commit_sequence"] + 1 != after["contract"]["commit_sequence"]
                or before["after_digest"] != after["before_digest"]
            ):
                raise MigrationBlocked("source_outbox_unverified")
        last = frames[-1]
        if last["after_digest"] != journal._digest(journal._canonical(body["bot_state"])) or any(
            frame["bot_id"] != journal.bot_id or not frame["contract"]["atomic_state"] for frame in frames
        ):
            raise MigrationBlocked("source_outbox_unverified")
        previous = journal.outbox.pending
        known = set(journal.outbox.known)
        try:
            journal.outbox.pending = frames
            journal.outbox._read_known()
            if any(frame["contract"]["frame_id"] not in journal.outbox.known for frame in frames):
                raise MigrationBlocked("source_outbox_acknowledgement_missing")
        finally:
            journal.outbox.pending = previous
            journal.outbox.known = known

    def _checkpoint_body(self, operation):
        path = get_managed_paths().bot_file(operation.bot_id, "migration-" + operation.id + ".json")
        data, body = read_state(path)
        if hashlib.sha256(data).hexdigest() != operation.checkpoint_digest:
            raise MigrationBlocked("checkpoint_changed")
        return body

    def _switch(self, db, operation, bot):
        from lockbot.backend.app.audit.delegation import _guard as audit_guard

        body = self._checkpoint_body(operation)
        empty_state(body["bot_state"], bot)
        current, _ = read_state(get_managed_paths().state_file(bot.id))
        if hashlib.sha256(current).hexdigest() != operation.source_file_digest:
            raise MigrationBlocked("source_state_changed_after_checkpoint")
        with audit_guard, authority_guard(bot.id):
            if self.switch_handoff is not None:
                self.switch_handoff(db, operation, bot)
            bot.platform, bot.group_id = "Feishu", operation.target_chat_id
            bot.identity_revision += 1
            bot.binding_accept_after = self._now()
            bot.observed_group_ids = bot.last_user_id = bot.last_request_at = None
            bot.api_key = None
            revoke_bindings_for_bot(db, bot.id)
            db.add(
                FeishuGroupRoute(
                    application_id=operation.target_application_id, chat_id=operation.target_chat_id, bot_id=bot.id
                )
            )
            operation.target_bot_digest = source_digest(bot)
            operation.phase = "switched"
            db.commit()
        self.fault("identity_switched")

    def _activate(self, db, operation, bot, actor_id, token_version):
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.io import _save_bot_state_atomically

        expected = self._checkpoint_body(operation)["bot_state"]
        _, present = read_state(get_managed_paths().state_file(bot.id))
        if present["bot_state"] != expected:
            raise MigrationBlocked("target_state_changed")
        empty_state(present["bot_state"], bot)
        instance = self.manager.get_instance(bot.id)
        if instance is not None:
            self.manager.stop_bot(bot.id)
        self.manager.start_bot(bot.id, _build_config_dict(bot, db), activate=False)
        instance = self.manager.get_instance(bot.id)
        with instance.bot._lock:
            if instance.bot.state.bot_state != expected:
                raise MigrationBlocked("target_state_changed")
            empty_state(instance.bot.state.bot_state, bot)
            journal = getattr(instance.config, "_lifecycle_observer", None)
            if journal is None:
                raise MigrationBlocked("target_lifecycle_unavailable")
            evidence = journal.prepare_state_write(instance.bot.state.bot_state)
            _save_bot_state_atomically(instance.bot.state.bot_state, instance.config, evidence=evidence)
            journal.committed(instance.bot.state.bot_state)
            if journal.gap is not None or journal.outbox.pending:
                raise MigrationBlocked("target_baseline_unsettled")
            raw, _ = read_state(get_managed_paths().state_file(bot.id))
            operation.target_state_digest = hashlib.sha256(raw).hexdigest()
        self._actor(db, actor_id, token_version)
        operation.phase = "activating"
        bot.status, bot.pid = "running", self.manager.get_pid(bot.id)
        db.commit()
        self.fault("target_staged")
        self._actor(db, actor_id, token_version)
        self.manager.activate_bot(bot.id)
        self.fault("target_activated")
        self._actor(db, actor_id, token_version)
        operation.phase, operation.completed_at, operation.last_error = "completed", self._now(), None
        db.commit()
        self.fault("completion_committed")
        self.manager.clear_migration_fence(bot.id, operation.id)

    def _restore_source(self, db, operation, bot, actor_id, token_version):
        from lockbot.backend.app.bots.router import _build_config_dict

        if db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first() is not None:
            raise MigrationBlocked("source_rollback_target_may_be_bound")
        with contextlib.suppress(BotNotRunning):
            self.manager.stop_bot(bot.id)
        _, body = read_state(get_managed_paths().state_file(bot.id))
        if not _inventory_matches(body["bot_state"], bot):
            raise MigrationBlocked("source_inventory_changed")
        _counts(body["bot_state"], bot.bot_type)
        self.manager.start_bot(bot.id, _build_config_dict(bot, db), activate=False)
        instance = self.manager.get_instance(bot.id)
        with instance.bot._lock:
            if instance.bot.state.bot_state != body["bot_state"]:
                self.manager.stop_bot(bot.id)
                raise MigrationBlocked("source_rollback_state_changed")
        self._actor(db, actor_id, token_version)
        self.manager.activate_bot(bot.id)
        self._actor(db, actor_id, token_version)
        bot.status, bot.pid = "running", self.manager.get_pid(bot.id)
        operation.phase, operation.completed_at, operation.last_error = "aborted", self._now(), None
        db.commit()
        self.manager.clear_migration_fence(bot.id, operation.id)

    def abort(self, operation_id, *, actor_id, token_version, expected_phase=None):
        """Only a source that never switched can be reopened; never restore an old snapshot."""
        with connectors.application_guard(), self.factory() as db:
            self._actor(db, actor_id, token_version)
            operation = db.get(BotMigration, operation_id)
            if operation is None:
                raise MigrationBlocked("operation_not_found")
            if operation.schema_version != JOURNAL_VERSION:
                raise MigrationBlocked("journal_version_unsupported")
            if expected_phase is not None and operation.phase != expected_phase:
                raise MigrationBlocked("migration_phase_changed")
            if operation.phase in ("switched", "activating", "completed"):
                raise MigrationBlocked("reverse_migration_requires_separate_confirmation")
            if operation.phase == "aborted":
                if self.manager.migration_operation_id(operation.bot_id) == operation.id:
                    self._source(db, operation)
                    self.manager.clear_migration_fence(operation.bot_id, operation.id)
                return self._result(operation)
            if operation.phase == "planned":
                with self.manager.transition(operation.bot_id):
                    self._actor(db, actor_id, token_version)
                    operation.phase, operation.completed_at = "aborted", self._now()
                    operation.last_actor_id = actor_id
                    db.commit()
                    return self._result(operation)
            self.manager.install_migration_fence(operation.bot_id, operation.id)
            with (
                self.manager.migration_operation(operation.bot_id, operation.id),
                self.manager.transition(operation.bot_id),
            ):
                bot = self._source(db, operation)
                operation.phase, operation.last_actor_id = "aborting", actor_id
                db.commit()
                self._restore_source(db, operation, bot, actor_id, token_version)
                return self._result(operation)

    def resume(self, operation_id, *, actor_id, token_version, expected_phase=None):
        # Import lazily: the regular audit API never depends on the migration executor.
        from lockbot.backend.app.audit.delegation import _guard as audit_guard

        with connectors.application_guard(), self.factory() as db:
            self._actor(db, actor_id, token_version)
            operation = db.get(BotMigration, operation_id)
            if operation is None:
                raise MigrationBlocked("operation_not_found")
            if operation.schema_version != JOURNAL_VERSION:
                raise MigrationBlocked("journal_version_unsupported")
            if expected_phase is not None and operation.phase != expected_phase:
                raise MigrationBlocked("migration_phase_changed")
            if operation.phase in ("completed", "aborted"):
                if self.manager.migration_operation_id(operation.bot_id) == operation.id:
                    self._source(db, operation, switched=operation.phase == "completed")
                    with contextlib.suppress(Exception):
                        self.manager.clear_migration_fence(operation.bot_id, operation.id)
                return self._result(operation)
            if operation.phase in FROZEN:
                self.manager.install_migration_fence(operation.bot_id, operation.id)
            was_frozen = operation.phase in FROZEN
            scope = (
                self.manager.migration_operation(operation.bot_id, operation.id)
                if was_frozen
                else contextlib.nullcontext()
            )
            try:
                with scope, self.manager.transition(operation.bot_id):
                    if operation.phase == "planned":
                        if operation.expires_at <= self._now():
                            raise MigrationBlocked("plan_expired")
                        bot = self._source(db, operation)
                        if bot.utilization_config:
                            if (
                                self.maximum_clock_offset is None
                                or type(self.maximum_clock_offset) not in (int, float)
                                or not math.isfinite(self.maximum_clock_offset)
                                or not 0 <= self.maximum_clock_offset <= 300
                            ):
                                raise MigrationBlocked("clock_bound_unverified")
                            try:
                                source = json.loads(bot.utilization_config)
                            except (ValueError, TypeError):
                                raise MigrationBlocked("metrics_authority_unverified") from None
                            if not isinstance(source, dict) or source.get("accessMode") != "short_lived":
                                raise MigrationBlocked("independent_metrics_authority_requires_review")
                            if self.metrics_validator is None or self.metrics_validator(db, bot, source) is not True:
                                raise MigrationBlocked("metrics_authority_unverified")
                        self._target(db, operation, live=True)
                        with self.manager.admit(bot.id, for_read=True) as lease, lease.bot._lock:
                            empty_state(lease.bot.state.bot_state, bot)
                            if lease.bot.config.get_val("DURATION_WHITELIST", []):
                                raise MigrationBlocked("source_identity_rules_require_mapping")
                        with audit_guard, authority_guard(bot.id):
                            self._actor(db, actor_id, token_version)
                            operation.last_actor_id = actor_id
                            operation.phase = "freezing"
                            db.commit()
                            self.manager.install_migration_fence(bot.id, operation.id)
                        self.fault("freeze_committed")
                    with (
                        self.manager.migration_operation(operation.bot_id, operation.id)
                        if not was_frozen and operation.phase == "freezing"
                        else contextlib.nullcontext()
                    ):
                        bot = self._source(db, operation, switched=operation.phase in ("switched", "activating"))
                        if operation.phase == "aborting":
                            self._restore_source(db, operation, bot, actor_id, token_version)
                        if operation.phase == "freezing":
                            self._checkpoint(db, operation, bot)
                        if operation.phase == "checkpointed":
                            if operation.metrics_not_before is None or self._now() < operation.metrics_not_before:
                                return self._result(operation)
                            self._target(db, operation, live=True)
                            self._actor(db, actor_id, token_version)
                            operation.last_actor_id = actor_id
                            self._switch(db, operation, bot)
                        if operation.phase in ("switched", "activating"):
                            self._target(db, operation, live=True)
                            bot = self._source(db, operation, switched=True)
                            self._actor(db, actor_id, token_version)
                            operation.last_actor_id = actor_id
                            self._activate(db, operation, bot, actor_id, token_version)
                        return self._result(operation)
            except Exception as exc:
                db.rollback()
                current = db.get(BotMigration, operation_id)
                if current and current.phase not in ("completed", "aborted"):
                    current.last_error = exc.code if isinstance(exc, MigrationBlocked) else "migration_step_failed"
                    if current.phase != "planned":
                        self.manager.install_migration_fence(current.bot_id, current.id)
                    db.commit()
                raise

    @staticmethod
    def _actor(db, actor_id, token_version):
        if type(actor_id) is not int or type(token_version) is not int or token_version < 0:
            raise MigrationBlocked("current_super_administrator_required")
        actor = db.execute(
            select(User).where(User.id == actor_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            actor is None
            or actor.role != "super_admin"
            or actor.must_change_password
            or actor.effective_token_version != token_version
        ):
            raise MigrationBlocked("current_super_administrator_required")

    @staticmethod
    def _result(operation):
        return {
            "operation_id": operation.id,
            "bot_id": operation.bot_id,
            "phase": operation.phase,
            "metrics_not_before": operation.metrics_not_before.isoformat() + "Z"
            if operation.metrics_not_before
            else None,
            "last_error": operation.last_error,
            "commands_replayed": False,
            "active_state_migration_supported": False,
        }
