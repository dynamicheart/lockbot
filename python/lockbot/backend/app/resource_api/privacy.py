"""Task disclosure is separate from bot management and resource-operation authority."""

import re
import sys

from sqlalchemy import select

from lockbot.backend.app.auth.models import User

PUBLIC_DECLARATION_FIELDS = ("resource_id", "actor", "task_category", "public_task", "updated_at", "expires_at")
_PUBLIC_OPERATIONS = frozenset({"lock", "slock", "book", "unlock", "alias", "query", "help", "opkick", "kicklock"})


def can_read_private_tasks(db, user):
    """Read current DB authority, not a cached role or a matching IM display name.

    A management account has no verified IM identity mapping. Even a bot owner with
    the same username as an actor must use that actor's personal key for self-access.
    Ordinary OP credentials never call this function as a privileged user.
    """
    return (
        user is not None
        and db.execute(select(User.role).where(User.id == user.id)).scalar_one_or_none() == "super_admin"
    )


def audit_private_read(db, user, *, bot_id, surface):
    """Audit elevated disclosure without copying any task body or credential ID."""
    import json

    from fastapi import HTTPException

    from lockbot.backend.app.audit.models import AuditLog

    if not can_read_private_tasks(db, user):
        raise HTTPException(403, "Private task access requires super administrator")
    db.add(
        AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role="super_admin",
            action="task_private.read",
            target_type="bot",
            target_id=bot_id,
            detail=json.dumps({"surface": surface}),
            result="success",
        )
    )
    db.commit()


def visible_declarations(items, *, caller_id=None, private=False):
    """Allowlist public fields; caller_id must come from a verified ResourceActor."""
    return [
        item
        if private or (caller_id is not None and item["actor"]["external_user_id"] == caller_id)
        else {key: item[key] for key in PUBLIC_DECLARATION_FIELDS if key in item}
        for item in items
    ]


def public_command(command):
    """An operation label, not a copy of free-form command or alias text."""
    from lockbot.core.handler import canonical_command

    if not isinstance(command, str) or not command.strip():
        return None
    word = canonical_command(command.split()[0].lower())
    return word if word in _PUBLIC_OPERATIONS else None


def public_event(entry):
    """Shared safe history projection. Apply BEFORE search, pagination or export.

    Reply text, arbitrary fields, binding IDs, Agent labels, purposes, observed
    holding proofs and raw commands are deliberately absent. Future private fields
    remain hidden unless this allowlist explicitly adds them.
    """
    from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES

    result = {}
    for key in ("schema_version", "bot_id", "http_status"):
        value = entry.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    for key, allowed in (
        ("operation", {"command", "usage.update", "usage.delete"}),
        ("outcome", {"dispatched", "applied", "rejected", "may_have_executed"}),
        ("bot_type", {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}),
    ):
        value = entry.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, str):
        result["timestamp"] = timestamp[:64]
    duration = entry.get("duration_ms")
    # Comparing before any float conversion also rejects enormous legacy integers safely.
    if type(duration) in (int, float) and 0 <= duration <= sys.float_info.max:
        result["duration_ms"] = duration
    actor = entry.get("actor")
    if isinstance(actor, dict):
        result["actor"] = {
            key: actor[key][:256] for key in ("platform", "external_user_id") if isinstance(actor.get(key), str)
        }
    result["command"] = public_command(entry.get("command"))
    category = entry.get("task_category")
    if isinstance(category, str) and category in TASK_CATEGORIES:
        result.update(task_category=category, public_task=TASK_CATEGORIES[category])
    previous = entry.get("previous_task_category")
    if isinstance(previous, str) and previous in TASK_CATEGORIES:
        result.update(previous_task_category=previous, previous_public_task=TASK_CATEGORIES[previous])
    targets = []
    requested = entry.get("requested_resources")
    for target in requested[:100] if isinstance(requested, list) else []:
        if isinstance(target, str):
            target = {"resource_id": target}
        if not isinstance(target, dict) or not isinstance(target.get("resource_id"), str):
            continue
        item = {"resource_id": target["resource_id"][:256]}
        if isinstance(target.get("device_ids"), list):
            item["device_ids"] = [i for i in target["device_ids"][:100] if type(i) is int and i >= 0]
        targets.append(item)
    result["requested_resources"] = targets
    return result


def public_log_entry(entry):
    """Legacy free-form logs cannot be reliably scrubbed by looking for field names.

    Retain operational timestamps/status and verified command source, but only
    reconstruct the actor and fixed command word from command records. Raw log
    details are available to an elevated management reader only.
    """
    result = {}
    if type(entry.get("id")) is int:
        result["id"] = entry["id"]
    if isinstance(entry.get("created_at"), str):
        result["created_at"] = entry["created_at"][:64]
    for key, allowed in (
        ("level", {"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "UNKNOWN"}),
        ("category", {"command", "system"}),
        ("source", {"chat", "api", "service_key"}),
    ):
        value = entry.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    message = entry.get("message")
    match = re.fullmatch(r"\[([^\]\r\n]{1,256})\]\s+(.+)", message) if isinstance(message, str) else None
    word = public_command(match[2]) if match and entry.get("category") == "command" else None
    result["message"] = f"[{match[1]}] {word}" if word else "—"
    result["details_restricted"] = True
    return result
