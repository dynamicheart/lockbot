"""AC-7: admission, draining, and instance lifecycle coordination.

What these tests are defending:

- A lease bounds *waiting* work as well as executing work, so a burst that is all blocked
  on the bot's state lock still sheds instead of piling up.
- A lifecycle transition refuses new work immediately and waits for admitted work, so a
  retired instance cannot mutate state after its replacement has started.
- A client that disconnects does not release its slot or let a drain finish early.
- No second lock domain over bot state is introduced: the manager serializes metadata only.
- A drain that times out reports "may have executed", never a clean rollback.
"""

import contextlib
import threading
import time

import pytest
from lockbot.backend.app.bots.manager import (
    AdmissionFull,
    BotManager,
    BotNotRunning,
    InstanceRetiring,
    MayHaveExecuted,
)

TIMEOUT = 5.0
BOT_ID = 501


@pytest.fixture
def manager(tmp_path):
    mgr = BotManager(max_inflight=3, drain_timeout=1.0)
    yield mgr
    with contextlib.suppress(Exception):
        mgr.shutdown_all()


@pytest.fixture
def running(manager, tmp_path):
    manager.start_bot(
        BOT_ID,
        {
            "BOT_TYPE": "NODE",
            "BOT_NAME": "gated",
            "BOT_ID": BOT_ID,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": {"n1": "m1"},
        },
    )
    return manager


class TestAdmission:
    def test_admitting_yields_the_live_instance(self, running):
        with running.admit(BOT_ID) as lease:
            assert lease.instance is running.get_instance(BOT_ID)
            assert lease.bot is lease.instance.bot

    def test_a_lease_is_counted_while_held_and_released_after(self, running):
        assert running.inflight(BOT_ID) == 0
        with running.admit(BOT_ID):
            assert running.inflight(BOT_ID) == 1
        assert running.inflight(BOT_ID) == 0

    def test_a_lease_is_released_even_when_the_operation_raises(self, running):
        with pytest.raises(ValueError), running.admit(BOT_ID):
            raise ValueError("boom")
        assert running.inflight(BOT_ID) == 0

    def test_an_unknown_bot_is_refused(self, manager):
        with pytest.raises(BotNotRunning), manager.admit(9999):
            pass

    def test_the_bound_covers_work_that_is_only_waiting(self, running):
        """AC-7: admission must bound waiting and executing work, not just executing."""
        bot = running.get_instance(BOT_ID).bot
        release = threading.Event()
        admitted = threading.Semaphore(0)
        errors = []

        def blocked_worker():
            try:
                with running.admit(BOT_ID):
                    admitted.release()
                    with bot._lock:  # contending for the real state lock
                        release.wait(TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with bot._lock:  # nobody can make progress while this is held
            threads = [threading.Thread(target=blocked_worker, daemon=True) for _ in range(3)]
            for thread in threads:
                thread.start()
            for _ in range(3):
                assert admitted.acquire(timeout=TIMEOUT)

            # All three are admitted but none is executing. A fourth must shed.
            assert running.inflight(BOT_ID) == 3
            with pytest.raises(AdmissionFull), running.admit(BOT_ID):
                pass

            release.set()

        for thread in threads:
            thread.join(TIMEOUT)
        assert errors == []
        assert running.inflight(BOT_ID) == 0

    def test_shedding_frees_up_once_work_completes(self, running):
        release = threading.Event()
        holders = []

        def hold():
            with running.admit(BOT_ID):
                holders.append(1)
                release.wait(TIMEOUT)

        threads = [threading.Thread(target=hold, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + TIMEOUT
        while running.inflight(BOT_ID) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

        with pytest.raises(AdmissionFull), running.admit(BOT_ID):
            pass

        release.set()
        for thread in threads:
            thread.join(TIMEOUT)

        with running.admit(BOT_ID):
            pass  # accepted again


class TestDraining:
    def test_new_work_is_refused_once_retiring_begins(self, running):
        release = threading.Event()
        inside = threading.Event()
        stop_result = {}

        def holder():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)

        def stopper():
            try:
                running.stop_bot(BOT_ID, drain_timeout=TIMEOUT)
                stop_result["ok"] = True
            except Exception as exc:  # noqa: BLE001
                stop_result["error"] = exc

        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        assert inside.wait(TIMEOUT)

        stop_thread = threading.Thread(target=stopper, daemon=True)
        stop_thread.start()

        deadline = time.monotonic() + TIMEOUT
        while not running.is_retiring(BOT_ID) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert running.is_retiring(BOT_ID)

        with pytest.raises(InstanceRetiring), running.admit(BOT_ID):
            pass

        release.set()
        holder_thread.join(TIMEOUT)
        stop_thread.join(TIMEOUT)
        assert stop_result.get("ok") is True

    def test_stop_waits_for_admitted_work_before_dropping_the_instance(self, running):
        inside = threading.Event()
        release = threading.Event()
        instance_seen_during_work = {}

        def holder():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)
                instance_seen_during_work["still_registered"] = running.get_instance(BOT_ID) is not None

        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        assert inside.wait(TIMEOUT)

        stop_thread = threading.Thread(target=lambda: running.stop_bot(BOT_ID, drain_timeout=TIMEOUT), daemon=True)
        stop_thread.start()
        stop_thread.join(0.3)
        assert stop_thread.is_alive(), "stop_bot returned before the admitted operation finished"

        release.set()
        holder_thread.join(TIMEOUT)
        stop_thread.join(TIMEOUT)

        assert instance_seen_during_work["still_registered"] is True
        assert running.get_instance(BOT_ID) is None

    def test_a_timed_out_drain_reports_may_have_executed(self, running):
        inside = threading.Event()
        release = threading.Event()

        def holder():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert inside.wait(TIMEOUT)

        with pytest.raises(MayHaveExecuted) as exc:
            running.stop_bot(BOT_ID, drain_timeout=0.1)

        assert "may have executed" in str(exc.value).lower()
        assert "not a rollback" in str(exc.value).lower()
        assert "/resources" in str(exc.value)

        release.set()
        thread.join(TIMEOUT)

    def test_a_timed_out_drain_leaves_the_instance_in_place(self, running):
        """Replacement would be unsafe while a worker may still persist state."""
        inside = threading.Event()
        release = threading.Event()

        def holder():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert inside.wait(TIMEOUT)

        with pytest.raises(MayHaveExecuted):
            running.stop_bot(BOT_ID, drain_timeout=0.1)

        assert running.get_instance(BOT_ID) is not None
        assert running.is_retiring(BOT_ID) is True

        release.set()
        thread.join(TIMEOUT)

    def test_a_client_timeout_does_not_free_the_slot_early(self, running):
        """The worker owns completion; an abandoned caller changes nothing."""
        inside = threading.Event()
        release = threading.Event()

        def worker():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)  # the HTTP client has long since given up

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert inside.wait(TIMEOUT)

        assert running.inflight(BOT_ID) == 1
        with pytest.raises(MayHaveExecuted):
            running.stop_bot(BOT_ID, drain_timeout=0.1)

        release.set()
        thread.join(TIMEOUT)
        assert running.inflight(BOT_ID) == 0


