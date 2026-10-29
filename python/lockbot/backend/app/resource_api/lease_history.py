"""Public allocator lifecycle evidence, separate from Agent requests and private notes."""

import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid

from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.core.command_context import command_action, command_source

LOG_NAME = "resource-lifecycle.jsonl"
MAX_LINE_BYTES = 512 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
BACKUPS = 9
MAX_ENTRIES = 2048
BOUNDARIES = {
    "session_start",
    "state_persist_failed",
    "journal_write_failed",
    "invalid_state",
    "clock_reversal",
    "unconfirmed_mutation",
    "outbox_overflow",
}
SOURCES = {"chat", "api", "operator", "admin", "internal"}
ACTIONS = {
    "lock",
    "slock",
    "unlock",
    "book",
    "take",
    "kickout",
    "kicklock",
    "opkick",
    "alias",
    "state_edit",
    "query",
    "help",
}
logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def number(value):
    return type(value) in (int, float) and 0 <= value <= 253402300799 and math.isfinite(value)


def canonical(state, *, platform, bot_type, claim_seconds=300):
    """Only allocator facts; never copy arbitrary user/state/config fields."""
    result = {"allocations": [], "bookings": [], "complete": True}
    count = 0
    seen = set()
    if not isinstance(state, dict):
        return {"allocations": [], "bookings": [], "complete": False}
    for node, value in state.items():
        count += 1
        if count > MAX_ENTRIES or not isinstance(node, str) or not 0 < len(node) <= 128:
            result["complete"] = False
            break
        entries = enumerate(value) if isinstance(value, list) else [(None, value)]
        for index, entry in entries:
            count += 1
            if count > MAX_ENTRIES:
                return {"allocations": [], "bookings": [], "complete": False}
            if not isinstance(entry, dict):
                result["complete"] = False
                continue
            if index is not None and (type(entry.get("dev_id", index)) is not int or entry.get("dev_id", index) < 0):
                result["complete"] = False
                continue
            resource = f"{node}:dev{entry.get('dev_id', index)}" if index is not None else node
            if len(resource) > 160:
                result["complete"] = False
                continue
            for field, destination in (("current_users", "allocations"), ("booking_list", "bookings")):
                users = entry.get(field, [])
                if not isinstance(users, list):
                    result["complete"] = False
                    continue
                for user in users:
                    count += 1
                    if count > MAX_ENTRIES:
                        return {"allocations": [], "bookings": [], "complete": False}
                    if not isinstance(user, dict):
                        result["complete"] = False
                        continue
                    uid, identity = user.get("user_id"), user.get("holding_id")
                    start, duration = user.get("start_time"), user.get("duration")
                    if (
                        not isinstance(uid, str)
                        or not 0 < len(uid) <= 256
                        or not isinstance(identity, str)
                        or not 0 < len(identity) <= 128
                        or not number(start)
                        or not number(duration)
                        or not number(start + duration)
                    ):
                        result["complete"] = False
                        continue
                    item = {
                        "ref": digest([platform, bot_type, resource, destination, identity]),
                        "resource": resource,
                        "actor": {"platform": platform, "external_user_id": uid},
                        "unit": "device" if index is not None else "node",
                        "mode": entry.get("status")
                        if isinstance(entry.get("status"), str) and entry.get("status") in {"exclusive", "shared"}
                        else "exclusive",
                        "start": start,
                        "deadline": start + duration,
                        "origin": user.get("source")
                        if isinstance(user.get("source"), str) and user.get("source") in {"chat", "api", "service_key"}
                        else "unknown",
                    }
                    if item["ref"] in seen:
                        result["complete"] = False
                        continue
                    seen.add(item["ref"])
                    if destination == "bookings":
                        item["notified"] = bool(user.get("is_notified"))
                        item["deadline"] = start + claim_seconds if item["notified"] else None
                    result[destination].append(item)
    for field in ("allocations", "bookings"):
        result[field].sort(key=lambda item: item["ref"])
    return result


