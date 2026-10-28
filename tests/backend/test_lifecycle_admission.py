"""AC-7: the managed scheduler sweep and construction run under the admission protocol.

These pin the generation-serialization guarantees the manager was designed for but did not
wire up: a sweep holds a lease (so a drain waits for it and a replacement cannot be published
under it), admission is reserved for the sweep (API load cannot shed it), instances are built
outside the registry lock, and a replacement is refused while a generation is retiring.
"""

import heapq
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager, BotNotRunning, InstanceRetiring
from lockbot.core.scheduler import BotScheduler


def _fake_instance(check):
    return SimpleNamespace(
        bot=SimpleNamespace(_check_and_notify=check),
        config=SimpleNamespace(get_val=lambda *_a, **_k: "fake"),
    )


class TestSchedulerSweepAdmission:
    def test_managed_sweep_runs_inside_the_lease(self):
        held = []

        @contextmanager
        def admission(_bot_id):
            held.append("enter")
            try:
                yield
            finally:
                held.append("exit")

        def check():
            held.append("check")
            return None

        sched = BotScheduler()
        sched.add(7, _fake_instance(check), delay=0.0, admission=admission)
        sched._fire_due()
        assert held == ["enter", "check", "exit"], "the sweep must run between lease enter and exit"

    def test_refused_sweep_is_skipped_without_a_failure_bump(self):
        @contextmanager
        def refusing(_bot_id):
            raise RuntimeError("retiring")
            yield  # pragma: no cover

        check = MagicMock()
        sched = BotScheduler()
        sched.add(7, _fake_instance(check), delay=0.0, admission=refusing)
        sched._fire_due()
        check.assert_not_called()
        assert sched._failure_counts.get(7, 0) == 0, "an admission refusal is not a bot failure"

    def test_standalone_sweep_calls_check_directly(self):
        check = MagicMock(return_value=None)
        sched = BotScheduler()
        sched.add(7, _fake_instance(check), delay=0.0)  # no admission injected
        sched._fire_due()
        check.assert_called_once()


class TestConstructOutsideLock:
    def test_instance_is_built_before_the_registry_lock_is_taken(self):
        mgr = BotManager()
        observed = {}

        def construct(*_a, **_k):
            # If construction ran under manager._lock this acquire would fail.
            got = mgr._lock.acquire(blocking=False)
            observed["lock_free_during_construction"] = got
            if got:
                mgr._lock.release()
            return MagicMock()

        with patch("lockbot.backend.app.bots.manager.BotInstance", side_effect=construct):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE", "PLATFORM": "infoflow"})
        assert observed["lock_free_during_construction"] is True

    def test_start_is_refused_while_a_generation_is_retiring(self):
        mgr = BotManager()
        mgr._retiring.add(1)
        with (
            patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()),
            pytest.raises(InstanceRetiring),
        ):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})

    def test_second_start_for_a_running_bot_is_refused(self):
        mgr = BotManager()
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})
            with pytest.raises(RuntimeError, match="already running"):
                mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})


class TestSweepReservedCapacity:
    def test_sweep_lease_is_never_shed_for_fullness(self):
        mgr = BotManager(max_inflight=1)
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})
        with mgr.admit(1):  # fills the cap of one
            # A capped admit is now shed, but the reserved sweep lease still goes through.
            from lockbot.backend.app.bots.manager import AdmissionFull

            with pytest.raises(AdmissionFull), mgr.admit(1):
                pass
            with mgr.admit(1, enforce_cap=False):
                assert mgr.inflight(1) == 2

    def test_reserved_admit_still_refuses_a_retiring_bot(self):
        mgr = BotManager()
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})
        mgr._begin_retiring(1)
        with pytest.raises(InstanceRetiring), mgr.admit(1, enforce_cap=False):
            pass


