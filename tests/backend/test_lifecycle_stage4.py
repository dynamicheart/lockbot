"""Stage-4 snapshot cache single-flight + stale-publish guard.

The notification best-effort/coalescing/dispatched-not-delivered semantics and the time/queue
computation are already covered by test_resource_api_notifications.py and
test_resource_api_resources.py; stage 4 adds true concurrent single-flight to the snapshot cache
and the invalidate-version guard against a late refresh repopulating a retired generation.
"""

import threading
import time
from datetime import datetime, timezone

from lockbot.backend.app.resource_api.resources import _SnapshotCache


def test_concurrent_misses_collapse_onto_one_computation():
    """Single-flight: a burst of concurrent misses takes the locked deep copy once; followers
    reuse the leader's result rather than each computing their own."""
    cache = _SnapshotCache(ttl=60)
    calls = []
    started = threading.Event()
    release = threading.Event()

    def compute():
        calls.append(1)
        started.set()
        release.wait(5)
        return datetime.now(timezone.utc), {"n": 1}

    results = []

    def worker():
        results.append(cache.get_or_compute(1, 1, datetime.now(timezone.utc), compute))

    leader = threading.Thread(target=worker, name="leader")
    leader.start()
    assert started.wait(5)  # leader is inside compute; the in-flight marker is registered
    followers = [threading.Thread(target=worker, name=f"follower-{i}") for i in range(4)]
    for t in followers:
        t.start()
    time.sleep(0.05)  # let the followers reach the single wait
    release.set()
    for t in (leader, *followers):
        t.join(5)

    assert len(calls) == 1  # computed exactly once
    assert all(r[1] == {"n": 1} for r in results)
    assert any(r[2] for r in results)  # at least one follower was served from the leader's result


def test_invalidation_during_flight_is_not_republished():
    """A retirement that lands while a leader is computing must prevent it from publishing a
    now-stale entry for the retired generation (invalidate-version guard)."""
    cache = _SnapshotCache(ttl=60)
    started = threading.Event()
    release = threading.Event()

    def compute():
        started.set()
        release.wait(5)
        return datetime.now(timezone.utc), {"n": 1}

    result = []

    def worker():
        result.append(cache.get_or_compute(1, 1, datetime.now(timezone.utc), compute))

    t = threading.Thread(target=worker, name="leader")
    t.start()
    assert started.wait(5)
    cache.invalidate_generation(1, 1)  # the generation retires mid-flight
    release.set()
    t.join(5)

    assert result[0][1] == {"n": 1}  # the leader still returns its freshly computed copy
    assert cache.get(1, 1, datetime.now(timezone.utc)) is None  # but did not repopulate the cache
