"""AC-6 / AC-6.1: state must be read as one consistent detached copy.

The pre-existing defect these tests close: `GET /api/bots/{id}/locked-users` read
`instance.bot.state.bot_state` live, with no lock, while chat commands and the expiry
sweep mutate that same dict inside `bot._lock`. A multi-resource command could therefore
be observed half-applied.

`state_snapshot()` is the fix. The tests below are deterministic — they use events and
barriers to construct the exact interleaving, not sleeps.
"""

import threading

import pytest
from lockbot.core.bot_instance import BotInstance

TIMEOUT = 5.0


@pytest.fixture
def bot(tmp_path):
    instance = BotInstance(
        "NODE",
        {"BOT_NAME": "snap", "BOT_ID": 7001, "DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"}},
    )
    return instance.bot


class _TornWriter:
    """Mutates two nodes inside one critical section, pausing between them on demand."""

    def __init__(self, bot):
        self.bot = bot
        self.inside = threading.Event()
        self.may_finish = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.bot._lock:
            self.bot.state.bot_state["n1"]["status"] = "exclusive"
            self.inside.set()
            self.may_finish.wait(TIMEOUT)
            self.bot.state.bot_state["n2"]["status"] = "exclusive"
        self.done.set()

    def __enter__(self):
        self.thread.start()
        assert self.inside.wait(TIMEOUT), "writer never entered its critical section"
        return self

    def __exit__(self, *exc):
        self.may_finish.set()
        assert self.done.wait(TIMEOUT), "writer never finished"
        self.thread.join(TIMEOUT)


def test_unlocked_read_can_observe_a_half_applied_change(bot):
    """Establishes that the hazard is real, so the next test is not vacuous."""
    with _TornWriter(bot):
        torn = dict(bot.state.bot_state)
        assert torn["n1"]["status"] == "exclusive"
        assert torn["n2"]["status"] == "idle", "expected to catch the writer mid-change"


def test_snapshot_waits_for_the_whole_critical_section(bot):
    """AC-6: a concurrent mutation cannot produce a half-applied multi-resource change."""
    result = {}
    reader_started = threading.Event()

    def reader():
        reader_started.set()
        result["captured_at"], result["state"] = bot.state_snapshot()

    with _TornWriter(bot) as writer:
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        assert reader_started.wait(TIMEOUT)
        # The reader is now blocked on the lock the writer holds.
        assert not writer.done.is_set()
        assert "state" not in result, "the snapshot must not have been taken yet"

    thread.join(TIMEOUT)

    assert result["state"]["n1"]["status"] == "exclusive"
    assert result["state"]["n2"]["status"] == "exclusive", "snapshot observed a torn state"


def test_snapshot_is_detached_from_live_state(bot):
    _captured_at, snapshot = bot.state_snapshot()
    bot.state.bot_state["n1"]["status"] = "exclusive"
    bot.state.bot_state["n1"]["current_users"].append({"user_id": "later"})

    assert snapshot["n1"]["status"] == "idle"
    assert snapshot["n1"]["current_users"] == []


def test_snapshot_mutation_does_not_leak_into_live_state(bot):
    _captured_at, snapshot = bot.state_snapshot()
    snapshot["n1"]["status"] = "tampered"
    snapshot["n1"]["current_users"].append({"user_id": "ghost"})

    assert bot.state.bot_state["n1"]["status"] == "idle"
    assert bot.state.bot_state["n1"]["current_users"] == []


def test_captured_at_is_a_single_utc_instant(bot):
    captured_at, _state = bot.state_snapshot()
    assert captured_at.tzinfo is not None
    assert captured_at.utcoffset().total_seconds() == 0


def test_snapshot_releases_the_lock(bot):
    """A non-reentrant lock would stay held on a buggy implementation."""
    bot.state_snapshot()
    assert bot._lock.acquire(blocking=False)
    bot._lock.release()


def test_snapshot_does_not_reenter_the_lock(bot):
    """AC-7 / task5: re-entering a non-reentrant lock self-deadlocks."""
    acquisitions = []
    real_lock = bot._lock

    class CountingLock:
        def __enter__(self):
            acquired = real_lock.acquire(blocking=False)
            assert acquired, "state_snapshot re-entered the non-reentrant state lock"
            acquisitions.append(1)
            return self

        def __exit__(self, *exc):
            real_lock.release()

    bot._lock = CountingLock()
    try:
        bot.state_snapshot()
    finally:
        bot._lock = real_lock
    assert sum(acquisitions) == 1


@pytest.mark.parametrize("bot_type", ["NODE", "QUEUE", "DEVICE"])
def test_snapshot_works_for_every_bot_type(bot_type, tmp_path):
    configs = {"n1": ["gpu"] * 2} if bot_type == "DEVICE" else {"n1": "m1"}
    instance = BotInstance(
        bot_type,
        {"BOT_NAME": f"snap-{bot_type}", "BOT_ID": 7002, "DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": configs},
    )
    captured_at, snapshot = instance.bot.state_snapshot()
    assert captured_at is not None
    assert "n1" in snapshot
