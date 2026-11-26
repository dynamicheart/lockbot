"""Per-bot daily utilization from committed leases and authorized observer samples."""

import json
import logging
import re
import sqlite3
import threading
import time
from bisect import bisect_right
from collections import defaultdict
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as local_time
from pathlib import Path
from zoneinfo import ZoneInfo

from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.resource_api.lease_history import BACKUPS, LOG_NAME, MAX_LINE_BYTES, public_frame

SETTING = "DAILY_UTILIZATION_REPORT_ENABLED"
RETENTION_SECONDS = 4 * 86400
MIN_COVERAGE = 0.8
logger = logging.getLogger(__name__)


def report_group(platform, group_ids):
    groups = {item.strip() for item in (group_ids or "").split(",") if item.strip()}
    if len(groups) != 1:
        return None
    group = groups.pop()
    platform = (platform or "").lower()
    if platform == "infoflow" and re.fullmatch(r"[0-9]{1,20}", group) and int(group) > 0:
        return group
    if platform == "feishu" and re.fullmatch(r"oc_[A-Za-z0-9_-]+", group):
        return group
    return None


def report_available(bot, observer):
    return bool(
        observer
        and public_datasource(bot)
        and report_group(bot.platform, bot.group_id)
        and observer.sampling_status(bot.id, bot.uuid)["configured_nodes"]
    )


def report_window(now, zone):
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    if local.hour != 9:
        return None
    day = local.date() - timedelta(days=1)
    start = datetime.combine(day, local_time.min, local.tzinfo).timestamp()
    end = datetime.combine(local.date(), local_time.min, local.tzinfo).timestamp()
    return day, start, end


def read_day_frames(bot, bot_id, start, end):
    """Pin the bounded journal files and a current checkpoint under the writer lock."""
    paths = get_managed_paths()
    with ExitStack() as streams:
        with bot._lock:
            journal = getattr(bot.config, "_lifecycle_observer", None)
            if journal is None:
                return None
            checkpoint = journal.checkpoint(bot.state.bot_state)
            if not checkpoint["consistent"] or checkpoint["contract"]["pending_frames"]:
                return None
            files = []
            for index in reversed(range(BACKUPS + 1)):
                name = LOG_NAME + (f".{index}" if index else "")
                try:
                    files.append(streams.enter_context(paths.bot_file(bot_id, name).open("rb")))
                except FileNotFoundError:
                    continue
        frames = []
        for stream in files:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                if not line.strip():
                    continue
                if len(line) > MAX_LINE_BYTES:
                    return None
                try:
                    raw = json.loads(line)
                except ValueError:
                    return None
                if not isinstance(raw, dict) or raw.get("bot_id") != bot_id:
                    return None
                frame = public_frame(raw)
                if frame is None:
                    return None
                at = frame["timestamp"]
                if at <= start:
                    frames = [frame]
                elif at < end:
                    frames.append(frame)
                elif at <= checkpoint["timestamp"]:
                    return [*frames, frame]
        if (
            frames
            and frames[-1]["after_digest"] == checkpoint["after_digest"]
            and (frames[-1]["session"], frames[-1]["sequence"]) == (checkpoint["session"], checkpoint["sequence"])
        ):
            return [*frames, {**checkpoint, "before_digest": checkpoint["after_digest"], "boundary": None}]
    return None


@dataclass
class Usage:
    occupied: float = 0
    covered: float = 0
    sampled_card_seconds: float = 0
    full_card_seconds: float = 0


