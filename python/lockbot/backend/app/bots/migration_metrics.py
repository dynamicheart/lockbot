"""Deployment-verified verifier bounds for the supervised platform cutover."""

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.metrics_signers import REGISTRY_ENV, _unique_object, canonical_audience
from lockbot.metrics.gateway import GRANT_CLOCK_LEEWAY_SECONDS, GRANT_MAX_TTL_SECONDS

METRICS_POLICY_ENV = "LOCKBOT_MIGRATION_METRICS_CLOCK_SKEW"
DEFAULT_WAIT_SECONDS = GRANT_MAX_TTL_SECONDS + GRANT_CLOCK_LEEWAY_SECONDS


def verified_metrics_gateways():
    raw = os.environ.get(METRICS_POLICY_ENV)
    if raw is None:
        return None
    if not raw or len(raw) > 65536:
        raise ValueError("Invalid migration metrics policy")
    policy = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(policy, dict) or not 1 <= len(policy) <= 64:
        raise ValueError("Invalid migration metrics policy")
    for audience, skew in policy.items():
        canonical_audience(audience)
        if type(skew) is not int or not 0 <= skew <= 3600:
            raise ValueError("Invalid migration metrics clock skew")
    return policy


def metrics_wait_seconds(bot):
    """Return zero for unsigned deployments, or None when the verifier is unverified."""
    source = public_datasource(bot)
    if not source and bot.utilization_config:
        return None
    endpoint = source["endpoint"] if source and source["accessMode"] == "short_lived" else None
    try:
        policy = verified_metrics_gateways()
        if policy is None:
            # Owner connection tests may have issued grants to a candidate audience.
            signing_enabled = REGISTRY_ENV in os.environ or bool(os.environ.get("METRICS_GRANT_SECRET_FILE"))
            return None if endpoint or signing_enabled else 0
        if endpoint is not None and canonical_audience(endpoint) not in policy:
            return None
        return DEFAULT_WAIT_SECONDS + max(policy.values())
    except (ValueError, RecursionError):
        return None


def metrics_grants_deadline(bot, migration):
    current_wait = metrics_wait_seconds(bot)
    recorded_wait = migration.metrics_wait_seconds or 0
    if current_wait is None or (recorded_wait and not current_wait):
        raise HTTPException(409, "The metrics gateway expiry policy must be verified before switching")
    wait = max(current_wait, recorded_wait)
    if not wait:
        return None
    if migration.drained_at is None:
        raise HTTPException(409, "The metrics gateway expiry policy must be verified before switching")
    return migration.drained_at + timedelta(seconds=wait)


def metrics_grants_expired(deadline, *, now=None):
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now > deadline
