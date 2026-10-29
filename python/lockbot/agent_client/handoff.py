"""Build final payloads from trusted orchestration policy, not child-Agent prose.

The caller must authenticate context/manifest on the approved origin and retain the
original envelope outside the child's writable state. These helpers do no I/O.
"""

from datetime import datetime, timezone
from urllib.parse import urlsplit

SERVICE_ID = "lockbot.resource-api"
MODES = {"command_only", "public_category", "private_details"}


class GateError(ValueError):
    """Do not send, guess another credential or replay a previous command."""


def _text(value, maximum=512):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise GateError("Expected bounded plain text")
    return value


def _origin(value):
    _text(value, 2048)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(char.isspace() for char in value)
            or parsed.port == 0
        ):
            raise GateError("Expected the verified service origin")
    except ValueError as error:
        raise GateError("Invalid service origin") from error
    return value.rstrip("/")


def scope_from_authenticated_context(origin, context, credential_ref):
    """Record an already authenticated identity; this function cannot verify TLS/key use."""
    if not isinstance(context, dict):
        raise GateError("Missing authenticated context")
    server, bot, actor = context.get("server", {}), context.get("bot", {}), context.get("actor", {})
    if (
        not isinstance(server, dict)
        or server.get("service_id") != SERVICE_ID
        or server.get("api_version") != "1"
        or context.get("credential_scope") != "one_user_one_bot"
        or context.get("agent_access_enabled") is not True
        or not isinstance(bot, dict)
        or type(bot.get("id")) is not int
        or bot["id"] <= 0
        or not isinstance(actor, dict)
    ):
        raise GateError("Unsupported or disabled authenticated scope")
    reference = _text(credential_ref)
    if reference.startswith(("lbr_", "lbk_", "Bearer ")):
        raise GateError("Pass a private-store reference, never a bearer key")
    return {
        "origin": _origin(origin),
        "service_id": SERVICE_ID,
        "instance_id": _text(server.get("instance_id"), 128),
        "bot_uuid": _text(bot.get("uuid"), 128),
        "bot_id": bot["id"],
        "actor": {
            "platform": _text(actor.get("platform"), 64),
            "external_user_id": _text(actor.get("external_user_id")),
        },
        "credential_ref": reference,
    }


def _policy(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "mode"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("mode"), str)
        or value["mode"] not in MODES
    ):
        raise GateError("Unsupported privacy policy")
    return dict(value)


def make_handoff(origin, authenticated_context, credential_ref, policy=None):
    """Create a JSON-serializable envelope; keep an immutable trusted copy in the parent."""
    return {
        "schema_version": 1,
        "scope": scope_from_authenticated_context(origin, authenticated_context, credential_ref),
        "privacy_policy": _policy(policy if policy is not None else {"version": 1, "mode": "command_only"}),
    }


def _envelope(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "scope", "privacy_policy"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("scope"), dict)
    ):
        raise GateError("Unsupported handoff envelope")
    _policy(value["privacy_policy"])
    return value


def _matched_scope(trusted_handoff, origin, context, credential_ref):
    trusted = _envelope(trusted_handoff)
    current = scope_from_authenticated_context(origin, context, credential_ref)
    if trusted["scope"] != current:
        raise GateError("Credential/service/bot/actor scope changed")
    return current


def final_command_payload(trusted_handoff, child_handoff, *, origin, authenticated_context, credential_ref, candidate):
    """Return a fresh allowlisted body; never mutate inputs, send or retry a command.

    The original parent policy is authoritative. A child cannot replace it by
    editing its serialized envelope. Command authorization remains the caller's job.
    """
    trusted = _envelope(trusted_handoff)
    if _envelope(child_handoff) != trusted:
        raise GateError("Child changed the trusted handoff policy or scope")
    _matched_scope(trusted, origin, authenticated_context, credential_ref)
    if not isinstance(candidate, dict):
        raise GateError("Missing command candidate")
    result = {"command": _text(candidate.get("command"))}
    privacy = authenticated_context.get("usage_privacy")
    if not isinstance(privacy, dict):
        return result
    mode = trusted["privacy_policy"]["mode"]
    if mode == "public_category":
        categories = privacy.get("public_task_categories")
        if not isinstance(categories, dict) or not categories:
            return result
        category = candidate.get("task_category")
        if category is not None:
            if not isinstance(category, str) or category not in categories:
                raise GateError("Choose a public category ID from authenticated context")
            result["task_category"] = category
    elif mode == "private_details":
        fields = privacy.get("private_fields")
        if (
            privacy.get("legacy_purpose_visibility") != "private"
            or not isinstance(fields, list)
            or "purpose" not in fields
            or "agent_label" not in fields
        ):
            return result
        for field, maximum in (("purpose", 512), ("agent_label", 64)):
            if candidate.get(field) is not None:
                result[field] = _text(candidate[field], maximum)
    return result


def _instant(value):
    try:
        instant = datetime.fromisoformat(_text(value, 64).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("Timezone required")
        return instant.astimezone(timezone.utc)
    except (ValueError, TypeError) as error:
        raise GateError("Expected an absolute lease timestamp") from error


def lease_matches(trusted_handoff, *, origin, authenticated_context, credential_ref, resource_id, saved, holder, now):
    """Compare this task's saved evidence with a fresh resource holder; no release I/O.

    Current APIs expose start_time; holding_id is additionally checked when present
    in saved evidence. No public Agent label is used as an ownership boundary.
    """
    scope = _matched_scope(trusted_handoff, origin, authenticated_context, credential_ref)
    if not isinstance(saved, dict) or not isinstance(holder, dict):
        return False
    actor = scope["actor"]
    if (
        saved.get("resource_id") != _text(resource_id)
        or saved.get("actor") != actor
        or holder.get("platform") != actor["platform"]
        or holder.get("external_user_id") != actor["external_user_id"]
        or _instant(saved.get("start_time")) != _instant(holder.get("start_time"))
        or _instant(holder.get("expires_at")) <= _instant(now)
    ):
        return False
    return saved.get("holding_id") is None or saved["holding_id"] == holder.get("holding_id")


def feedback_url(trusted_handoff, *, manifest_origin, verified_manifest):
    """Return only the verified manifest's feedback entry; never submit or upload anything."""
    scope = _envelope(trusted_handoff)["scope"]
    if (
        _origin(manifest_origin) != scope.get("origin")
        or not isinstance(verified_manifest, dict)
        or verified_manifest.get("service_id") != scope.get("service_id")
        or verified_manifest.get("instance_id") != scope.get("instance_id")
    ):
        raise GateError("Feedback manifest does not match the verified service")
    url = _text(verified_manifest.get("issues_url"), 2048)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise GateError("Invalid feedback entry") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in url
        or any(char.isspace() for char in url)
        or port == 0
    ):
        raise GateError("Invalid feedback entry")
    return url
