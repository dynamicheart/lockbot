"""Window timestamps follow lock acquisition order across minute boundaries."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from lockbot.backend.app.service_traffic import TrafficStats


class DelayedAcquire:
    """Pause one caller before acquiring the real lock, without blocking others."""

    def __init__(self):
        self.lock = threading.Lock()
        self.target = None
        self.armed = False
        self.waiting = threading.Event()
        self.proceed = threading.Event()

    def __enter__(self):
        if self.armed and threading.get_ident() == self.target:
            self.armed = False
            self.waiting.set()
            assert self.proceed.wait(5), "Delayed caller was not released"
        self.lock.acquire()
        return self

    def __exit__(self, *_):
        self.lock.release()


class DelayedHistory:
    def __init__(self, gate):
        self.gate = gate

    def record(self, **_):
        self.operation()

    def operation(self, *_):
        # Journal callbacks must remain outside the aggregate counter lock.
        assert not self.gate.lock.locked()
        if threading.get_ident() == self.gate.target:
            self.gate.armed = True


@pytest.mark.parametrize("operation", [False, True], ids=["requests", "operations"])
def test_delayed_writer_cannot_remove_a_newer_minute(operation):
    now = [179.0]
    gate = DelayedAcquire()
    stats = TrafficStats(clock=lambda: now[0], history=DelayedHistory(gate))
    stats.lock = gate

    def record():
        if operation:
            stats.record_operation(bot_id=1, source="agent", command="lock", outcome="dispatched")
        else:
            stats.record(bot_id=1, source="agent", route="GET /known", status=200, duration_ms=1)

    def delayed_record():
        gate.target = threading.get_ident()
        record()

    with ThreadPoolExecutor(max_workers=1) as pool:
        delayed = pool.submit(delayed_record)
        try:
            assert gate.waiting.wait(5), "Writer did not reach its acquisition barrier"
            now[0] = 180.0
            record()
        finally:
            gate.proceed.set()
        delayed.result(timeout=5)

    for body in (stats.snapshot(), stats.snapshot(bot_id=1)):
        if operation:
            operations = body["business_operations"]
            assert operations["total"] == 2
            assert sum(row["sources"]["agent"] for row in operations["history"]) == 2
            assert operations["history"][-1]["sources"]["agent"] == 2
        else:
            assert body["totals"]["requests"] == 2
            assert sum(row["requests"] for row in body["sources"]) == 2
            assert sum(row["requests"] for row in body["history"]) == 2
            assert body["history"][-1]["requests"] == 2
            assert body["last_minute"]["requests"] == 2


def test_delayed_snapshot_captures_the_locked_window():
    now = [179.0]
    stats = TrafficStats(clock=lambda: now[0], scoped=False)
    gate = DelayedAcquire()
    stats.lock = gate

    def delayed_snapshot():
        gate.target = threading.get_ident()
        gate.armed = True
        return stats.snapshot()

    with ThreadPoolExecutor(max_workers=1) as pool:
        snapshot = pool.submit(delayed_snapshot)
        try:
            assert gate.waiting.wait(5), "Snapshot did not reach its acquisition barrier"
            now[0] = 180.0
            stats.record(source="agent", route="GET /known", status=200, duration_ms=1)
            stats.record_operation(bot_id=1, source="agent", command="lock", outcome="dispatched")
        finally:
            gate.proceed.set()
        body = snapshot.result(timeout=5)

    assert body["captured_at"] == "1970-01-01T00:03:00+00:00"
    assert body["totals"]["requests"] == body["history"][-1]["requests"] == 1
    assert body["last_minute"]["requests"] == 1
    assert body["business_operations"]["total"] == 1
    assert body["business_operations"]["history"][-1]["sources"]["agent"] == 1
