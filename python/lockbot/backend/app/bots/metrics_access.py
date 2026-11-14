"""Issue short read-only data-source grants; never query or proxy hardware metrics."""

import json
import time
from collections import Counter
from datetime import timezone

import jwt

from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.metrics_signers import select_metrics_signer
from lockbot.backend.app.bots.migration_journal import migration_fence


def _resource_scope(bot, cluster, source, holdings):
    """Translate Lockbot holding IDs to unambiguous collector labels."""
    mapping = source["nodeMapping"]
    node_counts = Counter(mapping.values())
    device_bot = bot.bot_type in {"DEVICE", "QUEUE_DEVICE"}
    scope = {}
    for node, devices in holdings.items():
        label = mapping.get(node)
        if label is None or node_counts[label] != 1:
            continue
        if not device_bot:
            if devices is None:
                scope[label] = None
            continue
        if not devices:
            continue
        configured = cluster.get(node) if isinstance(cluster, dict) else None
        if not isinstance(configured, list):
            continue
        by_index = {str(index): source["deviceMapping"].get(f"{node}:dev{index}") for index in range(len(configured))}
        counts = Counter(by_index.values())
        allowed = sorted(mapped for device in devices if (mapped := by_index.get(device)) and counts[mapped] == 1)
        if allowed:
            scope[label] = allowed
    return scope


def issue_metrics_access(db, bot, source=None, *, expires_at=None, holdings=None):
    source = source or public_datasource(bot)
    if not source or source.get("accessMode", "independent") != "short_lived":
        return None
    if migration_fence(db, bot.id) is not None:
        return None
    signer = select_metrics_signer(source.get("endpoint"))
    if signer is None:
        return None
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id

    now = int(time.time())
    issued = now // 30 * 30
    expiry = issued + 300
    if expires_at is not None:
        # Database values are naive UTC; an already-aware value denotes its own
        # instant and must not be relabelled as UTC (for example +08:00).
        parent_expiry = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at
        expiry = min(expiry, int(parent_expiry.timestamp()))
    if expiry <= now:
        return None
    # Only source labels explicitly mapped from this bot's configured resources may be granted.
    try:
        cluster = json.loads(bot.cluster_configs)
        configured = (
            set(cluster)
            if isinstance(cluster, dict)
            else {item if isinstance(item, str) else item.get("node_key") for item in cluster}
        )
        mapping = source.get("nodeMapping", {})
        if set(mapping) - configured:
            return None
        if holdings is None:
            labels = sorted(set(mapping.values()))
            scope = {"nodes": labels}
            descriptor = {}
        else:
            if source.get("profile") != "lockbot-v1":
                return None
            labels = _resource_scope(bot, cluster, source, holdings)
            if sum(len(devices) for devices in labels.values() if devices is not None) > 512:
                return None
            # Older gateways require "nodes" and ignore unknown JWT claims.
            # Omit it entirely so the restricted grant can never widen there.
            scope = {"scope_version": 2, "resources": labels}
            descriptor = {
                "scope_version": 2,
                "node_ids": sorted(node for node, label in mapping.items() if label in labels),
            }
        if not labels or len(labels) > 512:
            return None
    except (TypeError, ValueError, AttributeError, KeyError):
        return None
    token = jwt.encode(
        {
            "iss": ensure_instance_id(db),
            "sub": bot.uuid,
            "aud": signer.audience,
            "iat": issued,
            "exp": expiry,
            **scope,
        },
        signer.secret,
        algorithm="HS256",
    )
    if len(token) > 16384:
        return None
    return {"token": token, "expires_at": expiry, "scope": "metrics:read", **descriptor}
