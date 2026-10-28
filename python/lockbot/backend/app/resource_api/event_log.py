"""Bounded per-bot JSONL history for verified Agent operations; never a DB audit sink."""

import json
import logging
import os
import re
import stat
import threading
from datetime import datetime, timezone

from lockbot.backend.app.managed_paths import ManagedPathError, get_managed_paths
from lockbot.core.handler import redact_command_text

logger = logging.getLogger(__name__)
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 9
_write_lock = threading.Lock()
_secrets = re.compile(
    r"(?:lbr_|lbk_)[A-Za-z0-9_-]+|lbv1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?i:Bearer\s+\S+)"
)
_secret_values = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|password|authorization)\s*[:=]\s*)\S+")


def _safe(value):
    if isinstance(value, str):
        return _secret_values.sub(r"\1[redacted]", _secrets.sub("[redacted]", value))
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value[:100]]
    return value


def begin_event(request, *, bot=None, operation, command=None, agent_label=None, purpose=None, resource_id=None):
    """Only validated request fields, never a raw request body or Authorization header."""
    request.state.agent_event = {
        "operation": operation,
        "command": redact_command_text(command) if command is not None else None,
        "agent_label": agent_label,
        "purpose": purpose,
        "requested_resources": [resource_id] if resource_id is not None else [],
        "bot_uuid": getattr(bot, "uuid", None),
        "bot_type": getattr(bot, "bot_type", None),
        "stage": "received",
    }


def describe_targets(bot, actor, word, text):
    """Describe requested targets using the original parser, without changing its language."""
    if word not in {"lock", "slock", "book", "free", "unlock"}:
        return []
    try:
        parsed = bot.parse_command(actor.external_user_id, word, text, word in {"lock", "slock", "book"})
        if not parsed[0]:
            return []
        if len(parsed) == 5:
            return [
                {"resource_id": node, "device_ids": list(cards)}
                for node, cards in zip(parsed[2], parsed[3], strict=True)
            ]
        return [{"resource_id": node} for node in parsed[2]]
    except Exception:
        # Observability must not alter dispatch or mask the command's own parser reply.
        return []


def event_for_request(request):
    event = getattr(request.state, "agent_event", None)
    if event is not None:
        return event
    # Authentication can succeed before body validation or an access-switch refusal.
    # Attribute the status without inspecting or retaining an invalid raw body.
    if request.method == "POST" and request.url.path.endswith("/commands"):
        return {"operation": "command", "stage": "received"}
    if request.method in {"PUT", "DELETE"} and "/usage-declarations/" in request.url.path:
        return {"operation": "usage.update" if request.method == "PUT" else "usage.delete", "stage": "received"}
    return None


def observe_own_resources(bot, actor, targets, *, all_owned=False):
    """A timestamped observation, not a claim that the request caused every state change."""
    try:
        captured_at, state = bot.state_snapshot()
        selected = targets or ([{"resource_id": node} for node in state] if all_owned else [])
        resources = []
        for target in selected:
            key = target["resource_id"]
            node = state.get(key)
            cards = target.get("device_ids")
            # QUEUE_DEVICE uses card IDs while preserving the DEVICE state layout.
            if node is None and ":dev" in key:
                parent, index = key.rsplit(":dev", 1)
                parent_state = state.get(parent)
                if index.isdigit() and isinstance(parent_state, list) and int(index) < len(parent_state):
                    node, cards = parent_state, [int(index)]
            entries = enumerate(node) if isinstance(node, list) else [(None, node)]
            for card, entry in entries:
                if not isinstance(entry, dict) or (cards is not None and card not in cards):
                    continue
                observed = {"resource_id": key, "device_id": card}
                for source, destination in (("current_users", "holders"), ("booking_list", "bookings")):
                    observed[destination] = [
                        {field: user.get(field) for field in ("holding_id", "start_time", "duration", "source")}
                        for user in entry.get(source, [])
                        if user.get("user_id") == actor.external_user_id
                    ]
                if not all_owned or observed["holders"] or observed["bookings"]:
                    resources.append(observed)
        return {"captured_at": captured_at.isoformat(), "resources": resources}
    except Exception:
        return None


def write_event(*, actor, event, status, duration_ms):
    """Best-effort, private, size-rotated history. HTTP 200 does not prove a lock succeeded."""
    stage = event.get("stage", "received")
    if status >= 400:
        outcome = "may_have_executed" if stage in {"dispatching", "dispatched", "committed"} else "rejected"
    else:
        outcome = "dispatched" if event.get("operation") == "command" else "applied"
    entry = _safe(
        {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_id": actor.bot_id,
            "actor": {"platform": actor.platform, "external_user_id": actor.external_user_id},
            "credential_id": actor.binding_record_id,
            **{key: value for key, value in event.items() if key != "stage"},
            "http_status": status,
            "outcome": outcome,
            "duration_ms": round(max(0, duration_ms), 2),
        }
    )
    body = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    try:
        with _write_lock:
            paths = get_managed_paths()
            paths.bot_dir(actor.bot_id).mkdir(parents=True, exist_ok=True, mode=0o700)

            def path(index=0):
                name = "agent-operations.jsonl" + (f".{index}" if index else "")
                return paths.bot_file(actor.bot_id, name)

            current = path()
            if current.exists() and current.stat().st_size + len(body) > MAX_BYTES:
                # Check every rotation target before moving anything.
                files = [path(index) for index in range(BACKUPS + 1)]
                files[-1].unlink(missing_ok=True)
                for index in range(BACKUPS - 1, -1, -1):
                    if files[index].exists():
                        files[index].replace(files[index + 1])
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
            descriptor = os.open(path(), flags, 0o600)
            with os.fdopen(descriptor, "ab") as output:
                info = os.fstat(output.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ManagedPathError("Agent history is not a private regular file")
                os.fchmod(output.fileno(), 0o600)
                output.write(body)
    except Exception:
        logger.warning("Bot %s: could not append Agent operation history", actor.bot_id, exc_info=True)