class TestGenerations:
    def test_restart_bumps_the_generation(self, running, tmp_path):
        before = running.generation(BOT_ID)
        running.restart_bot(
            BOT_ID,
            {
                "BOT_TYPE": "NODE",
                "BOT_NAME": "gated",
                "BOT_ID": BOT_ID,
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"n1": "m1"},
            },
        )
        assert running.generation(BOT_ID) > before

    def test_a_stale_lease_is_detectable_after_replacement(self, running, tmp_path):
        with running.admit(BOT_ID) as lease:
            assert running.lease_is_current(lease)
            stale = lease

        running.restart_bot(
            BOT_ID,
            {
                "BOT_TYPE": "NODE",
                "BOT_NAME": "gated",
                "BOT_ID": BOT_ID,
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"n1": "m1"},
            },
        )
        assert not running.lease_is_current(stale), "a retired instance's lease must not look current"

    def test_restart_clears_retiring(self, running, tmp_path):
        running.restart_bot(
            BOT_ID,
            {
                "BOT_TYPE": "NODE",
                "BOT_NAME": "gated",
                "BOT_ID": BOT_ID,
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"n1": "m1"},
            },
        )
        assert running.is_retiring(BOT_ID) is False
        with running.admit(BOT_ID):
            pass


class TestNoSecondLockDomain:
    def test_admission_does_not_touch_the_bot_state_lock(self, running):
        """The manager serializes metadata only; state stays behind bot._lock alone."""
        bot = running.get_instance(BOT_ID).bot
        with bot._lock:
            # If admission tried to take the state lock, this would block forever.
            with running.admit(BOT_ID) as lease:
                assert lease.bot is bot
            assert running.inflight(BOT_ID) == 0

    def test_the_state_lock_is_still_the_only_state_guard(self, running):
        bot = running.get_instance(BOT_ID).bot
        with running.admit(BOT_ID):
            # A lease is not an exclusive permit over state: the lock is still required.
            assert bot._lock.acquire(blocking=False)
            bot._lock.release()

    def test_drain_does_not_hold_the_manager_lock_while_waiting(self, running):
        """Workers need that lock to decrement; holding it across the wait would deadlock."""
        inside = threading.Event()
        release = threading.Event()

        def holder():
            with running.admit(BOT_ID):
                inside.set()
                release.wait(TIMEOUT)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert inside.wait(TIMEOUT)

        stop_thread = threading.Thread(target=lambda: running.stop_bot(BOT_ID, drain_timeout=TIMEOUT), daemon=True)
        stop_thread.start()
        time.sleep(0.1)

        # Readable while the drain is waiting, which proves the lock is not held.
        assert running.inflight(BOT_ID) == 1

        release.set()
        thread.join(TIMEOUT)
        stop_thread.join(TIMEOUT)
        assert running.get_instance(BOT_ID) is None