def summarize(frames, samples, start, end):
    if not frames or frames[0]["timestamp"] > start or frames[-1]["timestamp"] < end:
        return None

    by_resource = defaultdict(list)
    for resource, observed, expires, count, utilization in samples:
        by_resource[resource].append((observed, expires, count, utilization))
    intervals = {}
    for resource, series in by_resource.items():
        starts = [row[0] for row in series]
        spans = [
            (left, min(expiry, starts[index + 1]) if index + 1 < len(starts) else expiry, count, utilization)
            for index, (left, expiry, count, utilization) in enumerate(series)
        ]
        intervals[resource] = starts, spans

    results = defaultdict(Usage)
    for before, after in zip(frames[:-1], frames[1:], strict=True):
        if (
            not before["state"]["complete"]
            or not after["state"]["complete"]
            or after["timestamp"] < before["timestamp"]
        ):
            return None
        if after["boundary"] == "session_start":
            if after["after_digest"] != before["after_digest"]:
                return None
        elif after["boundary"] or after["before_digest"] != before["after_digest"]:
            return None
        for allocation in before["state"]["allocations"]:
            left = max(start, before["timestamp"], allocation["start"])
            right = min(end, after["timestamp"], allocation["deadline"])
            if left >= right:
                continue
            usage = results[allocation["actor"]["external_user_id"]]
            usage.occupied += right - left
            resource = allocation["resource"]
            if allocation["mode"] != "exclusive" or resource not in intervals:
                continue
            starts, spans = intervals[resource]
            for a, b, count, percent in spans[max(0, bisect_right(starts, left) - 1) :]:
                if a >= right:
                    break
                duration = min(b, right) - max(a, left)
                if duration > 0:
                    usage.covered += duration
                    usage.sampled_card_seconds += count * duration
                    usage.full_card_seconds += count * duration * percent / 100
    return results


def render_report(day, results, *, language="zh", card_units=False):
    chinese = language != "en"
    heading = (
        f"📊【资源利用率日报 · {day.month} 月 {day.day} 日】"
        if chinese
        else f"📊 Resource utilization · {day.isoformat()}"
    )
    if not results:
        empty = "昨日无资源占用。" if chinese else "No resource holdings yesterday."
        return f"{heading}\n\n{empty}"
    label = ("总卡时" if card_units else "总机时") if chinese else ("Card-hours" if card_units else "Node-hours")
    unit = ("卡时" if card_units else "台时") if chinese else ("card-h" if card_units else "node-h")
    lines = [heading, "", f"用户　平均利用率 · {label}" if chinese else f"User  Mean utilization · {label}"]
    ranked = sorted(results.items(), key=lambda row: (-row[1].full_card_seconds if has_coverage(row[1]) else 1, row[0]))
    for index, (actor, usage) in enumerate(ranked):
        name = " ".join(actor.split())[:64]
        if has_coverage(usage):
            rate = f"{usage.full_card_seconds / usage.sampled_card_seconds:.1%}"
        else:
            rate = "数据不足" if chinese else "Insufficient samples"
        hours = usage.occupied / 3600
        held = "< 0.1" if hours < 0.1 else f"{hours:.1f}"
        line = f"{name}\u3000{rate} \u00b7 {held} {unit}" if chinese else f"{name}  {rate} \u00b7 {held} {unit}"
        if sum(map(len, lines)) + len(lines) + len(line) > 1700:
            remaining = len(ranked) - index
            lines.append(f"其余 {remaining} 人" if chinese else f"Plus {remaining} more")
            break
        lines.append(line)
    return "\n".join(lines)


def has_coverage(usage):
    return usage.sampled_card_seconds > 0 and usage.covered >= MIN_COVERAGE * usage.occupied


