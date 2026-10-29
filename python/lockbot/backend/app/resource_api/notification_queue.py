"""Bounded, process-local trailing batches for API group notifications."""

import logging
import threading
import time
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicEvent:
    text: str
    kind: str


@dataclass
class _Batch:
    target: object
    due: float
    events: deque = field(default_factory=deque)
    size: int = 0
    dropped: int = 0
    sending: bool = False
    cancelled: bool = False
    retry_text: str | None = None
    retry_until: float = 0.0


class NotificationQueue:
    """No retry or durable replay; one fixed deadline per bot, two send slots globally.

    The first send may use the caller's already-admitted lease. All trailing sends use
    ``deliver`` to revalidate the target. The coordinator never performs network IO.
    ``run_due`` provides a deterministic serial runner for clock-based tests.
    """

    def __init__(
        self,
        deliver,
        *,
        clock=time.monotonic,
        interval=10.0,
        workers=2,
        max_bots=256,
        max_events=64,
        max_bytes=65536,
        max_chars=1800,
        max_text_bytes=4000,
    ):
        self.deliver, self.clock, self.interval = deliver, clock, interval
        self.workers, self.max_bots, self.max_events = workers, max_bots, max_events
        self.max_bytes, self.max_chars = max_bytes, max_chars
        self.max_text_bytes = max_text_bytes
        self._condition = threading.Condition()
        self._batches = OrderedDict()
        self._inflight = 0
        self._thread = self._executor = None
        self._stopped = False
        self.counts = Counter()

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stopped = False
            self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="api-notify")
            self._thread = threading.Thread(target=self._loop, name="api-notify-batches", daemon=True)
            self._thread.start()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._clear_locked("shutdown")
            self._condition.notify_all()
            thread, executor = self._thread, self._executor
        if thread:
            thread.join(timeout=2)
        if executor:
            executor.shutdown(wait=False, cancel_futures=False)

    def reset(self):
        with self._condition:
            self._clear_locked("reset")
            self._stopped = False
            self.counts.clear()
            self._condition.notify_all()

    def _clear_locked(self, reason):
        for batch in self._batches.values():
            self._cancel_locked(batch, reason)
        self._batches.clear()

    def _cancel_locked(self, batch, reason):
        batch.cancelled = True
        batch.target.cancelled.set()
        count = len(batch.events) + batch.dropped + int(batch.retry_text is not None)
        self.counts["cancelled"] += count
        if count:
            logger.warning("Bot %d: discarded %d pending API notifications (%s)", batch.target.bot_id, count, reason)
        batch.events.clear()
        batch.size = batch.dropped = 0
        batch.retry_text = None

    def cancel(self, bot_id, generation=None):
        with self._condition:
            batch = self._batches.get(bot_id)
            if batch and (generation is None or batch.target.generation == generation):
                self._batches.pop(bot_id)
                self._cancel_locked(batch, "target_retired_or_disabled")
            self._condition.notify_all()

    def offer(self, target, event, *, immediate=None):
        """Return true only if a synchronous first send was platform-accepted.

        False may mean queued, cancelled or rejected; HTTP command results are independent.
        Only safe public text enters this queue. An overflow drops aliases before resource
        operations, otherwise the newest event; every drop is counted and reported.
        """
        with self._condition:
            if self._stopped:
                self.counts["shutdown_dropped"] += 1
                return False
            batch = self._batches.get(target.bot_id)
            if batch and batch.target.key != target.key:
                self._cancel_locked(batch, "target_changed")
                self._batches.pop(target.bot_id)
                batch = None
            if batch is None:
                if len(self._batches) >= self.max_bots:
                    idle = next(
                        (
                            key
                            for key, item in self._batches.items()
                            if not item.sending and not item.events and not item.dropped and not item.retry_text
                        ),
                        None,
                    )
                    if idle is None:
                        self.counts["capacity_dropped"] += 1
                        logger.warning("Bot %d: API notification capacity exhausted", target.bot_id)
                        return False
                    self._batches.pop(idle)
                batch = _Batch(target, self.clock())
                self._batches[target.bot_id] = batch
            self._batches.move_to_end(target.bot_id)
            if (
                immediate
                and not batch.sending
                and not batch.events
                and not batch.dropped
                and not batch.retry_text
                and len(event.text) <= self.max_chars
                and len(event.text.encode()) <= self.max_text_bytes
                and self.clock() >= batch.due
                and self._inflight < self.workers
            ):
                self._reserve_locked(batch)
            else:
                self._append_locked(batch, event)
                logger.info("Bot %d: API notification queued (%d pending items)", target.bot_id, len(batch.events))
                self._condition.notify_all()
                return False
        try:
            outcome = immediate(event.text)
            self._record(outcome, target.bot_id)
            return outcome == "accepted"
        finally:
            self._finish(batch)

    def _append_locked(self, batch, event):
        size = len(event.text.encode())

        def full():
            return len(batch.events) >= self.max_events or batch.size + size > self.max_bytes

        if full() and event.kind != "alias":
            alias = next((item for item in batch.events if item.kind == "alias"), None)
            if alias:
                batch.events.remove(alias)
                batch.size -= len(alias.text.encode())
                self._drop_locked(batch)
        if size > min(self.max_bytes, self.max_text_bytes) or len(event.text) > self.max_chars or full():
            self._drop_locked(batch)
            return
        batch.events.append(event)
        batch.size += size
        self.counts["queued"] += 1

    def _drop_locked(self, batch):
        batch.dropped += 1
        self.counts["overflow_dropped"] += 1
        if batch.dropped == 1:
            logger.warning("Bot %d: API notification overflow; a loss count will be included", batch.target.bot_id)

    def _reserve_locked(self, batch):
        self._inflight += 1
        batch.sending = True
        batch.due = self.clock() + self.interval

    def _take_locked(self):
        jobs = []
        for batch in sorted(self._batches.values(), key=lambda item: item.due):
            if self._inflight >= self.workers:
                break
            if (
                batch.cancelled
                or batch.sending
                or not (batch.events or batch.dropped or batch.retry_text)
                or batch.due > self.clock()
            ):
                continue
            if batch.retry_text and self.clock() >= batch.retry_until:
                batch.retry_text = None
                batch.retry_until = 0
                self.counts["admission_expired"] += 1
                logger.warning("Bot %d: pending API notification expired under admission pressure", batch.target.bot_id)
            if batch.retry_text:
                text, batch.retry_text = batch.retry_text, None
            else:
                parts = []
                suffix = batch.target.overflow_text(batch.dropped) if batch.dropped else ""
                while batch.events:
                    candidate = "\n\n".join(parts + [batch.events[0].text] + ([suffix] if suffix else []))
                    if len(candidate) > self.max_chars or len(candidate.encode()) > self.max_text_bytes:
                        break
                    event = batch.events.popleft()
                    parts.append(event.text)
                    batch.size -= len(event.text.encode())
                if suffix:
                    parts.append(suffix)
                    batch.dropped = 0
                if not parts:
                    continue
                text = "\n\n".join(parts)
            self._reserve_locked(batch)
            jobs.append((batch, text))
        return jobs

    def _record(self, outcome, bot_id):
        with self._condition:
            self.counts[outcome] += 1
        logger.info("Bot %d: API notification delivery %s", bot_id, outcome)

    def _finish(self, batch):
        with self._condition:
            batch.sending = False
            self._inflight -= 1
            self._condition.notify_all()

    def _run(self, batch, text):
        try:
            with self._condition:
                valid = not batch.cancelled and self._batches.get(batch.target.bot_id) is batch
            outcome = self.deliver(batch.target, text) if valid else "cancelled"
            if outcome == "deferred":
                with self._condition:
                    if not batch.cancelled:
                        batch.retry_text = text
                        if not batch.retry_until:
                            batch.retry_until = self.clock() + 120
                        batch.due = self.clock() + 1
            else:
                batch.retry_until = 0
            self._record(outcome, batch.target.bot_id)
        except Exception:
            self._record("unknown", batch.target.bot_id)
            logger.warning("Bot %d: API notification delivery uncertain; not retried", batch.target.bot_id)
        finally:
            self._finish(batch)

    def run_due(self):
        """Execute currently due work synchronously, without sleeping or starting threads."""
        with self._condition:
            jobs = self._take_locked()
        for batch, text in jobs:
            self._run(batch, text)
        return len(jobs)

    def _loop(self):
        while True:
            with self._condition:
                if self._stopped:
                    return
                jobs = self._take_locked()
                if not jobs:
                    due = [
                        item.due
                        for item in self._batches.values()
                        if not item.sending and (item.events or item.dropped or item.retry_text)
                    ]
                    delay = max(0.05, min(due) - self.clock()) if due and self._inflight < self.workers else None
                    self._condition.wait(timeout=delay)
                    continue
            for batch, text in jobs:
                with self._condition:
                    if self._stopped:
                        self._finish(batch)
                    else:
                        self._executor.submit(self._run, batch, text)
