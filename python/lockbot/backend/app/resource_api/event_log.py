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
    r"/#/r/[0-9]+/[A-Za-z0-9_-]{22}|(?:lbr_|lbk_)[A-Za-z0-9_-]+|lbv1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?i:Bearer\s+\S+)"
)
_secret_values = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|password|authorization)\s*[:=]\s*)\S+")


def _safe(value, depth=0):
    if depth >= 20:
        return "[truncated]"
    if isinstance(value, str):
        return _secret_values.sub(r"\1[redacted]", _secrets.sub("[redacted]", value))
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if key.lower()
                in {
                    "authorization",
                    "password",
                    "app_secret",
                    "api_key",
                    "credential_digest",
                    "access_token",
                    "refresh_token",
                    "token",
                    "encryption_key",
                }
                else _safe(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value[:100]]
    return value


def begin_event(
    request, *, bot=None, operation, command=None, agent_label=None, purpose=None, resource_id=None, task_category=None
):
    """Only validated request fields, never a raw request body or Authorization header."""
    request.state.agent_event = {
        "operation": operation,
        "command": redact_command_text(command) if command is not None else None,
        "agent_label": agent_label,
        "purpose": purpose,
        "task_category": task_category,
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
            "credential_ref": actor.binding_public_id,
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


# Reading is bounded independently of the size/rotation policy used by the writer.
READ_BUDGET = 1024 * 1024
MAX_LINE_BYTES = 64 * 1024


def read_history(bot_id, *, limit=25, cursor=None, query=""):
    """Newest first, inode/offset cursor: appends do not shift subsequent pages.

    A rotated-away cursor fails explicitly rather than silently skipping history. File
    names are always selected by the server; cursor contents never become a path.
    """
    import base64

    from fastapi import HTTPException

    paths = get_managed_paths()
    handles = []
    try:
        for index in range(BACKUPS + 1):
            candidate = paths.bot_file(bot_id, "agent-operations.jsonl" + (f".{index}" if index else ""))
            try:
                fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            handle = os.fdopen(fd, "rb")
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                handle.close()
                raise ManagedPathError("Agent history is not a private regular file")
            if any(inode == info.st_ino for inode, _, _ in handles):
                handle.close()
                continue
            handles.append((info.st_ino, info.st_size, handle))
        start_index, end = 0, None
        if cursor:
            try:
                inode, end = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                assert type(inode) is int and type(end) is int and end >= 0
            except Exception:
                raise HTTPException(400, "Invalid history cursor") from None
            found = [i for i, (ino, size, _) in enumerate(handles) if ino == inode and end <= size]
            if not found:
                raise HTTPException(409, "History rotated; refresh the first page")
            start_index = found[0]
        items, scanned, next_position = [], 0, None
        needle = query.casefold()
        for index in range(start_index, len(handles)):
            inode, size, handle = handles[index]
            stop = end if index == start_index and end is not None else size
            while stop > 0 and scanned < READ_BUDGET:
                start = max(0, stop - (READ_BUDGET - scanned))
                handle.seek(start)
                body = handle.read(stop - start)
                scanned += len(body)
                # Do not parse a partial line at a bounded chunk's beginning.
                if start:
                    handle.seek(start - 1)
                    if handle.read(1) != b"\n":
                        boundary = body.find(b"\n")
                        if boundary < 0:
                            stop = start
                            break
                        body, start = body[boundary + 1 :], start + boundary + 1
                offsets, position = [], start
                for raw in body.splitlines(keepends=True):
                    offsets.append((position, raw))
                    position += len(raw)
                for position, raw in reversed(offsets):
                    stop = position
                    if not raw.endswith(b"\n") or len(raw) > MAX_LINE_BYTES:
                        continue
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if not isinstance(row, dict) or row.get("bot_id") != bot_id:
                        continue
                    # Only event-schema fields are exposed; redact again for older files.
                    row = _safe(
                        {
                            key: row[key]
                            for key in (
                                "timestamp",
                                "operation",
                                "command",
                                "agent_label",
                                "purpose",
                                "actor",
                                "credential_id",
                                "credential_ref",
                                "requested_resources",
                                "observed_state",
                                "reply",
                                "http_status",
                                "outcome",
                                "duration_ms",
                            )
                            if key in row
                        }
                    )
                    if isinstance(row.get("command"), str):
                        row["command"] = redact_command_text(row["command"])
                    if needle and needle not in json.dumps(row, ensure_ascii=False).casefold():
                        continue
                    row["id"] = f"{inode}:{position}"
                    items.append(row)
                    if len(items) >= limit:
                        next_position = (inode, position)
                        break
                if next_position:
                    break
                stop = start
            if next_position:
                break
            if stop > 0:
                next_position = (inode, stop)
                break
            if scanned >= READ_BUDGET and index + 1 < len(handles):
                next_position = handles[index + 1][:2]
                break
        next_cursor = None
        if next_position:
            next_cursor = base64.urlsafe_b64encode(json.dumps(next_position).encode()).decode().rstrip("=")
        return {
            "items": items,
            "next_cursor": next_cursor,
            "scanned_bytes": scanned,
            "retention": {"max_file_bytes": MAX_BYTES, "files": BACKUPS + 1},
        }
    except (OSError, ManagedPathError):
        raise HTTPException(503, "Agent history is temporarily unavailable") from None
    finally:
        for _, _, handle in handles:
            handle.close()