class TestDrainWaitsForSweep:
    def test_stop_blocks_until_an_in_flight_sweep_releases_its_lease(self):
        mgr = BotManager(drain_timeout=5.0)
        started = threading.Event()
        release = threading.Event()

        def blocking_check():
            started.set()
            release.wait(5.0)
            return None

        inst = MagicMock()
        inst.bot._check_and_notify.side_effect = blocking_check
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=inst):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})

        # Run one sweep on a background thread, standing in for the scheduler thread.
        sweeper = threading.Thread(target=mgr._scheduler._fire_due)
        sweeper.start()
        assert started.wait(5.0), "the sweep should have started"
        assert mgr.inflight(1) == 1, "the sweep must hold a lease while it runs"

        stopped = threading.Event()
        threading.Thread(target=lambda: (mgr.stop_bot(1, drain_timeout=5.0), stopped.set())).start()

        # The drain cannot complete while the sweep lease is still held.
        assert not stopped.wait(0.3), "stop must wait for the in-flight sweep"
        release.set()
        assert stopped.wait(5.0), "stop should finish once the sweep lease releases"
        sweeper.join(5.0)
        assert mgr.inflight(1) == 0


class TestGenerationSafePublication:
    """M1/B16: scheduler.add() runs after manager._lock is released, so a concurrent stop+start
    can publish a newer generation first. A late add from the superseded start must not overwrite
    it, and the managed sweep must act on the manager's current instance, not a stale snapshot."""

    def test_stale_add_does_not_overwrite_a_newer_registration(self):
        sched = BotScheduler()
        inst_a = _fake_instance(MagicMock())
        inst_b = _fake_instance(MagicMock())

        sched.add(1, inst_b, generation=2)  # newer generation registered first
        sched.add(1, inst_a, generation=1)  # superseded start's late add arrives after

        assert sched._instances[1] is inst_b, "a lower-generation add must not overwrite a newer one"
        assert sched._mgr_gens[1] == 2

    def test_higher_generation_always_wins_regardless_of_arrival_order(self):
        sched = BotScheduler()
        inst_a = _fake_instance(MagicMock())
        inst_b = _fake_instance(MagicMock())

        sched.add(1, inst_a, generation=1)
        sched.add(1, inst_b, generation=2)  # newer arrives later and must take over

        assert sched._instances[1] is inst_b
        assert sched._mgr_gens[1] == 2

    def test_stop_start_interleaving_leaves_registry_and_scheduler_consistent(self):
        """Reproduce the reviewer's race: start B publishes gen 2 and registers it, then the
        paused start A resumes and issues its late gen-1 add. Both the manager registry and the
        scheduler must still point at B."""
        mgr = BotManager()
        inst_a = MagicMock(name="A")
        inst_b = MagicMock(name="B")
        instances = iter([inst_a, inst_b])

        with patch(
            "lockbot.backend.app.bots.manager.BotInstance",
            side_effect=lambda *a, **k: next(instances),
        ):
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})  # A, gen 1
            mgr.stop_bot(1)  # retire A; manager generation stays 1 (monotonic)
            mgr.start_bot(bot_id=1, config_dict={"BOT_TYPE": "NODE"})  # B, gen 2

        # Start A's late registration, racing in after B was published.
        mgr._scheduler.add(
            1, _fake_instance(MagicMock()), admission=lambda bid: mgr.admit(bid, enforce_cap=False), generation=1
        )

        assert mgr._bots[1] is inst_b
        assert mgr._scheduler._instances[1] is inst_b, "scheduler must not hold the stale A instance"
        assert mgr._scheduler._mgr_gens[1] == mgr.generation(1) == 2

    def test_managed_sweep_targets_the_lease_instance_not_the_registration(self):
        """Even if a stale instance lingers in the registration, the sweep runs the instance the
        lease was admitted against (the manager's current one)."""
        stale_check = MagicMock(name="stale")
        current_check = MagicMock(name="current", return_value=None)
        current_instance = _fake_instance(current_check)

        @contextmanager
        def admission_yielding_current(_bot_id):
            # Managed leases expose the current instance as lease.bot.
            yield SimpleNamespace(bot=current_instance.bot)

        sched = BotScheduler()
        # A stale registration lingers (simulating the mid-race window).
        sched._instances[1] = _fake_instance(stale_check)
        sched._admissions[1] = admission_yielding_current
        sched._failure_counts[1] = 0
        sched._gens[1] = 0
        heapq.heappush(sched._heap, (time.monotonic(), 0, 1))

        sched._fire_due()

        current_check.assert_called_once()
        stale_check.assert_not_called()


