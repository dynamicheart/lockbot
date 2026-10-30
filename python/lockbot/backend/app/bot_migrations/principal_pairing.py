"""Disabled account-pairing service; source and target prove exact scoped principals.

No HTTP endpoints expose this workflow and it never transfers keys or allocations.
"""

import re
import secrets
import uuid
from datetime import timedelta, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from lockbot.backend.app.bot_migrations.active_state import MappingScope, ReviewedLink
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.models import BotMigration, MigrationPrincipalPair
from lockbot.backend.app.bot_migrations.target_proof import _hash, _now, _reviewer, _scope
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.connectors import connectors
from lockbot.backend.app.feishu.traffic import IngressBudget
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.resource_auth.service import authenticate
from lockbot.backend.app.settings.models import SiteSetting

_COMMAND = re.compile(r"^migration-pair ([A-Za-z0-9_-]{43})$")
_OPEN_ID = re.compile(r"^ou_[A-Za-z0-9_-]{1,125}$")
_budget = IngressBudget(app_limit=60, actor_limit=6)


def _binding(db, row, operation, now):
    bot = db.get(Bot, operation.bot_id)
    binding = db.get(ResourceBinding, row.source_binding_id)
    if (
        bot is None
        or not bot.resource_auth_enabled
        or binding is None
        or binding.revoked_at is not None
        or str(binding.platform).lower() != "infoflow"
        or binding.bound_bot_id != bot.id
        or binding.bound_bot_uuid != operation.bot_uuid
        or binding.bound_identity_revision != operation.source_revision
        or binding.external_user_id != row.source_user_id
        or binding.credential_digest != row.source_binding_digest
        or binding.credential_expires_at is not None
        and binding.credential_expires_at <= now
    ):
        raise MigrationBlocked("source_pairing_authority_unavailable")
    return binding


def _valid(db, row, now):
    operation = db.get(BotMigration, row.operation_id)
    app = _scope(db, operation, now)
    instance = db.get(SiteSetting, "resource_api_instance_id")
    if (
        instance is None
        or instance.value != row.instance_id
        or row.source_digest != operation.source_digest
        or row.target_digest != operation.target_digest
        or not row.created_at <= now < row.expires_at
    ):
        raise MigrationBlocked("pairing_scope_changed_or_expired")
    _binding(db, row, operation, now)
    return operation, app


def create_pairing(db, operation_id, *, source_key, expected_target_open_id):
    """Caller must supply the exact intended Open ID before exposing any nonce."""
    if not isinstance(expected_target_open_id, str) or not _OPEN_ID.fullmatch(expected_target_open_id):
        raise MigrationBlocked("exact_target_open_id_required")
    with connectors.application_guard():
        db.rollback()
        now = _now()
        operation = db.get(BotMigration, operation_id)
        _scope(db, operation, now)
        with authority_guard(operation.bot_id):
            db.expire_all()
            _scope(db, operation, now)
            bot = db.get(Bot, operation.bot_id)
            binding = authenticate(db, bot_id=bot.id, bot_uuid=bot.uuid, token=source_key)
            if binding is None or not bot.resource_auth_enabled or str(binding.platform).lower() != "infoflow":
                raise MigrationBlocked("source_pairing_authority_unavailable")
            if db.query(MigrationPrincipalPair).filter_by(operation_id=operation.id).count() >= 512:
                raise MigrationBlocked("pairing_issue_limit_reached")
            instance = ensure_instance_id(db)
            nonce = secrets.token_urlsafe(32)
            expiry = now + timedelta(seconds=120)
            if binding.credential_expires_at:
                expiry = min(expiry, binding.credential_expires_at)
            row = MigrationPrincipalPair(
                id=uuid.uuid4().hex,
                operation_id=operation.id,
                instance_id=instance,
                source_digest=operation.source_digest,
                target_digest=operation.target_digest,
                source_binding_id=binding.id,
                source_binding_digest=binding.credential_digest,
                source_user_id=binding.external_user_id,
                expected_target_open_id=expected_target_open_id,
                nonce_digest=_hash(nonce),
                source_proof_ref=uuid.uuid4().hex,
                created_at=now,
                expires_at=expiry,
                status="pending",
            )
            db.add(row)
            db.commit()
            return {
                "pairing_id": row.id,
                "command": "migration-pair " + nonce,
                "expected_target_open_id": expected_target_open_id,
                "expires_at": row.expires_at.isoformat() + "Z",
                "can_commit": False,
            }


