"""Issue short read-only data-source grants; never query or proxy hardware metrics."""

import json
import time
from datetime import timezone

import jwt

from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.metrics_signers import select_metrics_signer


def issue_metrics_access(db, bot, source=None, *, expires_at=None):
    source = source or public_datasource(bot)
    if not source or source.get("accessMode", "independent") != "short_lived":
        return None
    signer = select_metrics_signer(source.get("endpoint"))
    if signer is None:
        return None
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id

    now = int(time.time())
    issued = now // 30 * 30
    expiry = issued + 300
    if expires_at is not None:
        expiry = min(expiry, int(expires_at.replace(tzinfo=timezone.utc).timestamp()))
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
        nodes = sorted(set(mapping.values()))
        if not nodes or len(nodes) > 512:
            return None
    except (TypeError, ValueError, AttributeError):
        return None
    token = jwt.encode(
        {
            "iss": ensure_instance_id(db),
            "sub": bot.uuid,
            "aud": signer.audience,
            "iat": issued,
            "exp": expiry,
            "nodes": nodes,
        },
        signer.secret,
        algorithm="HS256",
    )
    return {"token": token, "expires_at": expiry, "scope": "metrics:read"}