class DailyReports:
    def __init__(self, manager, observer, *, factory=SessionLocal, clock=time.time, database=None):
        self.manager = manager
        self.observer = observer
        self.factory = factory
        self.clock = clock
        self.database = database
        self.stopping = threading.Event()
        self.thread = None

    def store_path(self):
        if self.database is not None:
            return Path(self.database)
        return get_managed_paths().checked("daily-utilization.sqlite3")

    def connect(self):
        path = self.store_path()
        if not path.exists():
            path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(path, timeout=2)
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sample(
                    bot_uuid TEXT, revision INTEGER, resource TEXT, observed REAL, expires REAL,
                    devices INTEGER, utilization REAL, received REAL,
                    PRIMARY KEY(bot_uuid, revision, resource, observed)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS samples_expiry ON sample(observed);
                CREATE TABLE IF NOT EXISTS delivery(
                    bot_uuid TEXT, day TEXT, status TEXT,
                    PRIMARY KEY(bot_uuid, day)
                ) WITHOUT ROWID;
            """)
        except Exception:
            db.close()
            raise
        return db

    def record(self, rows, now):
        if rows:
            with closing(self.connect()) as db, db:
                db.executemany("INSERT OR IGNORE INTO sample VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                db.execute("DELETE FROM sample WHERE observed < ?", (now - RETENTION_SECONDS,))

    def report_samples(self, bot, start, end):
        with closing(self.connect()) as db:
            scope = (bot.uuid, bot.identity_revision)
            earliest = db.execute("SELECT MIN(received) FROM sample WHERE bot_uuid=? AND revision=?", scope).fetchone()[
                0
            ]
            if earliest is None or earliest > start:
                return None
            return db.execute(
                """SELECT resource, observed, expires, devices, utilization
                   FROM sample WHERE bot_uuid=? AND revision=? AND expires>? AND observed<?
                   ORDER BY resource, observed""",
                (*scope, start, end),
            ).fetchall()

    def send_due(self, bot_id):
        with self.manager.admit(bot_id) as lease, self.factory() as db:
            row = db.get(Bot, bot_id)
            if row is None or row.is_deleted or row.status != "running" or not report_available(row, self.observer):
                return
            config = lease.bot.config
            if not config.get_val(SETTING) or config.get_val("_LIFECYCLE_BOT_UUID") != row.uuid:
                return
            if config.get_val("_IDENTITY_REVISION") != row.identity_revision:
                return
            window = report_window(self.clock(), config.get_val("OVERNIGHT_TIMEZONE"))
            group = report_group(row.platform, row.group_id)
            if window is None or not group:
                return
            day, start, end = window
            key = row.uuid, day.isoformat()
            with closing(self.connect()) as store:
                if store.execute("SELECT 1 FROM delivery WHERE bot_uuid=? AND day=?", key).fetchone():
                    return
            samples = self.report_samples(row, start, end)
            if not samples:
                return
            results = summarize(read_day_frames(lease.bot, bot_id, start, end), samples, start, end)
            if results is None or self.stopping.is_set():
                return
            content = render_report(
                day, results, language=config.get_val("LANGUAGE"), card_units=row.bot_type in {"DEVICE", "QUEUE_DEVICE"}
            )
            with closing(self.connect()) as store:
                with store:
                    claimed = store.execute("INSERT OR IGNORE INTO delivery VALUES (?, ?, 'attempted')", key).rowcount
                if claimed != 1:
                    return
                try:
                    status, _ = lease.bot.adapter.send_broadcast(content, group)
                except Exception:
                    status = "unknown"
                with store:
                    store.execute("UPDATE delivery SET status=? WHERE bot_uuid=? AND day=?", (status, *key))
                logger.info("Daily utilization report for bot %s: %s", bot_id, status)

    def tick(self):
        if not self.store_path().exists():
            return
        with self.factory() as db:
            candidates = (
                db.query(Bot.id, Bot.config_overrides)
                .filter(Bot.status == "running", Bot.is_deleted.is_(False), Bot.config_overrides.contains(SETTING))
                .all()
            )
            bot_ids = [bot_id for bot_id, config in candidates if json.loads(config).get(SETTING) is True]
        for bot_id in bot_ids:
            if self.stopping.is_set():
                break
            try:
                self.send_due(bot_id)
            except (AdmissionFull, BotNotRunning, InstanceRetiring):
                continue
            except Exception as error:
                logger.warning("Daily utilization report for bot %s unavailable (%s)", bot_id, type(error).__name__)

    def run(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as error:
                logger.warning("Daily utilization scheduler unavailable (%s)", type(error).__name__)
            self.stopping.wait(30)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="daily-utilization-reports", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=12)