def consume_verified_pair_event(db, application_id, data, message, *, verified_ingress=False):
    command = message.get("command", "")
    if not isinstance(command, str) or command.split(maxsplit=1)[:1] != ["migration-pair"]:
        return None
    response = {"accepted": False, "migration_pairing": True}
    if not verified_ingress or not _budget.claim(application_id, message.get("user_id")):
        return response
    match = _COMMAND.fullmatch(command)
    if match is None:
        return response
    with connectors.application_guard():
        db.rollback()
        row = db.query(MigrationPrincipalPair).filter_by(nonce_digest=_hash(match[1]), status="pending").first()
        if row is None:
            return response
        operation = db.get(BotMigration, row.operation_id)
        if operation is None:
            return response
        with authority_guard(operation.bot_id):
            now = _now()
            try:
                operation, app = _valid(db, row, now)
            except MigrationBlocked:
                row.status = "invalidated"
                db.commit()
                return response
            if (
                message.get("user_id") != row.expected_target_open_id
                or application_id != app.id
                or message.get("group_id") != operation.target_chat_id
                or data.get("header", {}).get("app_id") != app.app_id
                or data.get("header", {}).get("tenant_key") != app.tenant_key
            ):
                return response
            try:
                event_ms = int(data["event"]["message"]["create_time"])
            except (KeyError, TypeError, ValueError):
                return response
            if not (
                int(row.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
                <= event_ms
                <= int(now.replace(tzinfo=timezone.utc).timestamp() * 1000) + 1000
            ):
                return response
            result = db.execute(
                update(MigrationPrincipalPair)
                .where(
                    MigrationPrincipalPair.id == row.id,
                    MigrationPrincipalPair.status == "pending",
                )
                .values(
                    status="target_verified",
                    target_proof_ref=uuid.uuid4().hex,
                    target_event_digest=_hash(message["message_id"]),
                )
            )
            db.commit()
            return {"accepted": result.rowcount == 1, "migration_pairing": True}


def confirm_pairing_source(db, pairing_id, *, source_key, expected_target_open_id):
    with connectors.application_guard():
        db.rollback()
        row = db.get(MigrationPrincipalPair, pairing_id)
        if row is None or row.status != "target_verified":
            raise MigrationBlocked("target_principal_not_verified")
        operation = db.get(BotMigration, row.operation_id)
        if operation is None:
            raise MigrationBlocked("pairing_scope_changed_or_expired")
        with authority_guard(operation.bot_id):
            _valid(db, row, _now())
            binding = authenticate(db, bot_id=operation.bot_id, bot_uuid=operation.bot_uuid, token=source_key)
            if (
                binding is None
                or binding.id != row.source_binding_id
                or binding.credential_digest != row.source_binding_digest
                or expected_target_open_id != row.expected_target_open_id
            ):
                raise MigrationBlocked("source_pairing_confirmation_mismatch")
            row.status, row.source_confirmed_at = "source_confirmed", _now()
            db.commit()
            return {"pairing_id": row.id, "source_confirmed": True, "can_commit": False}


def review_pairing(db, pairing_id, *, actor_id, token_version, exact_source_user_id, exact_target_open_id):
    with connectors.application_guard():
        db.rollback()
        _reviewer(db, actor_id, token_version)
        row = db.get(MigrationPrincipalPair, pairing_id)
        if row is None or row.status != "source_confirmed":
            raise MigrationBlocked("source_confirmation_required")
        operation = db.get(BotMigration, row.operation_id)
        if operation is None:
            raise MigrationBlocked("pairing_scope_changed_or_expired")
        with authority_guard(operation.bot_id):
            _valid(db, row, _now())
            if (exact_source_user_id, exact_target_open_id) != (row.source_user_id, row.expected_target_open_id):
                raise MigrationBlocked("exact_principal_review_required")
            row.reviewed_by, row.reviewer_token_version = actor_id, token_version
            row.reviewed_at, row.status = _now(), "reviewed"
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise MigrationBlocked("reviewed_pairing_conflicts") from None
            return {"pairing_id": row.id, "reviewed": True, "can_commit": False}


def load_reviewed_links(db, operation_id, *, actor_id, token_version):
    """Private planner input only; recheck all proofs, never issue destination keys."""
    with connectors.application_guard():
        db.rollback()
        _reviewer(db, actor_id, token_version)
        operation = db.get(BotMigration, operation_id)
        _scope(db, operation, _now())
        with authority_guard(operation.bot_id):
            rows = db.query(MigrationPrincipalPair).filter_by(operation_id=operation.id, status="reviewed").all()
            result = []
            for row in rows:
                _, app = _valid(db, row, _now())
                _reviewer(db, row.reviewed_by, row.reviewer_token_version)
                if row.reviewed_by != actor_id:
                    raise MigrationBlocked("current_reviewer_confirmation_required")
                bot = db.get(Bot, operation.bot_id)
                scope = MappingScope(
                    row.instance_id,
                    operation.id,
                    bot.uuid,
                    operation.source_revision,
                    bot.user_id,
                    app.id,
                    app.app_id,
                    app.tenant_key,
                    operation.target_chat_id,
                )
                result.append(
                    ReviewedLink(
                        scope,
                        row.source_user_id,
                        row.expected_target_open_id,
                        row.source_proof_ref,
                        row.target_proof_ref,
                        actor_id,
                        row.expires_at.replace(tzinfo=timezone.utc).timestamp(),
                    )
                )
            return tuple(result)


def invalidate_pairing(db, pairing_id, *, actor_id, token_version):
    """Explicitly retire proof authority, retaining its evidence and original principals."""
    with connectors.application_guard():
        db.rollback()
        _reviewer(db, actor_id, token_version)
        row = db.get(MigrationPrincipalPair, pairing_id)
        if row is None:
            raise MigrationBlocked("pairing_not_found")
        _scope(db, db.get(BotMigration, row.operation_id), _now())
        row.status = "invalidated"
        db.commit()
        return {"pairing_id": row.id, "invalidated": True, "can_commit": False}
