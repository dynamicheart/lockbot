"""
tests/core/test_scheduler.py - Unit tests for BotScheduler.

Verifies scheduling precision, cancellation, failure backoff,
and fatal-error callback without real time delays.
"""

import time
from unittest.mock import MagicMock, patch

from lockbot.core.scheduler import (
    _BACKOFF_SEC,
    _IDLE_INTERVAL,
    _MAX_CONSECUTIVE_FAILURES,
    BotScheduler,
)


def _make_instance(return_value=None):
    """Return a fake BotInstance whose _check_and_notify returns *return_value*."""
    inst = MagicMock()
    inst.bot._check_and_notify.return_value = return_value
    inst.config.get_val.return_value = "test_bot"
    return inst


# ── helpers ──────────────────────────────────────────────────────────────────


def _wait_for(condition, timeout=3.0, interval=0.02):
    """Spin-wait until *condition()* is True or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


# ── basic scheduling ──────────────────────────────────────────────────────────


class TestBasicScheduling:
    def test_check_fires_on_add(self):
        """First check should fire immediately (delay=0)."""
        inst = _make_instance(return_value=None)
        sched = BotScheduler()
        sched.start()
        try:
            sched.add(1, inst, delay=0.0)
            assert _wait_for(lambda: inst.bot._check_and_notify.call_count >= 1)
        finally:
            sched.stop()

    def test_idle_bot_rechecked_after_idle_interval(self):
        """Bot returning None should be rescheduled after _IDLE_INTERVAL."""
        inst = _make_instance(return_value=None)
        sched = BotScheduler()

        fired_at = []

        def recording_check():
            fired_at.append(time.monotonic())
            return None

        inst.bot._check_and_notify.side_effect = recording_check

        sched.start()
        try:
            sched.add(1, inst, delay=0.0)
            # Wait for 2 checks
            assert _wait_for(lambda: len(fired_at) >= 2, timeout=_IDLE_INTERVAL * 2 + 2)
            gap = fired_at[1] - fired_at[0]
            assert gap >= _IDLE_INTERVAL * 0.9, f"Expected ~{_IDLE_INTERVAL}s gap, got {gap:.1f}s"
        finally:
            sched.stop()

    def test_active_bot_uses_returned_delay(self):
        """Bot returning N seconds should be checked again in ~N seconds."""
        next_wakeup = 0.2  # fast for testing
        calls = []

        inst = MagicMock()
        inst.config.get_val.return_value = "bot"

        def check():
            calls.append(time.monotonic())
            return next_wakeup

        inst.bot._check_and_notify.side_effect = check

        sched = BotScheduler()
        sched.start()
        # Patch _MIN_INTERVAL so small test delays aren't clamped to 1s
        with patch("lockbot.core.scheduler._MIN_INTERVAL", 0.01):
            try:
                sched.add(1, inst, delay=0.0)
                assert _wait_for(lambda: len(calls) >= 4, timeout=3.0)
                # Gaps between consecutive checks should be ~next_wakeup
                gaps = [calls[i + 1] - calls[i] for i in range(len(calls) - 1)]
                for g in gaps:
                    assert g >= next_wakeup * 0.7, f"Gap too short: {g:.3f}s (expected ~{next_wakeup}s)"
                    assert g <= next_wakeup * 3.0, f"Gap too long: {g:.3f}s"
            finally:
                sched.stop()


# ── cancellation ─────────────────────────────────────────────────────────────


class TestCancellation:
    def test_remove_stops_checks(self):
        """After remove(), no more _check_and_notify calls."""
        next_wakeup = 0.1
        inst = _make_instance(return_value=next_wakeup)
        sched = BotScheduler()
        sched.start()
        try:
            sched.add(1, inst, delay=0.0)
            assert _wait_for(lambda: inst.bot._check_and_notify.call_count >= 1)
            sched.remove(1)
            count_at_remove = inst.bot._check_and_notify.call_count
            time.sleep(next_wakeup * 3)
            assert inst.bot._check_and_notify.call_count == count_at_remove, "check_and_notify called after remove()"
        finally:
            sched.stop()

    def test_add_after_remove_works(self):
        """Re-adding a bot after removal should work correctly."""
        inst = _make_instance(return_value=None)
        sched = BotScheduler()
        sched.start()
        try:
            sched.add(1, inst, delay=0.0)
            assert _wait_for(lambda: inst.bot._check_and_notify.call_count >= 1)
            sched.remove(1)

            inst2 = _make_instance(return_value=None)
            sched.add(1, inst2, delay=0.0)
            assert _wait_for(lambda: inst2.bot._check_and_notify.call_count >= 1)
            # Original instance should not be called again
            count_before = inst.bot._check_and_notify.call_count
            time.sleep(0.05)
            assert inst.bot._check_and_notify.call_count == count_before
        finally:
            sched.stop()


# ── failure handling ──────────────────────────────────────────────────────────


class TestFailureHandling:
    def test_single_failure_retries(self):
        """One exception → retry after _BACKOFF_SEC (not removed)."""
        calls = []

        inst = MagicMock()
        inst.config.get_val.return_value = "bot"

        def check():
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError("simulated failure")
            return None

        inst.bot._check_and_notify.side_effect = check

        sched = BotScheduler()
        sched.start()
        try:
            sched.add(1, inst, delay=0.0)
            # First call raises, second should come after _BACKOFF_SEC
            assert _wait_for(lambda: len(calls) >= 2, timeout=_BACKOFF_SEC + 5)
            gap = calls[1] - calls[0]
            assert gap >= _BACKOFF_SEC * 0.8, f"Backoff too short: {gap:.1f}s"
            assert 1 in sched._instances, "Bot should still be registered after 1 failure"
        finally:
            sched.stop()

    def test_max_failures_triggers_fatal_callback(self):
        """After _MAX_CONSECUTIVE_FAILURES, on_fatal_error is called once."""
        callback_calls = []
        inst = _make_instance()
        inst.bot._check_and_notify.side_effect = RuntimeError("always fails")

        def on_fatal(bot_id):
            callback_calls.append(bot_id)

        sched = BotScheduler()
        # Patch _BACKOFF_SEC to speed up the test
        with patch("lockbot.core.scheduler._BACKOFF_SEC", 0.05):
            sched.start()
            try:
                sched.add(1, inst, delay=0.0, on_fatal_error=on_fatal)
                assert _wait_for(
                    lambda: len(callback_calls) >= 1,
                    timeout=(_MAX_CONSECUTIVE_FAILURES + 2) * 0.1,
                )
                assert callback_calls == [1], f"Expected [1], got {callback_calls}"
                assert 1 not in sched._instances, "Bot should be removed after fatal"
                # No more calls after fatal
                time.sleep(0.15)
                total = inst.bot._check_and_notify.call_count
                time.sleep(0.15)
                assert inst.bot._check_and_notify.call_count == total
            finally:
                sched.stop()

    def test_success_resets_failure_count(self):
        """A successful check resets the failure counter."""
        call_num = [0]
        inst = MagicMock()
        inst.config.get_val.return_value = "bot"

        def check():
            call_num[0] += 1
            n = call_num[0]
            # Fail 4 times (below threshold), then succeed
            if n <= _MAX_CONSECUTIVE_FAILURES - 1:
                raise RuntimeError("temporary failure")
            return None

        inst.bot._check_and_notify.side_effect = check
        fatal_called = []

        with patch("lockbot.core.scheduler._BACKOFF_SEC", 0.05):
            sched = BotScheduler()
            sched.start()
            try:
                sched.add(1, inst, delay=0.0, on_fatal_error=lambda bot_id: fatal_called.append(bot_id))
                # Wait for enough calls
                assert _wait_for(lambda: call_num[0] >= _MAX_CONSECUTIVE_FAILURES, timeout=5)
                time.sleep(0.2)
                assert not fatal_called, "Fatal callback should NOT fire after a success resets count"
                assert 1 in sched._instances, "Bot should still be registered"
            finally:
                sched.stop()


# ── multiple bots ─────────────────────────────────────────────────────────────


class TestMultipleBots:
    def test_independent_scheduling(self):
        """Multiple bots are scheduled independently."""
        delay_a, delay_b = 0.1, 0.3
        inst_a = _make_instance(return_value=delay_a)
        inst_b = _make_instance(return_value=delay_b)

        sched = BotScheduler()
        sched.start()
        with patch("lockbot.core.scheduler._MIN_INTERVAL", 0.01):
            try:
                sched.add(1, inst_a, delay=0.0)
                sched.add(2, inst_b, delay=0.0)
                assert _wait_for(lambda: inst_a.bot._check_and_notify.call_count >= 5, timeout=5)
                assert _wait_for(lambda: inst_b.bot._check_and_notify.call_count >= 2, timeout=5)
                # bot_a (0.1s) fires roughly 3x more often than bot_b (0.3s)
                ratio = inst_a.bot._check_and_notify.call_count / max(inst_b.bot._check_and_notify.call_count, 1)
                assert 1.5 <= ratio <= 5.0, f"Unexpected call ratio: {ratio:.2f}"
            finally:
                sched.stop()

    def test_one_fatal_does_not_affect_other(self):
        """A fatal failure in bot A should not stop bot B."""
        inst_a = _make_instance()
        inst_a.bot._check_and_notify.side_effect = RuntimeError("always fails")
        inst_b = _make_instance(return_value=None)

        with patch("lockbot.core.scheduler._BACKOFF_SEC", 0.05):
            sched = BotScheduler()
            sched.start()
            try:
                sched.add(1, inst_a, delay=0.0)
                sched.add(2, inst_b, delay=0.0)
                # Wait for bot_a to go fatal
                assert _wait_for(lambda: 1 not in sched._instances, timeout=5)
                # Bot_b should still be running
                count_b = inst_b.bot._check_and_notify.call_count
                assert count_b >= 1, "Bot B should have been checked at least once"
                assert 2 in sched._instances
            finally:
                sched.stop()
