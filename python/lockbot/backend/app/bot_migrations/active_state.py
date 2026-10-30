"""Disabled private active-state planner; never writes allocator state or grants authority.

Proof records must come from a separately verified workflow, not an API request body.
A plan is a comparison artifact, never permission to execute a migration.
"""

import copy
import json
import math
import re
from dataclasses import dataclass

from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked, digest
from lockbot.backend.app.bots.migration_preflight import _inventory_matches
from lockbot.core.resource_policy import resource_entries

PROOF_ID = re.compile(r"^[a-f0-9]{32}$")
OPEN_ID = re.compile(r"^ou_[A-Za-z0-9_-]{1,125}$")
USER_FIELDS = {"user_id", "start_time", "duration", "is_notified", "holding_id", "source", "booked_start_time"}
SUPPORTED_RULES = {
    "DURATION_WHITELIST",
    "MAX_RESOURCES_PER_USER",
    "FORBID_RELOCK",
    "MAX_LOCK_DURATION",
    "DEFAULT_LOCK_DURATION",
    "TIME_ALERT",
    "EARLY_NOTIFY",
}


@dataclass(frozen=True, repr=False)
class MappingScope:
    instance_id: str
    operation_id: str
    bot_uuid: str
    source_revision: int
    owner_id: int
    target_application_id: int
    target_app_id: str
    target_tenant_key: str
    target_chat_id: str


@dataclass(frozen=True, repr=False)
class ReviewedLink:
    scope: MappingScope
    source_user_id: str
    target_open_id: str
    source_account_proof: str
    target_group_sender_proof: str
    reviewed_by: int
    expires_at: float


@dataclass(frozen=True, repr=False)
class ActiveStatePlan:
    scope: MappingScope
    source_state_digest: str
    source_inventory_digest: str
    source_rules_digest: str
    target_state_digest: str
    target_rules_digest: str
    reviewed_by: int
    valid_before: float
    mapped_state: dict
    mapped_rules: dict
    provenance: tuple
    counts: dict

    def public_summary(self):
        return {
            "counts": dict(self.counts),
            "valid_before": self.valid_before,
            "can_commit": False,
            "active_state_execution_enabled": False,
            "task_declarations_require_reenrollment": True,
            "source_credentials_transferred": False,
        }


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _links(links, scope, reviewed_by, now):
    if (
        not isinstance(scope, MappingScope)
        or type(reviewed_by) is not int
        or reviewed_by <= 0
        or not scope.instance_id
        or not PROOF_ID.fullmatch(scope.operation_id)
        or not scope.bot_uuid
        or type(scope.source_revision) is not int
        or scope.source_revision < 0
        or type(scope.owner_id) is not int
        or scope.owner_id <= 0
        or type(scope.target_application_id) is not int
        or scope.target_application_id <= 0
        or not scope.target_app_id
        or not scope.target_tenant_key
        or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", scope.target_chat_id)
    ):
        raise MigrationBlocked("mapping_scope_unverified")
    result, destinations, proofs = {}, set(), set()
    deadline = now + 300
    for link in links:
        if (
            not isinstance(link, ReviewedLink)
            or link.scope != scope
            or link.reviewed_by != reviewed_by
            or not isinstance(link.source_user_id, str)
            or not 1 <= len(link.source_user_id) <= 128
            or not isinstance(link.target_open_id, str)
            or not OPEN_ID.fullmatch(link.target_open_id)
            or not isinstance(link.source_account_proof, str)
            or not PROOF_ID.fullmatch(link.source_account_proof)
            or not isinstance(link.target_group_sender_proof, str)
            or not PROOF_ID.fullmatch(link.target_group_sender_proof)
            or link.source_account_proof == link.target_group_sender_proof
            or not _finite(link.expires_at)
            or not now < link.expires_at <= now + 300
        ):
            raise MigrationBlocked("reviewed_identity_proof_unverified")
        if link.source_user_id in result or link.target_open_id in destinations:
            raise MigrationBlocked("identity_mapping_not_bijective")
        if {link.source_account_proof, link.target_group_sender_proof} & proofs:
            raise MigrationBlocked("identity_proof_reused")
        proofs.update((link.source_account_proof, link.target_group_sender_proof))
        result[link.source_user_id] = link.target_open_id
        destinations.add(link.target_open_id)
        deadline = min(deadline, link.expires_at)
    return result, deadline


def inventory_digest(bot):
    try:
        cluster = json.loads(bot.cluster_configs)
    except (TypeError, ValueError):
        raise MigrationBlocked("source_inventory_changed") from None
    if bot.bot_type not in {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}:
        raise MigrationBlocked("source_inventory_changed")
    return digest({"bot_type": bot.bot_type, "cluster": cluster})