def transitions(before, after, now):
    events = []
    old = {item["ref"]: item for item in before["allocations"]}
    new = {item["ref"]: item for item in after["allocations"]}
    booked = {(item["resource"], json.dumps(item["actor"], sort_keys=True)): item for item in before["bookings"]}
    claimed = set()
    for ref, item in new.items():
        if ref not in old:
            booking = booked.get((item["resource"], json.dumps(item["actor"], sort_keys=True)))
            event = {"kind": "claimed" if booking else "acquired", "lease_ref": ref, "resource": item["resource"]}
            if booking:
                event["booking_ref"] = booking["ref"]
                claimed.add(booking["ref"])
            events.append(event)
        elif old[ref]["deadline"] != item["deadline"]:
            events.append(
                {"kind": "renewed", "lease_ref": ref, "resource": item["resource"], "late": now > old[ref]["deadline"]}
            )
        elif old[ref]["mode"] != item["mode"]:
            events.append({"kind": "mode_changed", "lease_ref": ref, "resource": item["resource"]})
    for ref, item in old.items():
        if ref not in new:
            events.append(
                {
                    "kind": "expired" if now >= item["deadline"] else "released",
                    "lease_ref": ref,
                    "resource": item["resource"],
                }
            )
    old_book = {item["ref"]: item for item in before["bookings"]}
    new_book = {item["ref"]: item for item in after["bookings"]}
    for ref, item in new_book.items():
        if ref not in old_book:
            events.append({"kind": "booked", "booking_ref": ref, "resource": item["resource"]})
        elif item["notified"] and not old_book[ref]["notified"]:
            events.append({"kind": "claim_ready", "booking_ref": ref, "resource": item["resource"]})
    for ref, item in old_book.items():
        if ref not in new_book and ref not in claimed:
            kind = "claim_timeout" if item["deadline"] is not None and now >= item["deadline"] else "booking_removed"
            events.append({"kind": kind, "booking_ref": ref, "resource": item["resource"]})
    return events


