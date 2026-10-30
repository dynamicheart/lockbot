"""Read-only observations for a future, explicitly confirmed platform handover.

This module deliberately cannot create a migration, reserve a route or change state.
"""

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute


class MigrationPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: int = Field(gt=0)
    chat_id: str = Field(pattern=r"^oc_[A-Za-z0-9_-]+$", max_length=128)


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


def _inventory_matches(state, bot):
    """A missing node/card cannot be mistaken for an empty allocation pool."""
    try:
        cluster = json.loads(bot.cluster_configs)
        if bot.bot_type in {"DEVICE", "QUEUE_DEVICE"}:
            return (
                isinstance(cluster, dict)
                and bool(cluster)
                and set(cluster) == set(state)
                and all(
                    isinstance(state[node], list)
                    and len(state[node]) == len(models)
                    and {card.get("dev_id") for card in state[node]} == set(range(len(models)))
                    for node, models in cluster.items()
                )
            )
        keys = set(cluster) if isinstance(cluster, (list, dict)) else None
        return bool(keys) and keys == set(state)
    except (TypeError, ValueError, AttributeError):
        return False


def observe_migration(db, manager, bot_id, body):
    bot = db.get(Bot, bot_id)
    if bot is None or bot.is_deleted:
        raise HTTPException(404, "Bot not found")
    blockers = []
    counts = None
    generation = None
    try:
        with manager.admit(bot_id, for_read=True) as lease, lease.bot._lock:
            db.rollback()
            bot = db.get(Bot, bot_id)
            if bot is None or bot.is_deleted:
                raise HTTPException(404, "Bot not found")
            generation = lease.generation
            if lease.health == "error":
                blockers.append("source_generation_unhealthy")
            if lease.bot.config.get_val("_IDENTITY_REVISION", 0) != bot.identity_revision:
                blockers.append("source_generation_changed")
            try:
                counts = _counts(lease.bot.state.bot_state, bot.bot_type)
                if not _inventory_matches(lease.bot.state.bot_state, bot):
                    blockers.append("source_inventory_mismatch")
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
    if counts and any(counts[field] for field in ("holders", "reservations", "offers", "non_idle")):
        blockers.append("source_has_outstanding_allocations")
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
        "owner_id": bot.user_id,
        "group_id": bot.group_id,
        "resource_auth_enabled": bot.resource_auth_enabled,
        "resource_api_notify_enabled": bot.resource_api_notify_enabled,
        "target": body.model_dump(),
    }
    digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
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
        "target": target,
        "stored_obligations": counts,
        "identity_mapping": {"implemented": False, "unresolved_principals": None},
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
