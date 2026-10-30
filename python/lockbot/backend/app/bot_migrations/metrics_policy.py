"""Deployment-reviewed gateway expiry/clock policy, never HTTP-supplied authority."""

import json
import math
import os
import time

from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bots.metrics_signers import _private_file, canonical_audience, select_metrics_signer
from lockbot.backend.app.settings.models import SiteSetting


def reviewed_metrics_policy(db, bot):
    if not bot.utilization_config:
        return None
    try:
        source = json.loads(bot.utilization_config)
        if not isinstance(source, dict) or source.get("accessMode") != "short_lived":
            raise ValueError()
        endpoint = canonical_audience(source["endpoint"])
        signer = select_metrics_signer(endpoint)
        if signer is None or signer.audience != endpoint:
            raise ValueError()
        raw, _ = _private_file(os.environ.get("LOCKBOT_MIGRATION_METRICS_POLICY_FILE"), 65536)
        policy = json.loads(raw)
        instance = db.get(SiteSetting, "resource_api_instance_id")
        if (
            not isinstance(policy, dict)
            or policy.get("version") != 1
            or instance is None
            or policy.get("issuer") != instance.value
            or policy.get("audience") != endpoint
            or policy.get("profile") != "lockbot-gateway-v1"
            or policy.get("maximum_token_ttl_seconds") != 300
            or policy.get("expiry_leeway_seconds") != 15
            or policy.get("verify_expiration") is not True
            or not isinstance(policy.get("review_reference"), str)
            or not 1 <= len(policy["review_reference"]) <= 128
        ):
            raise ValueError()
        skew, expiry = policy.get("maximum_clock_skew_seconds"), policy.get("review_valid_until")
        if (
            type(skew) not in (int, float)
            or not math.isfinite(skew)
            or not 0 <= skew <= 300
            or type(expiry) not in (int, float)
            or not math.isfinite(expiry)
            or not time.time() < expiry <= time.time() + 86400
        ):
            raise ValueError()
        return {"source": source, "maximum_clock_skew_seconds": skew}
    except (ValueError, TypeError, KeyError, OSError):
        raise MigrationBlocked("reviewed_metrics_expiry_clock_policy_required") from None
