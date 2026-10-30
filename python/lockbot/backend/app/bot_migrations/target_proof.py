"""Disabled management workflow, existing verified ingress only; never grants a route.

A signed post-creation group nonce proves delivery, not source/destination account
ownership or enterprise publication. No HTTP endpoint creates these records yet.
"""

import hashlib
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked, source_digest, target_digest
from lockbot.backend.app.bot_migrations.models import JOURNAL_VERSION, BotMigration, MigrationTargetProof
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.connectors import connectors
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.feishu.traffic import IngressBudget
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.settings.models import SiteSetting

_budget = IngressBudget(app_limit=60, actor_limit=6)
_COMMAND = re.compile(r"^migration-check ([A-Za-z0-9_-]{43})$")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _now():
    return datetime.utcfromtimestamp(time.time())


def _reviewer(db, actor_id, token_version):
    actor = db.get(User, actor_id)
    if (
        actor is None
        or actor.role != "super_admin"
        or actor.must_change_password
        or actor.effective_token_version != token_version
    ):
        raise MigrationBlocked("current_review_authority_required")


def _scope(db, operation, now):
    if (
        operation is None
        or operation.schema_version != JOURNAL_VERSION
        or operation.phase not in {"planned", "checkpointed"}
        or (operation.phase == "planned" and operation.expires_at <= now)
    ):
        raise MigrationBlocked("migration_proof_scope_unavailable")
    bot, app = db.get(Bot, operation.bot_id), db.get(FeishuApplication, operation.target_application_id)
    if (
        bot is None
        or bot.is_deleted
        or bot.uuid != operation.bot_uuid
        or bot.identity_revision != operation.source_revision
        or str(bot.platform).lower() != "infoflow"
        or source_digest(bot) != operation.source_digest
        or app is None
        or not app.tenant_key
        or not app.bot_open_id
        or target_digest(app, operation.target_chat_id) != operation.target_digest
    ):
        raise MigrationBlocked("migration_proof_scope_changed")
    if db.query(FeishuGroupRoute).filter_by(application_id=app.id, chat_id=operation.target_chat_id).first():
        raise MigrationBlocked("target_group_already_bound")
    member = db.get(FeishuGroupMembership, (app.id, operation.target_chat_id))
    if (
        member is None
        or member.status != "present"
        or (member.app_id, member.tenant_key) != (app.app_id, app.tenant_key)
    ):
        raise MigrationBlocked("verified_bot_added_observation_required")
    return app


def create_target_challenge(
    db,
    operation_id,
    *,
    actor_id,
    token_version,
    reviewed_target_digest,
    publication_evidence_ref,
    publication_valid_until,
):
    """Internal explicit admin attestation; reference reviewed publication evidence.

    This is not a provider publication-status verifier, and must not be reachable
    through a management endpoint until review/confirmation UX and policy are ready.
    """
    with connectors.application_guard():
        db.rollback()
        _reviewer(db, actor_id, token_version)
        now = _now()
        operation = db.get(BotMigration, operation_id)
        _scope(db, operation, now)
        if (
            reviewed_target_digest != operation.target_digest
            or not isinstance(publication_evidence_ref, str)
            or not re.fullmatch(r"[a-f0-9]{32}", publication_evidence_ref)
            or not isinstance(publication_valid_until, datetime)
            or publication_valid_until.tzinfo is not None
            or not now < publication_valid_until <= now + timedelta(hours=1)
        ):
            raise MigrationBlocked("reviewed_publication_evidence_required")
        if db.query(MigrationTargetProof).filter_by(operation_id=operation.id).count() >= 32:
            raise MigrationBlocked("migration_proof_issue_limit_reached")
        instance = ensure_instance_id(db)
        # No database API response or log can reconstruct the nonce after this call.
        nonce = secrets.token_urlsafe(32)
        row = MigrationTargetProof(
            id=uuid.uuid4().hex,
            operation_id=operation.id,
            instance_id=instance,
            source_digest=operation.source_digest,
            target_digest=operation.target_digest,
            nonce_digest=_hash(nonce),
            reviewer_id=actor_id,
            reviewer_token_version=token_version,
            publication_evidence_ref=publication_evidence_ref,
            publication_valid_until=publication_valid_until,
            created_at=now,
            expires_at=min(now + timedelta(seconds=120), publication_valid_until),
            status="pending",
        )
        db.add(row)
        db.commit()
        return {
            "proof_id": row.id,
            "command": "migration-check " + nonce,
            "expires_at": row.expires_at.isoformat() + "Z",
            "can_commit": False,
        }