class LeaseJournal:
    """Synchronous bounded local append only. Failure never re-executes a resource command."""

    def __init__(self, bot_id, bot):
        self.bot_id, self.bot = bot_id, bot
        self.session, self.sequence = uuid.uuid4().hex, 0
        self.lock = threading.Lock()
        self.state = {"allocations": [], "bookings": [], "complete": True}
        self.gap, self.timestamp = "session_start", 0
        self._prepared = None
        from lockbot.backend.app.resource_api.lease_contract import DurableOutbox

        self.outbox = DurableOutbox(self)
        # A baseline is persisted with the same atomic state payload, including on restart.
        from lockbot.core.io import _save_bot_state_atomically

        evidence = self.prepare_state_write(bot.state.bot_state)
        try:
            _save_bot_state_atomically(bot.state.bot_state, bot.config, evidence=evidence)
        except Exception:
            self.failed()
            raise
        self.committed(bot.state.bot_state)

    _digest = staticmethod(digest)

    def _canonical(self, data):
        platform = str(self.bot.adapter.platform_name).lower()
        return canonical(
            data,
            platform=platform,
            bot_type=self.bot.config.get_val("BOT_TYPE"),
            claim_seconds=300,
        )

    def _append(self, frame):
        body = b"\n" + (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(body) > MAX_LINE_BYTES:
            raise ValueError("Lifecycle snapshot exceeds bounded line size")
        paths = get_managed_paths()
        paths.bot_dir(self.bot_id).mkdir(parents=True, exist_ok=True, mode=0o700)
        files = [paths.bot_file(self.bot_id, LOG_NAME + (f".{i}" if i else "")) for i in range(BACKUPS + 1)]
        for file in files:
            if file.exists():
                info = file.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Lifecycle file is not private regular storage")
        if files[0].exists() and files[0].stat().st_size + len(body) > MAX_FILE_BYTES:
            files[-1].unlink(missing_ok=True)
            for index in range(BACKUPS - 1, -1, -1):
                if files[index].exists():
                    files[index].replace(files[index + 1])
        fd = os.open(
            files[0], os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, 0o600
        )
        with os.fdopen(fd, "ab") as output:
            info = os.fstat(output.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Lifecycle file is not private regular storage")
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(paths.bot_dir(self.bot_id), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def unconfirmed(self, data):
        with self.lock:
            if self.gap is None and self._canonical(data) != self.state:
                self.gap = "unconfirmed_mutation"

    def failed(self):
        with self.lock:
            if self._prepared is not None:
                _, pending, sequence = self._prepared
                self.outbox.pending, self.outbox.sequence = pending, sequence
                self._prepared = None
            self.gap = "state_persist_failed"

    def _frame(self, data, *, atomic):
        now = time.time()
        current = self._canonical(data)
        boundary = self.gap
        if not current["complete"]:
            boundary = "invalid_state"
        if now < self.timestamp:
            boundary = "clock_reversal"
        source, action = command_source.get(), command_action.get()
        frame = {
            "schema": "lockbot.resource-lifecycle",
            "schema_version": 1,
            "bot_id": self.bot_id,
            "session": self.session,
            "sequence": self.sequence + 1,
            "timestamp": now,
            "before_digest": digest(self.state),
            "after_digest": digest(current),
            "boundary": boundary,
            "state": current,
            "source": source if source in SOURCES else "internal",
            "action": action if action in ACTIONS else None,
            "events": transitions(self.state, current, now) if not boundary else [],
        }
        before = {item["ref"]: item for field in ("allocations", "bookings") for item in self.state[field]}
        for event in frame["events"]:
            old = before.get(event.get("lease_ref") or event.get("booking_ref"))
            event["occurred_at"] = old["deadline"] if old and event["kind"] in {"expired", "claim_timeout"} else now
        return self.outbox.decorate(frame, atomic=atomic)

    def prepare_state_write(self, data):
        """Stage evidence before the core atomically replaces the allocator state."""
        with self.lock:
            if self._prepared is not None:
                raise RuntimeError("Lifecycle state write is already pending")
            pending, sequence = list(self.outbox.pending), self.outbox.sequence
            frame = self._frame(data, atomic=True)
            self._prepared = frame, pending, sequence
            return self.outbox.envelope(frame)

    def committed(self, data):
        with self.lock:
            if self._prepared is not None:
                frame = self._prepared[0]
                self._prepared = None
            else:
                # External/legacy persistence hooks cannot assert atomic coupling.
                frame = self._frame(data, atomic=False)
                self.outbox.envelope(frame)
            current = self._canonical(data)
            if digest(current) != frame["after_digest"]:
                self.gap = "unconfirmed_mutation"
                return
            self.sequence += 1
            self.outbox.acknowledged_state(frame)
            try:
                self.outbox.flush()
                self.gap = None if current["complete"] else "invalid_state"
                self.timestamp = frame["timestamp"]
            except Exception:
                self.gap = "journal_write_failed"
                logger.warning("Bot %s lifecycle evidence append failed", self.bot_id)
            self.state = current

    def checkpoint(self, data):
        with self.lock:
            current = self._canonical(data)
            if len(json.dumps(current, ensure_ascii=False).encode()) > MAX_LINE_BYTES:
                current = {"allocations": [], "bookings": [], "complete": False}
            if current != self.state and self.gap is None:
                self.gap = "unconfirmed_mutation"
            return {
                "contract": {
                    "version": 2,
                    "journal_id": self.outbox.journal_id,
                    "commit_sequence": self.outbox.sequence,
                    "pending_frames": len(self.outbox.pending),
                },
                "integrity_boundary": self.gap,
                "session": self.session,
                "sequence": self.sequence,
                "timestamp": time.time(),
                "after_digest": digest(current),
                "state": current,
                "consistent": not self.gap and current["complete"] and current == self.state,
            }


def public_frame(row):
    """A closed public schema, not a way to expose future raw event fields."""
    if (
        row.get("schema") != "lockbot.resource-lifecycle"
        or type(row.get("schema_version")) is not int
        or row["schema_version"] != 1
        or type(row.get("bot_id")) is not int
        or row["bot_id"] <= 0
        or type(row.get("sequence")) is not int
        or row["sequence"] < 1
        or not number(row.get("timestamp"))
        or not re.fullmatch(r"[a-f0-9]{32}", str(row.get("session", "")))
    ):
        return None
    # The writer stores only public values. Validate every nested state value again for imported/legacy files.
    state = row.get("state")
    if not isinstance(state, dict) or type(state.get("complete")) is not bool:
        return None
    safe = {"allocations": [], "bookings": [], "complete": state["complete"]}
    seen = set()
    for field in ("allocations", "bookings"):
        items = state.get(field)
        if not isinstance(items, list) or len(items) > MAX_ENTRIES:
            return None
        for item in items:
            if not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("ref", ""))):
                return None
            actor = item.get("actor")
            if (
                not isinstance(actor, dict)
                or not all(
                    isinstance(actor.get(k), str) and 0 < len(actor[k]) <= 256 for k in ("platform", "external_user_id")
                )
                or not isinstance(item.get("resource"), str)
                or not 0 < len(item["resource"]) <= 160
                or not isinstance(item.get("unit"), str)
                or item.get("unit") not in {"node", "device"}
                or not isinstance(item.get("mode"), str)
                or item.get("mode") not in {"exclusive", "shared"}
                or not number(item.get("start"))
                or (item.get("deadline") is not None and not number(item["deadline"]))
            ):
                return None
            if (
                (field == "allocations" and item.get("deadline") is None)
                or (item.get("deadline") is not None and item["deadline"] < item["start"])
                or item["ref"] in seen
            ):
                return None
            seen.add(item["ref"])
            clean = {key: item[key] for key in ("ref", "resource", "unit", "mode", "start", "deadline")}
            clean["actor"] = {key: actor[key] for key in ("platform", "external_user_id")}
            clean["origin"] = (
                item.get("origin")
                if isinstance(item.get("origin"), str) and item.get("origin") in {"chat", "api", "service_key"}
                else "unknown"
            )
            if field == "bookings":
                clean["notified"] = bool(item.get("notified"))
            safe[field].append(clean)
    if digest(safe) != row.get("after_digest") or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("before_digest", ""))):
        return None
    boundary = row.get("boundary")
    if boundary is not None and (not isinstance(boundary, str) or boundary not in BOUNDARIES):
        return None
    result = {
        "schema": row["schema"],
        "schema_version": 1,
        "bot_id": row["bot_id"],
        "session": row["session"],
        "sequence": row["sequence"],
        "timestamp": row["timestamp"],
        "state": safe,
        "before_digest": row["before_digest"],
        "after_digest": row["after_digest"],
        "boundary": boundary,
        "source": row.get("source")
        if isinstance(row.get("source"), str) and row.get("source") in SOURCES
        else "internal",
        "action": row.get("action") if isinstance(row.get("action"), str) and row.get("action") in ACTIONS else None,
    }
    kinds = {
        "acquired",
        "renewed",
        "released",
        "expired",
        "booked",
        "claim_ready",
        "claimed",
        "claim_timeout",
        "booking_removed",
        "mode_changed",
    }
    result["events"] = []
    events = row.get("events", [])
    for event in events[: MAX_ENTRIES * 2] if isinstance(events, list) else []:
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str) or event.get("kind") not in kinds:
            continue
        clean = {"kind": event["kind"]}
        for key in ("lease_ref", "booking_ref"):
            if isinstance(event.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", event[key]):
                clean[key] = event[key]
        if isinstance(event.get("resource"), str):
            clean["resource"] = event["resource"][:160]
        if type(event.get("late")) is bool:
            clean["late"] = event["late"]
        result["events"].append(clean)
    return result
