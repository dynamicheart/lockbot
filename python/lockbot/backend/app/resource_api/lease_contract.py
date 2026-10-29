"""Versioned, public durable evidence embedded in the allocator's atomic state file.

The outbox is bounded. Exhaustion is a persisted integrity boundary, never a claim
that missing history is complete. Public JSONL remains a replayable derivative.
"""

import hashlib
import json
import os
import re
import stat
import uuid

from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.core.command_context import command_correlation, command_initiator

MAX_PENDING_FRAMES = 16
MAX_PENDING_BYTES = 4 * 1024 * 1024
MAX_STATE_FILE_BYTES = 16 * 1024 * 1024
HEX32 = re.compile(r"[a-f0-9]{32}")
HEX64 = re.compile(r"[a-f0-9]{64}")


def initiator(value):
    if not isinstance(value, dict):
        return {"kind": "system"}
    kind = value.get("kind")
    if kind == "system":
        return {"kind": "system"}
    if kind == "platform_user":
        platform, uid = value.get("platform"), value.get("external_user_id")
        if (
            isinstance(platform, str)
            and platform in {"infoflow", "feishu"}
            and isinstance(uid, str)
            and 0 < len(uid) <= 256
        ):
            return {"kind": kind, "platform": platform, "external_user_id": uid}
    if kind in {"management_user", "operator"}:
        field = "user_id" if kind == "management_user" else "bot_id"
        if type(value.get(field)) is int and value[field] > 0:
            return {"kind": kind, field: value[field]}
    return {"kind": "unknown"}


def contract_frame(raw):
    from lockbot.backend.app.resource_api.lease_history import number, public_frame

    if not isinstance(raw, dict):
        return None
    safe = public_frame(raw)
    meta = raw.get("contract") if isinstance(raw, dict) else None
    if safe is None or not isinstance(meta, dict):
        return None
    if (
        type(meta.get("version")) is not int
        or meta.get("version") != 2
        or type(meta.get("commit_sequence")) is not int
        or meta["commit_sequence"] < 1
        or not isinstance(meta.get("journal_id"), str)
        or HEX32.fullmatch(meta["journal_id"]) is None
        or not isinstance(meta.get("frame_id"), str)
        or HEX32.fullmatch(meta["frame_id"]) is None
        or type(meta.get("atomic_state")) is not bool
    ):
        return None
    correlation = meta.get("correlation_id")
    if not isinstance(correlation, str) or HEX32.fullmatch(correlation) is None:
        return None
    versions = meta.get("versions")
    if not isinstance(versions, dict) or len(versions) > 4096:
        return None
    if any(
        not isinstance(k, str) or HEX64.fullmatch(k) is None or type(v) is not int or v < 1 for k, v in versions.items()
    ):
        return None
    safe["contract"] = {
        "version": 2,
        "commit_sequence": meta["commit_sequence"],
        "journal_id": meta["journal_id"],
        "frame_id": meta["frame_id"],
        "atomic_state": meta["atomic_state"],
        "initiator": initiator(meta.get("initiator")),
        "correlation_id": correlation,
        "versions": versions,
    }
    if not isinstance(raw.get("events"), list) or len(safe["events"]) != len(raw["events"]):
        return None
    for index, event in enumerate(safe["events"]):
        ref = event.get("lease_ref") or event.get("booking_ref")
        if ref not in versions:
            return None
        event["event_id"] = hashlib.sha256(f"{meta['frame_id']}:{index}".encode()).hexdigest()
        event["version"] = versions[ref]
        occurred = raw["events"][index].get("occurred_at", safe["timestamp"])
        if not number(occurred) or occurred > safe["timestamp"]:
            return None
        event["occurred_at"] = occurred
        event["recorded_at"] = safe["timestamp"]
        event["outcome"] = "committed"
    return safe