def _receipt_scope(db, row, now):
    operation = db.get(BotMigration, row.operation_id)
    app = _scope(db, operation, now)
    _reviewer(db, row.reviewer_id, row.reviewer_token_version)
    instance = db.get(SiteSetting, "resource_api_instance_id")
    if (
        instance is None
        or instance.value != row.instance_id
        or row.source_digest != operation.source_digest
        or row.target_digest != operation.target_digest
        or not row.created_at <= now < row.expires_at
        or row.publication_valid_until <= now
    ):
        raise MigrationBlocked("migration_proof_expired_or_changed")
    return operation, app


def consume_verified_group_event(db, application_id, data, message, *, verified_ingress=False):
    """Only call after existing signature/SDK verification and real own-AT normalization.

    Return None for ordinary commands. Reserved proof commands never reach an
    allocator, unbound hint, public operation log, group binding or outgoing send.
    """
    command = message.get("command", "")
    if not isinstance(command, str) or command.split(maxsplit=1)[:1] != ["migration-check"]:
        return None
    response = {"accepted": False, "migration_proof": True}
    if not verified_ingress:
        return response
    if not _budget.claim(application_id, message.get("user_id")):
        return {**response, "reason": "rate_limited"}
    match = _COMMAND.fullmatch(command)
    if match is None:
        return response
    with connectors.application_guard():
        db.rollback()
        row = db.query(MigrationTargetProof).filter_by(nonce_digest=_hash(match[1]), status="pending").first()
        if row is None:
            return response
        now = _now()
        try:
            operation, app = _receipt_scope(db, row, now)
        except MigrationBlocked:
            row.status = "invalidated"
            db.commit()
            return response
        if (
            app.id != application_id
            or message.get("group_id") != operation.target_chat_id
            or data.get("header", {}).get("app_id") != app.app_id
            or data.get("header", {}).get("tenant_key") != app.tenant_key
        ):
            return response
        # The provider timestamp must also be post-creation; an old signed event is not fresh proof.
        try:
            event_ms = int(data["event"]["message"]["create_time"])
        except (KeyError, TypeError, ValueError):
            return response
        if (
            event_ms < int(row.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
            or event_ms > int(now.replace(tzinfo=timezone.utc).timestamp() * 1000) + 1000
        ):
            return response
        result = db.execute(
            update(MigrationTargetProof)
            .where(
                MigrationTargetProof.id == row.id,
                MigrationTargetProof.status == "pending",
            )
            .values(status="consumed", consumed_at=now, event_digest=_hash(message["message_id"]))
        )
        db.commit()
        return {"accepted": result.rowcount == 1, "migration_proof": True}


def inspect_target_receipt(db, proof_id, *, actor_id, token_version):
    """Private validator input, still not cutover authority or proof of account ownership."""
    with connectors.application_guard():
        db.rollback()
        _reviewer(db, actor_id, token_version)
        row = db.get(MigrationTargetProof, proof_id)
        if row is None or row.status != "consumed":
            raise MigrationBlocked("target_delivery_proof_unverified")
        _receipt_scope(db, row, _now())
        return {
            "proof_id": row.id,
            "delivery_verified": True,
            "publication_reviewed": True,
            "account_ownership_verified": False,
            "can_commit": False,
        }
