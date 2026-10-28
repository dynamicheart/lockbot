"""
scheduler.py - Min-heap based bot scheduler.

Replaces per-bot threading.Timer with a single background thread.
BotManager owns the shared instance and supplies admission for every sweep.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lockbot.core.bot_instance import BotInstance

logger = logging.getLogger(__name__)

_IDLE_INTERVAL = 60.0  # seconds between checks when no active locks
_MIN_INTERVAL = 1.0  # minimum reschedule interval (prevent busy-loop)
_BACKOFF_SEC = 30.0  # retry delay after _check_and_notify raises
_MAX_CONSECUTIVE_FAILURES = 5  # failures before on_fatal_error is triggered


class _AdmissionRefused(Exception):
    """A managed sweep could not be admitted (bot retiring or stopped). Not a bot failure."""


class BotScheduler:
    """Single background thread + min-heap driving all bot timer checks.

    Every sweep requires a platform admission lease.
    """

    def __init__(self) -> None:
        self._instances: dict[int, BotInstance] = {}
        # heap entries: (fire_at, generation, bot_id)
        self._heap: list[tuple[float, int, int]] = []
        self._gens: dict[int, int] = {}  # bot_id → current generation
        self._failure_counts: dict[int, int] = {}  # bot_id → consecutive failure count
        self._callbacks: dict[int, Callable[[int], None]] = {}  # bot_id → on_fatal_error
        # bot_id → manager admission factory. Each sweep holds a lifecycle lease;
        # a missing admission means the registration is retired.
        self._admissions: dict[int, Callable[[int], object]] = {}
        # bot_id → highest manager generation ever registered. A high-water mark (never cleared
        # on remove) so a late ``add`` from a superseded start cannot overwrite a newer instance.
        self._mgr_gens: dict[int, int] = {}
        self._lock = threading.Lock()
        # Per-thread "expected manager generation" for the next remove() call. A remover
        # (lifecycle stop or the scheduler's own fatal path) captures the generation it intends
        # to retire and, via remove_expecting(), asks remove() to pop ONLY if the currently
        # registered generation still matches. Thread-local so two concurrent removers for the
        # same bot (e.g. a paused stop and a live fatal sweep) do not clobber each other's
        # expectation. Unset ⇒ unconditional remove (backward compatible).
        self._remove_expectation = threading.local()
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
        admission: Callable[[int], object] | None = None,
        generation: int = 0,
    ) -> None:
        """Register *instance* under *bot_id* and schedule its first check after *delay* seconds.

        Args:
            on_fatal_error: Optional callback invoked when the bot has failed
                _MAX_CONSECUTIVE_FAILURES times in a row.  Receives bot_id.
                Called from the scheduler thread — must not block for long.
            admission: Optional context-manager factory (``admission(bot_id)``) that the sweep
                enters around ``_check_and_notify``. Managed mode injects the manager's lease so
                a lifecycle drain waits for an in-flight sweep and a replacement cannot be
                published under it. If entering raises, the sweep is skipped (not a failure).
                The entered lease should expose the current instance as ``lease.bot`` so the
                sweep always acts on the manager's current object, not this registration snapshot.
            generation: The manager generation of *instance*. Monotonic per bot. A registration
                whose generation is lower than one already seen is a superseded start racing a
                newer one; it is ignored so it cannot overwrite the current instance.
        """
        with self._lock:
            if generation < self._mgr_gens.get(bot_id, 0):
                # A newer generation is already (or was) registered; this ``add`` lost the race
                # with a concurrent stop+restart. Dropping it keeps the registry and the manager
                # pointing at the same, newest instance.
                logger.debug(
                    "BotScheduler: ignoring stale add for bot %d (gen %d < seen %d)",
                    bot_id,
                    generation,
                    self._mgr_gens.get(bot_id, 0),
                )
                return
            self._mgr_gens[bot_id] = generation
            self._instances[bot_id] = instance
            self._failure_counts[bot_id] = 0
            if on_fatal_error is not None:
                self._callbacks[bot_id] = on_fatal_error
            if admission is not None:
                self._admissions[bot_id] = admission
            gen = self._gens.get(bot_id, 0)
            heapq.heappush(self._heap, (time.monotonic() + delay, gen, bot_id))
        self._wake.set()

    def remove(self, bot_id: int) -> None:
        """Unregister *bot_id* and cancel all pending checks (lazy deletion via generation bump).

        If the caller is inside ``remove_expecting(gen)``, the registration is popped only when
        the currently registered manager generation still equals ``gen``. This makes a stale
        remove (a delayed stop, or an old fatal sweep that resumed after a stop+start published a
        replacement) a no-op instead of unscheduling the replacement. Without an expectation the
        remove is unconditional.
        """
        expected = getattr(self._remove_expectation, "gen", None)
        with self._lock:
            if expected is not None and self._mgr_gens.get(bot_id, 0) != expected:
                logger.debug(
                    "BotScheduler: skipping stale remove for bot %d (expected gen %d, current %d)",
                    bot_id,
                    expected,
                    self._mgr_gens.get(bot_id, 0),
                )
                return
            self._gens[bot_id] = self._gens.get(bot_id, 0) + 1
            self._instances.pop(bot_id, None)
            self._failure_counts.pop(bot_id, None)
            self._callbacks.pop(bot_id, None)
            self._admissions.pop(bot_id, None)

    @contextmanager
    def remove_expecting(self, generation: int):
        """Scope a generation expectation for ``remove`` calls on this thread (see ``remove``).

        A thread-local (rather than a ``remove`` argument) is used deliberately: ``remove`` must
        stay callable as ``remove(bot_id)`` so a paused lifecycle test can wrap it, and two
        concurrent removers for the same bot (a paused stop and a live fatal sweep) each need
        their own expectation. The previous value is saved/restored so nesting is safe.
        """
        previous = getattr(self._remove_expectation, "gen", None)
        self._remove_expectation.gen = generation
        try:
            yield
        finally:
            self._remove_expectation.gen = previous

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

    def _run_check(self, bot_id: int, instance: BotInstance) -> float | None:
        """Run one sweep under the platform admission lease.

        Raises :class:`_AdmissionRefused` if a managed lease could not be taken (the bot is
        retiring or gone); any exception from ``_check_and_notify`` itself propagates as a
        normal sweep failure.
        """
        admission = self._admissions.get(bot_id)
        if admission is None:
            # A removed/retiring registration must never run outside a lease.
            raise _AdmissionRefused
        # The due entry captured *instance*. If it is no longer the current registration (a
        # stop+start published a replacement while this sweep was pending), skip: a stale due
        # entry must not sweep the replacement, and the replacement has its own due entry.
        with self._lock:
            if self._instances.get(bot_id) is not instance:
                raise _AdmissionRefused
        try:
            lease_cm = admission(bot_id)
            lease = lease_cm.__enter__()
        except Exception as exc:
            raise _AdmissionRefused from exc
        try:
            # Sweep the instance the lease was admitted against — the manager's *current* one —
            # not this registration snapshot. If a stale registration briefly lingers (a start
            # racing a stop+restart), the lease still points at the live instance, so a retired
            # object is never swept. Fall back to the registered instance if the lease does not
            # expose one.
            target_bot = getattr(lease, "bot", None)
            if target_bot is None:
                target_bot = instance.bot
            return target_bot._check_and_notify()
        finally:
            # The manager's lease decrements inflight unconditionally; exc info is not needed.
            lease_cm.__exit__(None, None, None)

    def _fire_due(self) -> None:
        """Pop all due entries and run their checks outside the lock."""
        due: list[tuple[int, int, int, BotInstance]] = []
        now = time.monotonic()
        with self._lock:
            while self._heap and self._heap[0][0] <= now:
                fire_at, gen, bot_id = heapq.heappop(self._heap)
                if gen == self._gens.get(bot_id, 0):
                    # Capture the instance WITH the due item, under the lock, so a later
                    # dictionary lookup cannot retrieve a replacement published while another
                    # bot's sweep was running.
                    due.append((bot_id, gen, self._mgr_gens.get(bot_id, 0), self._instances.get(bot_id)))

        for bot_id, gen, mgr_gen, instance in due:
            if instance is None:
                continue

            try:
                next_in = self._run_check(bot_id, instance)
                delay = max(_MIN_INTERVAL, next_in) if next_in is not None else _IDLE_INTERVAL
                # Success: reset the consecutive failure count, but only for the registration
                # that actually ran. A stale success (a sweep that completed, then a stop+start
                # published a replacement) must not reset the replacement's count.
                with self._lock:
                    if self._gens.get(bot_id, 0) == gen:
                        self._failure_counts[bot_id] = 0
            except _AdmissionRefused:
                # The bot is retiring or stopped; this is not a bot failure. Skip the sweep
                # and let the next idle tick try again (remove() cancels it if truly gone).
                logger.debug("BotScheduler: sweep for bot %d skipped (admission refused)", bot_id)
                with self._lock:
                    if self._gens.get(bot_id, 0) == gen:
                        heapq.heappush(self._heap, (time.monotonic() + _IDLE_INTERVAL, gen, bot_id))
                self._wake.set()
                continue
            except Exception:
                bot_name = instance.config.get_val("BOT_NAME") if hasattr(instance, "config") else "?"
                with self._lock:
                    # Only account this failure against the registration that actually ran. If a
                    # stop/start replaced the generation while this sweep was in flight, its
                    # failure (and any fatal removal/callback) must not touch the replacement's
                    # counters or registration.
                    if self._gens.get(bot_id, 0) != gen:
                        continue
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
                    # Remove from scheduler, but only if this exact generation is still current:
                    # a stop+start during this fatal sweep must not have its replacement
                    # unscheduled. The manager fatal callback handles DB/state cleanup (itself
                    # generation-bound).
                    with self.remove_expecting(mgr_gen):
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
