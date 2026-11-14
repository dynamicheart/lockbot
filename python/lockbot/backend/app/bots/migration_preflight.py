"""Read-only observations for a future, explicitly confirmed platform handover.

This module deliberately cannot create a migration, reserve a route or change state.
"""

import hashlib
import hmac
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.config import JWT_ALGORITHM, JWT_SECRET
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.platform_applications.service import fingerprint
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.core.io import empty_device_state, empty_node_state, normalize_cluster_configs
from lockbot.core.platforms.registry import normalize_platform_name


class MigrationPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: int = Field(gt=0)
    chat_id: str = Field(pattern=r"^oc_[A-Za-z0-9_-]+$", max_length=128)


class MigrationReview(MigrationPreflight):
    review_receipt: str = Field(min_length=1, max_length=2048)


def _counts(state, bot_type):
    """Count stored obligations, including expired-but-unreconciled entries."""
    if not isinstance(state, dict):
        raise ValueError("Unrecognized state")
    result = {"resources": 0, "holders": 0, "reservations": 0, "offers": 0, "non_idle": 0}
    for value in state.values():
        entries = value if bot_type in {"DEVICE", "QUEUE_DEVICE"} else [value]
        if not isinstance(entries, list):
            raise ValueError("Unrecognized devices")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("status") not in {"idle", "exclusive", "shared"}:
                raise ValueError("Unrecognized resource")
            current, books = entry.get("current_users", []), entry.get("booking_list", [])
            if not isinstance(current, list) or not isinstance(books, list):
                raise ValueError("Unrecognized obligations")
            if not all(isinstance(user, dict) for user in current + books):
                raise ValueError("Unrecognized principal")
            result["resources"] += 1
            result["holders"] += len(current)
            result["reservations"] += len(books)
            result["offers"] += sum(bool(user.get("is_notified")) for user in books)
            result["non_idle"] += entry["status"] != "idle"
    return result


def _empty_destination_state(state, bot_type, cluster):
    """Prepare a clean runtime image only if the source matches the configured inventory."""
    if bot_type in {"DEVICE", "QUEUE_DEVICE"}:
        if (
            not isinstance(cluster, dict)
            or not cluster
            or not all(
                isinstance(models, list) and models and all(isinstance(model, str) and model for model in models)
                for models in cluster.values()
            )
        ):
            raise ValueError("Unrecognized device inventory")
        destination = empty_device_state(normalize_cluster_configs(cluster))
        if set(destination) != set(state) or any(
            len(cards) != len(state[node])
            or {card["dev_id"] for card in cards} != {card.get("dev_id") for card in state[node]}
            for node, cards in destination.items()
        ):
            raise ValueError("Source and destination devices differ")
        if bot_type == "QUEUE_DEVICE":
            for cards in destination.values():
                for card in cards:
                    card["booking_list"] = []
    elif bot_type in {"NODE", "QUEUE"} and isinstance(cluster, (list, dict)) and cluster:
        names = list(normalize_cluster_configs(cluster))
        destination = empty_node_state(names)
        if len(destination) != len(names) or set(destination) != set(state):
            raise ValueError("Source and destination nodes differ")
    else:
        raise ValueError("Unrecognized node inventory")
    return destination


def _review_digest(value):
    key = hmac.digest(JWT_SECRET.encode(), b"lockbot.migration-reset-review.v1", "sha256")
    return hmac.new(key, json.dumps(value, allow_nan=False, sort_keys=True).encode(), hashlib.sha256).hexdigest()


_RECEIPT_TTL = timedelta(minutes=5)
_RECEIPT_AUDIENCE = "lockbot.migration-reset-review"


def _receipt_key():
    return hmac.digest(JWT_SECRET.encode(), b"lockbot.migration-reset-receipt.v1", "sha256")


def _review_scope(user, observation):
    return {
        "actor": user.id,
        "actor_version": user.effective_token_version,
        "bot_uuid": observation["source"]["bot_uuid"],
        "identity_revision": observation["source"]["identity_revision"],
        "application": observation["target"]["application_id"],
        "chat": observation["target"]["chat_id"],
        "review": observation["review_digest"],
    }


def migration_review_receipt(user, observation, *, now=None):
    """Issue a short-lived receipt for an observed reset scope."""
    if not observation.get("review_digest"):
        return None
    now = now or datetime.now(timezone.utc)
    expires = now + _RECEIPT_TTL
    claims = {**_review_scope(user, observation), "aud": _RECEIPT_AUDIENCE, "exp": expires, "iat": now}
    return {
        "token": jwt.encode(claims, _receipt_key(), algorithm=JWT_ALGORITHM),
        "expires_at": expires.isoformat(),
    }