class DurableOutbox:
    def __init__(self, journal):
        self.journal = journal
        self.journal_id, self.sequence, self.versions = uuid.uuid4().hex, 0, {}
        self.pending, self.known = [], set()
        self.recovered = False
        self.retry_scan = False
        self._recover()

    def _recover(self):
        path = get_managed_paths().state_file(self.journal.bot_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_FILE_BYTES:
                raise ValueError("Lifecycle recovery state is not bounded private regular storage")
            body = json.load(source)
        envelope = body.get("lifecycle_outbox") if isinstance(body, dict) else None
        if not isinstance(envelope, dict) or envelope.get("version") != 2:
            return
        if envelope.get("bot_id") != self.journal.bot_id or envelope.get("identity_revision") != self._revision():
            return
        if envelope.get("bot_uuid", "") != self.journal.bot.config.get_val("_LIFECYCLE_BOT_UUID", ""):
            return
        pending = envelope.get("pending")
        if not isinstance(pending, list) or not 1 <= len(pending) <= MAX_PENDING_FRAMES:
            return
        if len(json.dumps(pending).encode()) > MAX_PENDING_BYTES:
            return
        frames = [contract_frame(row) for row in pending]
        if any(row is None or not row["contract"]["atomic_state"] for row in frames):
            return
        for previous, current_frame in zip(frames, frames[1:], strict=False):
            if (
                current_frame["bot_id"] != self.journal.bot_id
                or previous["contract"]["journal_id"] != current_frame["contract"]["journal_id"]
                or previous["contract"]["commit_sequence"] + 1 != current_frame["contract"]["commit_sequence"]
                or previous["after_digest"] != current_frame["before_digest"]
            ):
                return
        if any(frame["bot_id"] != self.journal.bot_id for frame in frames):
            return
        last = frames[-1]
        current = self.journal._canonical(self.journal.bot.state.bot_state)
        if last["after_digest"] != self.journal._digest(current):
            return
        for row in frames:
            for event in row["events"]:
                for key in ("event_id", "version", "recorded_at", "outcome"):
                    event.pop(key, None)
        self.pending = frames
        meta = last["contract"]
        self.journal_id, self.sequence = meta["journal_id"], meta["commit_sequence"]
        current_refs = {v["ref"] for field in ("allocations", "bookings") for v in current[field]}
        self.versions = {k: v for k, v in meta["versions"].items() if k in current_refs}
        # An interrupted directory fsync can leave a visible yet unacknowledged
        # state rename. Make this exact recovered file and its name durable before
        # acknowledging/replaying any derivative journal frame.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as recovered:
            current_info = os.fstat(recovered.fileno())
            if (current_info.st_dev, current_info.st_ino, current_info.st_size) != (
                info.st_dev,
                info.st_ino,
                info.st_size,
            ):
                raise ValueError("Lifecycle recovery state changed before reconciliation")
            os.fsync(recovered.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self._read_known()
        try:
            self.flush()
        except Exception:
            # Evidence remains in the atomic state envelope and is retried on the next commit.
            self.journal.gap = "journal_write_failed"
        self.recovered = True

    def _revision(self):
        return self.journal.bot.config.get_val("_IDENTITY_REVISION", 0)

    def _read_known(self):
        from lockbot.backend.app.resource_api.lease_history import BACKUPS, LOG_NAME, MAX_LINE_BYTES

        budget = MAX_PENDING_BYTES + 2 * MAX_LINE_BYTES
        paths = get_managed_paths()
        pending_ids = {frame["contract"]["frame_id"] for frame in self.pending}
        self.known.intersection_update(pending_ids)
        for index in range(BACKUPS + 1):
            path = paths.bot_file(self.journal.bot_id, LOG_NAME + (f".{index}" if index else ""))
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Lifecycle recovery journal is not regular storage")
                start = max(0, info.st_size - budget)
                source.seek(start)
                data = source.read(budget)
                budget -= len(data)
                if start:
                    data = data.partition(b"\n")[2]
                # Present bytes after an interrupted append are not yet a durable acknowledgement.
                os.fsync(source.fileno())
                for line in data.splitlines():
                    try:
                        value = json.loads(line)
                        frame = contract_frame(value)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if frame and frame["contract"]["frame_id"] in pending_ids:
                        self.known.add(frame["contract"]["frame_id"])
            if budget <= 0:
                break
        directory = os.open(paths.bot_dir(self.journal.bot_id), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def decorate(self, frame, *, atomic):
        self.sequence += 1
        versions = dict(self.versions)
        for event in frame["events"]:
            ref = event.get("lease_ref") or event.get("booking_ref")
            if ref:
                versions[ref] = versions.get(ref, 0) + 1
        for field in ("allocations", "bookings"):
            for lease in frame["state"][field]:
                versions.setdefault(lease["ref"], 1)
        correlation = command_correlation.get()
        frame["contract"] = {
            "version": 2,
            "journal_id": self.journal_id,
            "commit_sequence": self.sequence,
            "frame_id": uuid.uuid4().hex,
            "atomic_state": atomic,
            "initiator": initiator(command_initiator.get()),
            "correlation_id": correlation
            if isinstance(correlation, str) and HEX32.fullmatch(correlation)
            else uuid.uuid4().hex,
            "versions": versions,
        }
        return frame

    def envelope(self, frame):
        pending = self.pending + [frame]
        if len(pending) > MAX_PENDING_FRAMES or len(json.dumps(pending).encode()) > MAX_PENDING_BYTES:
            frame["boundary"], frame["events"] = "outbox_overflow", []
            pending = [frame]
        self.pending = pending
        return {"version": 2, "bot_id": self.journal.bot_id, "identity_revision": self._revision(), "pending": pending}

    def acknowledged_state(self, frame):
        refs = {v["ref"] for field in ("allocations", "bookings") for v in frame["state"][field]}
        self.versions = {k: v for k, v in frame["contract"]["versions"].items() if k in refs}

    def flush(self):
        if self.retry_scan:
            self._read_known()
            self.retry_scan = False
        while self.pending:
            frame = self.pending[0]
            identity = frame["contract"]["frame_id"]
            if identity not in self.known:
                try:
                    self.journal._append(frame)
                except Exception:
                    self.retry_scan = True
                    raise
                self.known.add(identity)
            self.pending.pop(0)
            self.known.discard(identity)
