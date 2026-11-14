"""Explicitly pinned, secret-free metadata for one already-proven native connection."""

import re
from datetime import datetime, timezone
from uuid import UUID

FIELDS = {
    "service_id",
    "api_version",
    "service_origin",
    "instance_id",
    "bot",
    "actor",
    "identity_revision",
    "expires_at",
    "credential_recovery_health",
}
SECRET = re.compile(r"\b(?:lbr_|lbk_|lba1\.)[A-Za-z0-9_.-]+")


def text(value, limit=256):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or SECRET.search(value)
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid connection metadata")
    return value


def pinned_services(origins, pins, allow_private_http=False):
    from .router import panel_origin

    if (
        not isinstance(origins, list)
        or not 0 < len(origins) <= 16
        or len(set(origins)) != len(origins)
        or not isinstance(pins, dict)
        or set(origins) != set(pins)
    ):
        raise ValueError("Explicit service origins and instance pins are required")
    result = {}
    for origin in origins:
        panel_origin(origin, allow_private_http)
        result[origin] = text(pins[origin], 128)
    return result


def project(raw, grant, pins):
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ValueError("Unexpected connection metadata fields")
    origin = raw["service_origin"]
    if (
        raw["service_id"] != "lockbot.resource-api"
        or raw["api_version"] != "1"
        or origin not in pins
        or raw["instance_id"] != pins[origin]
    ):
        raise ValueError("Native service identity is not pinned")
    actor, bot = raw["actor"], raw["bot"]
    if (
        not isinstance(actor, dict)
        or set(actor) != {"platform", "external_user_id"}
        or actor != {"platform": grant["platform"], "external_user_id": grant["subject"]}
    ):
        raise ValueError("Actor does not match the proven grant")
    if (
        not isinstance(bot, dict)
        or set(bot) != {"id", "uuid", "name", "type"}
        or type(bot["id"]) is not int
        or bot["id"] <= 0
    ):
        raise ValueError("Invalid bot metadata")
    UUID(text(bot["uuid"], 128))
    if bot["type"] not in {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}:
        raise ValueError("Unsupported native bot type")
    for value in actor.values():
        text(value)
    text(bot["name"], 128)
    if raw["identity_revision"] is not None and (
        type(raw["identity_revision"]) is not int or raw["identity_revision"] < 0
    ):
        raise ValueError("Invalid identity revision")
    expiry = raw["expires_at"]
    if expiry is not None:
        when = datetime.fromisoformat(text(expiry, 64).replace("Z", "+00:00"))
        if when.tzinfo is None or when <= datetime.now(timezone.utc):
            raise ValueError("Connection metadata expired")
    if raw["credential_recovery_health"] not in {"recoverable", "legacy_missing", "unreadable", "unknown"}:
        raise ValueError("Invalid recovery status")
    return {**raw, "bot": dict(bot), "actor": dict(actor)}
