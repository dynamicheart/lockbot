"""
scheduler.py - Min-heap based bot scheduler.

Replaces per-bot threading.Timer with a single background thread.
Supports both standalone mode (BotInstance self-contained) and
managed mode (BotManager injects a shared instance).
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lockbot.core.bot_instance import BotInstance

logger = logging.getLogger(__name__)

_IDLE_INTERVAL = 60.0  # seconds between checks when no active locks
_MIN_INTERVAL = 1.0  # minimum reschedule interval (prevent busy-loop)
_BACKOFF_SEC = 30.0  # retry delay after _check_and_notify raises
_MAX_CONSECUTIVE_FAILURES = 5  # failures before on_fatal_error is triggered


class BotScheduler:
    """Single background thread + min-heap driving all bot timer checks.

    Standalone mode:
        BotInstance creates its own BotScheduler and registers itself.
        Scheduler starts automatically; stops when BotInstance.shutdown() is called.

    Managed mode:
        BotManager creates one shared BotScheduler, injects it into every
        BotInstance via the ``scheduler`` constructor parameter, and calls
        scheduler.add(bot_id, instance) / scheduler.remove(bot_id) explicitly.
    """

    def __init__(self) -> None:
        self._instances: dict[int, BotInstance] = {}
        # heap entries: (fire_at, generation, bot_id)
        self._heap: list[tuple[float, int, int]] = []
        self._gens: dict[int, int] = {}  # bot_id → current generation
        self._failure_counts: dict[int, int] = {}  # bot_id → consecutive failure count
        self._callbacks: dict[int, Callable[[int], None]] = {}  # bot_id → on_fatal_error
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._alive = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="BotScheduler")

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Start the scheduler background thread (idempotent: no-op if already running)."""
        if self._alive:
            return
        self._alive = True
        # Recreate thread if a previous one was stopped (threads cannot be restarted)
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="BotScheduler")
        self._thread.start()

    def stop(self) -> None:
        """Stop the scheduler; blocks until the background thread exits (no-op if never started)."""
        self._alive = False
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def add(
        self,
        bot_id: int,
        instance: BotInstance,
        delay: float = 0.0,
        on_fatal_error: Callable[[int], None] | None = None,
    ) -> None:
        """Register *instance* under *bot_id* and schedule its first check after *delay* seconds.

        Args:
            on_fatal_error: Optional callback invoked when the bot has failed
                _MAX_CONSECUTIVE_FAILURES times in a row.  Receives bot_id.
                Called from the scheduler thread — must not block for long.
        """
        with self._lock:
            self._instances[bot_id] = instance
            self._failure_counts[bot_id] = 0
            if on_fatal_error is not None:
                self._callbacks[bot_id] = on_fatal_error
            gen = self._gens.get(bot_id, 0)
            heapq.heappush(self._heap, (time.monotonic() + delay, gen, bot_id))
        self._wake.set()

    def remove(self, bot_id: int) -> None:
        """Unregister *bot_id* and cancel all pending checks (lazy deletion via generation bump)."""
        with self._lock:
            self._gens[bot_id] = self._gens.get(bot_id, 0) + 1
            self._instances.pop(bot_id, None)
            self._failure_counts.pop(bot_id, None)
            self._callbacks.pop(bot_id, None)

    def reschedule_soon(self, bot_id: int) -> None:
        """Wake the scheduler to re-evaluate *bot_id* within ~1 s.

        Call this after any state change (e.g. a new lock was added) so the
        scheduler recalculates its sleep duration instead of waiting until the
        next idle wakeup.  Safe to call from any thread; no-op if the bot is
        not registered.
        """
        with self._lock:
            if bot_id not in self._instances:
                return
            gen = self._gens.get(bot_id, 0)
            heapq.heappush(self._heap, (time.monotonic() + _MIN_INTERVAL, gen, bot_id))
        self._wake.set()

    # ----------------------------------------------------------------- private

    def _run(self) -> None:
        while self._alive:
            try:
                self._wake.clear()
                self._wake.wait(timeout=self._next_sleep())
                if not self._alive:
                    break
                self._fire_due()
            except Exception:
                # Guard against any unexpected bug in scheduler infrastructure.
                # Log and keep running — individual bot errors are handled inside _fire_due.
                logger.exception("BotScheduler: unexpected error in main loop, continuing")

    def _next_sleep(self) -> float:
        """Return seconds until the next valid heap entry (discarding stale ones)."""
        with self._lock:
            while self._heap:
                fire_at, gen, bot_id = self._heap[0]
                if gen != self._gens.get(bot_id, 0):
                    heapq.heappop(self._heap)  # stale entry, discard
                    continue
                return max(0.0, fire_at - time.monotonic())
        return _IDLE_INTERVAL

    def _fire_due(self) -> None:
        """Pop all due entries and run their checks outside the lock."""
        due: list[tuple[int, int]] = []
        now = time.monotonic()
        with self._lock:
            while self._heap and self._heap[0][0] <= now:
                fire_at, gen, bot_id = heapq.heappop(self._heap)
                if gen == self._gens.get(bot_id, 0):
                    due.append((bot_id, gen))

        for bot_id, gen in due:
            instance = self._instances.get(bot_id)
            if instance is None:
                continue

            try:
                next_in = instance.bot._check_and_notify()
                delay = max(_MIN_INTERVAL, next_in) if next_in is not None else _IDLE_INTERVAL
                # Success: reset consecutive failure count
                with self._lock:
                    self._failure_counts[bot_id] = 0
            except Exception:
                bot_name = instance.config.get_val("BOT_NAME") if hasattr(instance, "config") else "?"
                with self._lock:
                    self._failure_counts[bot_id] = self._failure_counts.get(bot_id, 0) + 1
                    failures = self._failure_counts[bot_id]
                    callback = self._callbacks.get(bot_id)

                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.critical(
                        "BotScheduler: bot %d (%s) failed %d consecutive times — stopping checks",
                        bot_id,
                        bot_name,
                        failures,
                    )
                    # Remove from scheduler; callback handles DB/state cleanup
                    self.remove(bot_id)
                    if callback:
                        try:
                            callback(bot_id)
                        except Exception:
                            logger.exception("BotScheduler: on_fatal_error callback raised for bot %d", bot_id)
                    continue  # skip reschedule
                else:
                    logger.exception(
                        "BotScheduler: bot %d (%s) check failed (%d/%d), retry in %.0fs",
                        bot_id,
                        bot_name,
                        failures,
                        _MAX_CONSECUTIVE_FAILURES,
                        _BACKOFF_SEC,
                    )
                    delay = _BACKOFF_SEC

            with self._lock:
                if self._gens.get(bot_id, 0) == gen:  # bot wasn't stopped/removed
                    heapq.heappush(self._heap, (time.monotonic() + delay, gen, bot_id))
            self._wake.set()
