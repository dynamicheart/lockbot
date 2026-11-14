"""Bounded, aggregate-only monitor journal. Request paths never perform filesystem I/O."""

import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

log = logging.getLogger(__name__)
RETENTION_DAYS = 30
DISK_BUDGET = 128 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
RESOLUTIONS = (60, 900, 3600, 21600)


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def parse_range(start, end, resolution, now):
    try:
        dates = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end)]
        if any(value.tzinfo is None for value in dates):
            raise ValueError
        start, end = (value.timestamp() for value in dates)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, "start and end require RFC3339 timestamps with offsets") from exc
    if not 0 < end - start <= 31 * 86400 or end > now + 60:
        raise HTTPException(422, "History range must be positive, at most 31 days, and not in the future")
    if resolution is None:
        resolution = 900 if end - start <= 86400 else 3600 if end - start <= 7 * 86400 else 21600
    if (
        resolution not in RESOLUTIONS
        or math.ceil((end - start) / resolution) > 2880
        or (end - start > 86400 and resolution < 3600)
    ):
        raise HTTPException(422, "Unsupported resolution or more than 2880 buckets")
    return math.floor(start / resolution) * resolution, math.ceil(end / resolution) * resolution, resolution


def counter():
    from lockbot.backend.app.service_traffic import Counters

    return Counters()


def merge(target, raw):
    for key in (
        "requests",
        "client_errors",
        "server_errors",
        "exceptions",
        "request_bytes",
        "response_bytes",
        "elapsed_ms",
    ):
        setattr(target, key, getattr(target, key) + raw[key])
    target.max_ms = max(target.max_ms, raw["max_ms"])
    target.latency = [a + b for a, b in zip(target.latency, raw["latency"], strict=True)]
    for key, count in raw["statuses"].items():
        target.statuses[key] += count


def validate_record(row):
    from lockbot.backend.app.service_traffic import LATENCY_BOUNDS, SOURCES

    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    if not isinstance(row, dict) or row.get("v") != 1:
        raise ValueError("invalid version")
    stamp = row.get("at")
    bot_id = row.get("bot_id")
    if type(stamp) is not int or stamp % 60 or (bot_id is not None and (type(bot_id) is not int or bot_id <= 0)):
        raise ValueError("invalid scope")
    step = row.get("interval_seconds", 60)
    if step not in (60, 3600) or stamp % step:
        raise ValueError("invalid interval")
    intervals = row.get("intervals", [row["coverage"]])
    if len(intervals) > 60:
        raise ValueError("invalid interval count")
    previous = stamp
    for left, right in intervals:
        if not number(left) or not number(right) or not previous <= left < right <= stamp + step:
            raise ValueError("invalid coverage")
        previous = right
    raw = row["counter"]
    if any(not number(raw[key]) for key in asdict(counter()) if key not in ("statuses", "latency")):
        raise ValueError("invalid counters")
    if len(raw["latency"]) != len(LATENCY_BOUNDS) or any(not number(value) for value in raw["latency"]):
        raise ValueError("invalid histogram")
    if set(raw["statuses"]) != set(counter().statuses) or any(not number(value) for value in raw["statuses"].values()):
        raise ValueError("invalid statuses")
    if sum(raw["latency"]) != raw["requests"] or sum(raw["statuses"].values()) != raw["requests"]:
        raise ValueError("invalid count sums")
    if set(row["sources"]) - set(SOURCES) or any(not number(value) for value in row["sources"].values()):
        raise ValueError("invalid sources")
    if set(row["operations"]) != {"chat", "agent", "operator"} or any(
        not number(value) for value in row["operations"].values()
    ):
        raise ValueError("invalid operations")