def verify_migration_review_receipt(token, user, fresh_observation, *, now=None):
    """Compare against a fresh observation after the caller has checked current authority."""
    if not fresh_observation.get("review_digest") or not isinstance(token, str) or len(token) > 2048:
        return False
    try:
        claims = jwt.decode(
            token,
            _receipt_key(),
            algorithms=[JWT_ALGORITHM],
            audience=_RECEIPT_AUDIENCE,
            options={"require": ["iat", "exp", "aud"], "verify_iat": False, "verify_exp": False},
        )
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            return False
        now = (now or datetime.now(timezone.utc)).timestamp()
        if not claims["iat"] <= now < claims["exp"] or claims["exp"] - claims["iat"] != _RECEIPT_TTL.total_seconds():
            return False
        return all(claims.get(key) == value for key, value in _review_scope(user, fresh_observation).items())
    except (jwt.PyJWTError, ValueError):
        return False


def _reset_authorizations(db, bot, now):
    live = (
        ResourceBinding.bound_bot_id == bot.id,
        ResourceBinding.bound_bot_uuid == bot.uuid,
        ResourceBinding.bound_identity_revision == bot.identity_revision,
        ResourceBinding.platform == normalize_platform_name(bot.platform),
        ResourceBinding.revoked_at.is_(None),
        (ResourceBinding.credential_expires_at.is_(None)) | (ResourceBinding.credential_expires_at > now),
    )
    bindings = db.execute(
        select(ResourceBinding.id, ResourceBinding.credential_generation, ResourceBinding.credential_expires_at)
        .where(*live)
        .order_by(ResourceBinding.id)
        .execution_options(yield_per=200)
    )
    declarations = db.execute(
        select(UsageDeclaration.id, UsageDeclaration.updated_at, UsageDeclaration.expires_at)
        .join(ResourceBinding, UsageDeclaration.binding_id == ResourceBinding.id)
        .where(*live, UsageDeclaration.bot_id == bot.id, UsageDeclaration.expires_at > now)
        .order_by(UsageDeclaration.id)
        .execution_options(yield_per=200)
    )
    counts, revisions = {}, {}
    for name, rows in (("agent_bindings", bindings), ("usage_declarations", declarations)):
        count = 0
        digest = hashlib.sha256()
        for record in rows:
            count += 1
            digest.update(json.dumps(tuple(record), default=str, separators=(",", ":")).encode() + b"\n")
        counts[name] = count
        revisions[name] = digest.hexdigest()
    return counts, revisions