class TestFailedGenerationRetention:
    """M1/B4/B6: a generation that exhausts its sweep budget is retained read-only so
    /resources can report health=error, writes are refused, a replacement is refused until it is
    stopped, and stop drains ordinary leases then evicts the record and fires the retire hook."""

    @staticmethod
    def _fail(mgr, bot_id):
        # Mimic the scheduler on fatal: it removes the registration, then invokes the callback.
        mgr._scheduler.remove(bot_id)
        mgr._make_fatal_error_handler(bot_id)(bot_id)

    def _start(self, mgr, bot_id=1, inst=None):
        inst = inst or MagicMock()
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=inst):
            mgr.start_bot(bot_id, {"BOT_TYPE": "NODE"})
        return inst

    def test_failed_generation_is_retained_for_read_but_refuses_writes(self):
        mgr = BotManager()
        inst = self._start(mgr)
        self._fail(mgr, 1)

        assert 1 in mgr._failed
        assert mgr.is_running(1) is False
        # A read lease is served against the retained failed instance.
        with mgr.admit(1, for_read=True) as lease:
            assert lease.bot is inst.bot
        # A write (default) is refused.
        with pytest.raises(BotNotRunning), mgr.admit(1):
            pass

    def test_start_is_refused_while_a_failed_generation_is_retained(self):
        mgr = BotManager()
        self._start(mgr)
        self._fail(mgr, 1)
        with (
            patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()),
            pytest.raises(InstanceRetiring),
        ):
            mgr.start_bot(1, {"BOT_TYPE": "NODE"})

    def test_stop_evicts_the_failed_record_fires_hook_and_allows_restart(self):
        mgr = BotManager()
        fired = []
        mgr.register_retire_hook(lambda bid, gen: fired.append((bid, gen)))
        self._start(mgr)
        self._fail(mgr, 1)

        mgr.stop_bot(1)
        assert 1 not in mgr._failed
        assert fired and fired[0][0] == 1
        # A restart can now publish a fresh generation.
        self._start(mgr)
        assert mgr.is_running(1) is True

    def test_stop_waits_for_an_in_flight_failed_read(self):
        mgr = BotManager(drain_timeout=5.0)
        self._start(mgr)
        self._fail(mgr, 1)

        acquired = threading.Event()
        release = threading.Event()

        def hold_read():
            with mgr.admit(1, for_read=True):
                acquired.set()
                release.wait(5.0)

        reader = threading.Thread(target=hold_read)
        reader.start()
        assert acquired.wait(5.0)
        assert mgr.inflight(1) == 1

        stopped = threading.Event()
        threading.Thread(target=lambda: (mgr.stop_bot(1, drain_timeout=5.0), stopped.set())).start()
        assert not stopped.wait(0.3), "stop must wait for the in-flight failed-read lease"
        release.set()
        assert stopped.wait(5.0)
        reader.join(5.0)
        assert 1 not in mgr._failed

    def test_restart_recovers_a_failed_generation(self):
        mgr = BotManager()
        self._start(mgr)
        self._fail(mgr, 1)
        assert 1 in mgr._failed
        # restart is the one-step recovery: it stops (drains + evicts) the failed generation,
        # then publishes a fresh one.
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            mgr.restart_bot(1, {"BOT_TYPE": "NODE"})
        assert 1 not in mgr._failed
        assert mgr.is_running(1) is True