def build_active_state_plan(state, bot, rules, links, scope, *, reviewed_by, now):
    """Map exact principals in memory; expired obligations require lifecycle reconciliation."""
    if not _finite(now):
        raise MigrationBlocked("clock_unverified")
    mapping, deadline = _links(links, scope, reviewed_by, now)
    if (
        bot.uuid != scope.bot_uuid
        or bot.identity_revision != scope.source_revision
        or bot.user_id != scope.owner_id
        or str(bot.platform).lower() != "infoflow"
    ):
        raise MigrationBlocked("source_identity_changed")
    if not _inventory_matches(state, bot):
        raise MigrationBlocked("source_inventory_changed")
    if not isinstance(rules, dict) or set(rules) - SUPPORTED_RULES:
        raise MigrationBlocked("identity_or_unknown_rules_require_review")
    cap = rules.get("MAX_RESOURCES_PER_USER", -1)
    if type(cap) is not int or (cap != -1 and cap <= 0):
        raise MigrationBlocked("resource_quota_unverified")
    for key in ("FORBID_RELOCK", "EARLY_NOTIFY"):
        if key in rules and type(rules[key]) is not bool:
            raise MigrationBlocked("source_policy_unverified")
    for key in ("MAX_LOCK_DURATION", "DEFAULT_LOCK_DURATION", "TIME_ALERT"):
        if key in rules and (not _finite(rules[key]) or rules[key] < -1):
            raise MigrationBlocked("source_policy_unverified")
    whitelist = rules.get("DURATION_WHITELIST", [])
    if not isinstance(whitelist, list) or any(type(user) is not str for user in whitelist):
        raise MigrationBlocked("source_identity_rules_unrecognized")
    if len(set(whitelist)) != len(whitelist):
        raise MigrationBlocked("source_identity_rules_unrecognized")
    needed, claims, provenance = set(whitelist), {}, []
    result, mapped_rules = copy.deepcopy(state), copy.deepcopy(rules)
    counts = {"holders": 0, "bookings": 0, "offers": 0, "resources": 0}
    for resource, entry in resource_entries(result).items():
        counts["resources"] += 1
        if entry.get("status") not in {"idle", "shared", "exclusive"}:
            raise MigrationBlocked("source_state_unrecognized")
        holders, bookings = entry.get("current_users", []), entry.get("booking_list", [])
        if not isinstance(holders, list) or not isinstance(bookings, list):
            raise MigrationBlocked("source_state_unrecognized")
        if (entry["status"] == "idle") != (not holders):
            raise MigrationBlocked("source_state_unrecognized")
        if (
            bot.bot_type in {"QUEUE", "QUEUE_DEVICE"}
            and len(holders) > 1
            or entry["status"] == "exclusive"
            and len(holders) != 1
        ):
            raise MigrationBlocked("source_state_unrecognized")
        if bookings and bot.bot_type not in {"QUEUE", "QUEUE_DEVICE"}:
            raise MigrationBlocked("source_booking_mode_unrecognized")
        resource_users = set()
        for kind, users in (("holder", holders), ("booking", bookings)):
            for index, user in enumerate(users):
                if not isinstance(user, dict) or set(user) - USER_FIELDS:
                    raise MigrationBlocked("allocation_metadata_requires_explicit_review")
                principal = user.get("user_id")
                if not isinstance(principal, str) or principal not in mapping:
                    raise MigrationBlocked("allocation_principal_unmapped")
                if principal in resource_users:
                    raise MigrationBlocked("duplicate_resource_principal")
                resource_users.add(principal)
                needed.add(principal)
                if (
                    not _finite(user.get("start_time"))
                    or not _finite(user.get("duration"))
                    or user["duration"] <= 0
                    or not 0 <= user["start_time"] <= now
                    or type(user.get("is_notified")) is not bool
                    or not isinstance(user.get("holding_id"), str)
                    or not PROOF_ID.fullmatch(user["holding_id"])
                    or ("booked_start_time" in user and not _finite(user["booked_start_time"]))
                ):
                    raise MigrationBlocked("allocation_incarnation_unverified")
                offered = kind == "booking" and user["is_notified"]
                if offered and (index != 0 or holders):
                    raise MigrationBlocked("source_offer_unrecognized")
                expires = (
                    user["start_time"] + (300 if offered else user["duration"]) if kind == "holder" or offered else None
                )
                if expires is not None:
                    if expires <= now:
                        raise MigrationBlocked("expired_obligation_requires_reconciliation")
                    deadline = min(deadline, expires)
                claims.setdefault(principal, set()).add(resource)
                counts["holders" if kind == "holder" else "bookings"] += 1
                counts["offers"] += offered
                provenance.append(
                    (
                        resource,
                        kind,
                        index,
                        user["holding_id"],
                        principal,
                        mapping[principal],
                        user["start_time"],
                        user["duration"],
                        user.get("booked_start_time"),
                        expires,
                    )
                )
                user["user_id"] = mapping[principal]
    if needed - mapping.keys():
        raise MigrationBlocked("identity_rule_principal_unmapped")
    if mapping.keys() - needed:
        raise MigrationBlocked("unused_identity_mapping_requires_review")
    if cap != -1 and any(len(resources) > cap for resources in claims.values()):
        raise MigrationBlocked("mapped_resource_quota_requires_review")
    if "DURATION_WHITELIST" in mapped_rules:
        mapped_rules["DURATION_WHITELIST"] = [mapping[user] for user in whitelist]
    return ActiveStatePlan(
        scope,
        digest(state),
        inventory_digest(bot),
        digest(rules),
        digest(result),
        digest(mapped_rules),
        reviewed_by,
        deadline,
        result,
        mapped_rules,
        tuple(provenance),
        counts,
    )


def validate_active_state_plan(plan, state, bot, rules, scope, *, reviewed_by, now):
    """Refuse stale/tampered comparison artifacts; this still does not authorize writes."""
    if (
        not isinstance(plan, ActiveStatePlan)
        or plan.scope != scope
        or not _finite(now)
        or now >= plan.valid_before
        or plan.reviewed_by != reviewed_by
        or bot.uuid != scope.bot_uuid
        or bot.identity_revision != scope.source_revision
        or bot.user_id != scope.owner_id
        or str(bot.platform).lower() != "infoflow"
        or inventory_digest(bot) != plan.source_inventory_digest
        or not _inventory_matches(state, bot)
        or not _inventory_matches(plan.mapped_state, bot)
        or digest(state) != plan.source_state_digest
        or digest(rules) != plan.source_rules_digest
        or digest(plan.mapped_state) != plan.target_state_digest
        or digest(plan.mapped_rules) != plan.target_rules_digest
    ):
        raise MigrationBlocked("active_state_plan_changed_or_expired")
    return plan.public_summary()