def observe_migration(db, manager, bot_id, body, *, retired=None):
    bot = db.get(Bot, bot_id)
    if bot is None or bot.is_deleted:
        raise HTTPException(404, "Bot not found")
    blockers = []
    counts = None
    observed_at = datetime.now(timezone.utc)
    generation = None
    source_review = None
    target_review = None
    identity_rule_counts = None
    try:
        admission = manager.admit(bot_id, for_read=True) if retired is None else nullcontext(retired)
        with admission as lease, lease.bot._lock:
            db.rollback()
            bot = db.get(Bot, bot_id)
            if bot is None or bot.is_deleted:
                raise HTTPException(404, "Bot not found")
            generation = lease.generation
            whitelist = lease.bot.config.get_val("DURATION_WHITELIST", []) or []
            if not isinstance(whitelist, list):
                blockers.append("source_identity_rules_unrecognized")
            else:
                identity_rule_counts = {"DURATION_WHITELIST": len(whitelist)}
            if lease.health == "error":
                blockers.append("source_generation_unhealthy")
            if lease.bot.config.get_val("_IDENTITY_REVISION", 0) != bot.identity_revision:
                blockers.append("source_generation_changed")
            try:
                state = lease.bot.state.bot_state
                counts = _counts(state, bot.bot_type)
                authorization_counts, revisions = _reset_authorizations(db, bot, observed_at.replace(tzinfo=None))
                counts.update(authorization_counts)
                try:
                    destination = _empty_destination_state(state, bot.bot_type, json.loads(bot.cluster_configs))
                except (AttributeError, TypeError, ValueError):
                    blockers.append("source_inventory_mismatch")
                else:
                    source_review = _review_digest(
                        {
                            "state": state,
                            "destination_state": destination,
                            "authorization_revisions": revisions,
                            "duration_whitelist": whitelist,
                            "op_key": bot.api_key,
                            "messaging_credentials": fingerprint(bot, "infoflow"),
                        }
                    )
            except (TypeError, ValueError):
                blockers.append("source_state_unrecognized")
    except (BotNotRunning, InstanceRetiring, AdmissionFull):
        blockers.append("source_state_unavailable")
    if bot.status != "running":
        blockers.append("source_status_not_running")
    if str(bot.platform).lower() != "infoflow":
        blockers.append("source_platform_unsupported")
    if bot.bot_type not in {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}:
        blockers.append("source_type_unsupported")
    if not bot.uuid:
        blockers.append("source_identity_missing")
    app = db.get(FeishuApplication, body.application_id)
    target = {"application_id": body.application_id, "chat_id": body.chat_id}
    if app is None:
        blockers.append("target_application_missing")
    else:
        target.update({"application_name": app.name, "transport": app.transport})
        if not app.tenant_key or not app.bot_open_id:
            blockers.append("target_application_identity_unknown")
        membership = db.get(FeishuGroupMembership, (app.id, body.chat_id))
        target["membership_observation"] = membership.status if membership else "unknown"
        target_review = {
            "app_id": app.app_id,
            "tenant_key": app.tenant_key,
            "bot_open_id": app.bot_open_id,
            "transport": app.transport,
            "credentials": fingerprint(app, "feishu"),
            "membership_event": (
                [membership.event_id, membership.event_time_ms, membership.status] if membership is not None else None
            ),
        }
        if membership is None or membership.status != "present":
            blockers.append("target_membership_unverified")
        elif (membership.app_id, membership.tenant_key) != (app.app_id, app.tenant_key):
            blockers.append("target_membership_identity_changed")
    route = db.query(FeishuGroupRoute).filter_by(application_id=body.application_id, chat_id=body.chat_id).first()
    if route is not None:
        blockers.append("target_group_already_bound")
    if db.query(FeishuGroupRoute).filter_by(bot_id=bot_id).first() is not None:
        blockers.append("source_already_has_feishu_route")
    # Selected configuration feeds an observation digest; no raw configuration or task data is returned.
    reviewed = {
        "uuid": bot.uuid,
        "revision": bot.identity_revision,
        "platform": bot.platform,
        "bot_type": bot.bot_type,
        "cluster": bot.cluster_configs,
        "rules": bot.config_overrides,
        "utilization": bot.utilization_config,
        "node_access": bot.node_access_config,
        "owner_id": bot.user_id,
        "group_id": bot.group_id,
        "resource_auth_enabled": bot.resource_auth_enabled,
        "resource_api_notify_enabled": bot.resource_api_notify_enabled,
        "target": body.model_dump(),
    }
    digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "read_only": True,
        "can_commit": False,
        "reservation_created": False,
        "source": {
            "bot_id": bot.id,
            "bot_uuid": bot.uuid,
            "identity_revision": bot.identity_revision,
            "platform": bot.platform,
            "status": bot.status,
            "generation": generation,
        },
        "configuration_digest": digest,
        "review_digest": (
            _review_digest({"configuration": digest, "source": source_review, "target": target_review})
            if not blockers and source_review and target_review
            else None
        ),
        "target": target,
        "stored_obligations": counts,
        "identity_mapping": {"implemented": False, "unresolved_principals": None},
        "reset_policy": "clear_live_state_and_reenroll",
        "identity_bound_rule_counts": identity_rule_counts,
        "blockers": blockers + ["cutover_not_implemented"],
        "unverified": [
            "application_publication",
            "application_credentials",
            "receiver_connectivity",
            "current_group_membership",
            "durable_freeze_and_recovery",
        ],
        "credential_changes": {
            "agent": "Destination group re-enrollment required; source revision must be invalidated.",
            "op": "Explicitly revoke and reissue; identity_revision alone does not revoke existing OP keys.",
            "metrics": {
                "stop_new_issuance_before_wait": True,
                "minimum_wait_seconds": 315,
                "gateway_profile": "lockbot_gateway",
                "third_party_gateway_requires_separate_verification": True,
                "additional_clock_skew_must_be_verified": True,
                "revision_revokes_existing_grants": False,
            },
        },
        "next_step": "Review blockers; do not change platform or replay allocation commands. "
        "This observation is not an executable migration plan.",
    }