class ServiceHistory:
    def __init__(self, directory, clock=time.time, *, disk_budget=DISK_BUDGET):
        self.directory = Path(directory)
        self.clock = clock
        self.disk_budget = disk_budget
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.started = clock()
        self.last_observation = self.started
        self.next_minute = int(self.started // 60)
        self.process = uuid4().hex
        self.pending = {}
        self.pruned_minute = None
        self.bots = OrderedDict()
        self.last_write = None
        self.error = None

    def _scope(self, minute, bot_id):
        key = (minute, bot_id)
        if key not in self.pending:
            self.pending[key] = {
                "counter": counter(),
                "sources": {},
                "operations": {"chat": 0, "agent": 0, "operator": 0},
            }
        return self.pending[key]

    def record(self, *, bot_id=None, source, status, duration_ms, received, sent, failed, **_):
        from lockbot.backend.app.service_traffic import SOURCES

        source = source if source in SOURCES else "other"
        with self.lock:
            now = self.clock()
            minute = int(now // 60)
            self._observe_clock(now)
            # Two minutes maximum: a stalled writer loses coverage instead of growing memory.
            self._prune(minute)
            scopes = [None]
            if type(bot_id) is int and bot_id > 0:
                self._track_bot(bot_id, now)
                scopes.append(bot_id)
            for scope in scopes:
                bucket = self._scope(minute, scope)
                bucket["counter"].add(status, duration_ms, received, sent, failed)
                bucket["sources"][source] = bucket["sources"].get(source, 0) + 1

    def _prune(self, minute):
        if minute != self.pruned_minute:
            self.pending = {key: value for key, value in self.pending.items() if minute - 1 <= key[0] <= minute}
            self.pruned_minute = minute

    def _observe_clock(self, now):
        if now < self.last_observation:
            # Start a new observed interval instead of dropping requests whose
            # wall time now predates this process/bot's original start time.
            self.started = now
            self.next_minute = int(now // 60)
            for bot_id in self.bots:
                self.bots[bot_id] = now
            self._prune(self.next_minute)
        self.last_observation = now

    def _track_bot(self, bot_id, now):
        if bot_id not in self.bots:
            if len(self.bots) >= 256:
                evicted, _ = self.bots.popitem(last=False)
                self.pending = {key: value for key, value in self.pending.items() if key[1] != evicted}
            self.bots[bot_id] = now
        self.bots.move_to_end(bot_id)

    def operation(self, bot_id, source):
        with self.lock:
            now = self.clock()
            minute = int(now // 60)
            self._observe_clock(now)
            self._prune(minute)
            self._track_bot(bot_id, now)
            for scope in (None, bot_id):
                bucket = self._scope(minute, scope)
                bucket["operations"][source] += 1

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="service-history")
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(15):
            try:
                self.flush()
            except Exception:
                # Unexpected telemetry failures must never terminate collection or
                # escape into the resource service. Recovery is retried next tick.
                if self.error != "collection_failed":
                    log.warning("Service history collection failed; retrying on the next interval")
                self.error = "collection_failed"

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.flush(final=True)

    def flush(self, *, final=False):
        with self.io_lock:
            with self.lock:
                now = self.clock()
                current = int(now // 60)
                self._observe_clock(now)
                # Never infer coverage after an unobserved writer delay of more than 2 minutes.
                first = max(self.next_minute, current - 1)
                stop = current + int(final)
                records = []
                for minute in range(first, stop):
                    scopes = [None, *self.bots]
                    for bot_id in scopes:
                        observed_start = max(minute * 60, self.started, self.bots.get(bot_id, self.started))
                        observed_end = min((minute + 1) * 60, now)
                        if observed_end <= observed_start:
                            continue
                        bucket = self.pending.pop((minute, bot_id), None) or {
                            "counter": counter(),
                            "sources": {},
                            "operations": {"chat": 0, "agent": 0, "operator": 0},
                        }
                        records.append(
                            {
                                "v": 1,
                                "process": self.process,
                                "at": minute * 60,
                                "bot_id": bot_id,
                                "coverage": [observed_start, observed_end],
                                "counter": asdict(bucket["counter"]),
                                "sources": bucket["sources"],
                                "operations": bucket["operations"],
                            }
                        )
                self.next_minute = stop
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                batches = {}
                for record in records:
                    filename = datetime.fromtimestamp(record["at"], timezone.utc).strftime("%Y%m%dT%H") + ".jsonl"
                    line = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
                    if len(line) > MAX_LINE_BYTES:
                        raise ValueError("aggregate too large")
                    batches.setdefault(filename, []).append(line)
                for filename, lines in batches.items():
                    # A clock correction/restart can revisit an already sealed
                    # hour. Keep new records separate: rebuilding from minute
                    # detail could discard older data already compacted away.
                    directory = self.directory
                    if (directory / "hourly" / filename).exists():
                        directory = directory / "late"
                        directory.mkdir(mode=0o700, exist_ok=True)
                    self._append(directory / filename, b"".join(lines))
                self._rollup(now)
                self._cleanup(now)
                self.last_write, self.error = now, None
            except (OSError, ValueError):
                if self.error != "storage_unavailable":
                    log.warning("Service history storage unavailable; resource operations continue")
                self.error = "storage_unavailable"

    def _append(self, path, data):
        # Recover only an incomplete tail. Completed malformed lines remain visible
        # as damaged records during queries instead of rewriting historical data.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            if length:
                stream.seek(max(0, length - MAX_LINE_BYTES))
                tail = stream.read(MAX_LINE_BYTES)
                if not tail.endswith(b"\n"):
                    boundary = tail.rfind(b"\n")
                    stream.truncate(max(0, length - MAX_LINE_BYTES) + boundary + 1 if boundary >= 0 else 0)
            stream.seek(0, 2)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _rollup(self, now):
        """A derived hourly index keeps 7/30-day reads bounded without a database."""
        directory = self.directory / "hourly"
        directory.mkdir(mode=0o700, exist_ok=True)
        # The writer seals at most the last two hours. Earlier missing indexes
        # after long outages remain unknown instead of an unbounded startup scan.
        for hour in range(int(now // 3600) - 2, int(now // 3600)):
            name = datetime.fromtimestamp(hour * 3600, timezone.utc).strftime("%Y%m%dT%H") + ".jsonl"
            target, source = directory / name, self.directory / name
            if target.exists() or not source.exists() or source.stat().st_size > 16 * 1024 * 1024:
                continue
            scopes = {}
            damaged = False
            with source.open("rb") as stream:
                while line := stream.readline(MAX_LINE_BYTES + 1):
                    try:
                        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                            damaged = True
                            break
                        row = json.loads(line)
                        validate_record(row)
                        scope = row["bot_id"]
                        item = scopes.setdefault(
                            scope,
                            {
                                "counter": counter(),
                                "sources": {},
                                "operations": dict.fromkeys(("chat", "agent", "operator"), 0),
                                "intervals": [],
                            },
                        )
                        merge(item["counter"], row["counter"])
                        item["intervals"].append(row["coverage"])
                        for key, count in row["sources"].items():
                            item["sources"][key] = item["sources"].get(key, 0) + count
                        for key, count in row["operations"].items():
                            item["operations"][key] += count
                    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
                        damaged = True
                        continue
            output = [{"v": 1, "record_type": "damaged_segment", "at": hour * 3600}] if damaged else []
            for scope, item in scopes.items():
                intervals = []
                for left, right in sorted(item["intervals"]):
                    if intervals and left <= intervals[-1][1]:
                        intervals[-1][1] = max(intervals[-1][1], right)
                    else:
                        intervals.append([left, right])
                output.append(
                    {
                        "v": 1,
                        "process": "derived-hourly-v1",
                        "at": hour * 3600,
                        "interval_seconds": 3600,
                        "bot_id": scope,
                        "coverage": [hour * 3600, (hour + 1) * 3600],
                        "intervals": intervals,
                        "counter": asdict(item["counter"]),
                        "sources": item["sources"],
                        "operations": item["operations"],
                    }
                )
            temporary = target.with_suffix(".tmp")
            temporary.unlink(missing_ok=True)
            try:
                self._append(
                    temporary,
                    b"".join(
                        (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode() for row in output
                    ),
                )
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def _cleanup(self, now):
        files = (
            sorted(self.directory.glob("????????T??.jsonl"))
            + sorted((self.directory / "hourly").glob("????????T??.jsonl"))
            + sorted((self.directory / "late").glob("????????T??.jsonl"))
        )
        total = sum(path.stat().st_size for path in files)
        # Minute detail expires after 25h only when its hourly aggregate exists.
        # Budget eviction removes hourly bases before late deltas: retaining a
        # complete base after silently losing its delta would misstate coverage.
        for path in files:
            stamp = datetime.strptime(path.stem, "%Y%m%dT%H").replace(tzinfo=timezone.utc).timestamp()
            size = path.stat().st_size
            detail_expired = (
                path.parent == self.directory
                and stamp + 3600 <= now - 25 * 3600
                and (self.directory / "hourly" / path.name).exists()
            )
            if detail_expired or stamp + 3600 <= now - RETENTION_DAYS * 86400 or total > self.disk_budget:
                path.unlink()
                total -= size

    def query(self, start, end, resolution, bot_id=None):
        from lockbot.backend.app.service_traffic import SOURCES

        first = math.floor(start / resolution) * resolution
        buckets = {
            stamp: {
                "counter": counter(),
                "intervals": [],
                "sources": dict.fromkeys(SOURCES, 0),
                "operations": dict.fromkeys(("chat", "agent", "operator"), 0),
                "incomplete": False,
            }
            for stamp in range(int(first), math.ceil(end), resolution)
        }
        truncated = False
        bytes_read = 0
        damaged = False

        def mark_incomplete(hour):
            for stamp in range(hour * 3600 // resolution * resolution, (hour + 1) * 3600, resolution):
                if stamp in buckets:
                    buckets[stamp]["incomplete"] = True

        with self.io_lock:
            for hour in range(int(start // 3600), int((end - 0.001) // 3600) + 1):
                path = self.directory / (
                    datetime.fromtimestamp(hour * 3600, timezone.utc).strftime("%Y%m%dT%H") + ".jsonl"
                )
                hourly = self.directory / "hourly" / path.name
                if resolution >= 3600 and hourly.exists():
                    path = hourly
                segments = (path, self.directory / "late" / path.name)
                for path in segments:
                    try:
                        if not path.exists():
                            continue
                        if bytes_read + path.stat().st_size > MAX_READ_BYTES:
                            truncated = True
                            mark_incomplete(hour)
                            break
                        with path.open("rb") as stream:
                            while line := stream.readline(MAX_LINE_BYTES + 1):
                                bytes_read += len(line)
                                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                                    damaged = True
                                    mark_incomplete(hour)
                                    break
                                try:
                                    row = json.loads(line)
                                    if isinstance(row, dict) and row.get("record_type") == "damaged_segment":
                                        damaged = True
                                        mark_incomplete(hour)
                                        continue
                                    validate_record(row)
                                    if row["v"] != 1 or row["bot_id"] != bot_id or not start <= row["at"] < end:
                                        continue
                                    bucket = buckets[math.floor(row["at"] / resolution) * resolution]
                                    merge(bucket["counter"], row["counter"])
                                    bucket["intervals"].extend(row.get("intervals", [row["coverage"]]))
                                    for key, value in row["sources"].items():
                                        bucket["sources"][key] += value
                                    for key, value in row["operations"].items():
                                        bucket["operations"][key] += value
                                except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
                                    damaged = True
                                    mark_incomplete(hour)
                    except OSError:
                        damaged = True
                        mark_incomplete(hour)
                if truncated:
                    break
        history, counts, totals = (
            [],
            dict.fromkeys(("complete_buckets", "partial_buckets", "unknown_buckets"), 0),
            counter(),
        )
        for stamp, bucket in buckets.items():
            intervals = sorted(bucket["intervals"])
            covered, right = 0, max(start, stamp)
            for left, finish in intervals:
                left, finish = max(left, start, stamp), min(finish, end, stamp + resolution)
                covered += max(0, finish - max(left, right))
                right = max(right, finish)
            expected = min(end, stamp + resolution) - max(start, stamp)
            coverage = (
                "complete" if covered >= expected and not bucket["incomplete"] else "partial" if covered else "unknown"
            )
            counts[coverage + "_buckets"] += 1
            payload = bucket["counter"].payload()
            merge(totals, asdict(bucket["counter"]))
            if not covered:
                payload = dict.fromkeys(payload)
            history.append(
                {
                    "at": iso(stamp),
                    "coverage": coverage,
                    "observed": bool(covered),
                    "observed_seconds": round(covered, 3),
                    **payload,
                    "sources": bucket["sources"] if covered else None,
                    "operations": bucket["operations"] if covered else None,
                }
            )
        return {
            "schema_version": 1,
            "start": iso(start),
            "end": iso(end),
            "resolution_seconds": resolution,
            "captured_at": iso(self.clock()),
            "bot_id": bot_id,
            "history": history,
            "totals": totals.payload(),
            "coverage": counts,
            "storage": {
                "healthy": not self.error,
                "last_write_at": iso(self.last_write) if self.last_write else None,
                "error": self.error,
                "retention_days": RETENTION_DAYS,
                "disk_budget_bytes": self.disk_budget,
                "query_truncated": truncated,
                "damaged_segments": damaged,
            },
        }
