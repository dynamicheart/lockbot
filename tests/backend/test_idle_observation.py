import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.resource_api import idle_observation as policy


@pytest.fixture
def inventory(tmp_path):
    manager = BotManager()
    targets = []
    started = int(time.time())
    for bot_id, bot_type, cluster in ((1, "QUEUE", ["demo", "other"]), (2, "QUEUE_DEVICE", {"demo": ["gpu", "gpu"]})):
        uid = str(uuid4())
        manager.start_bot(
            bot_id,
            {
                "BOT_ID": bot_id,
                "BOT_TYPE": bot_type,
                "DATA_DIR": str(tmp_path),
                "CLUSTER_CONFIGS": cluster,
                "_LIFECYCLE_BOT_UUID": uid,
            },
        )
        bot = manager.get_instance(bot_id).bot
        target = "demo" if bot_id == 1 else "demo dev0"
        bot.lock("alice", f"lock {target} 1h")
        state = bot.published_snapshot().state["demo"]
        unit = state if bot_id == 1 else state[0]
        started = max(started, unit["current_users"][0]["start_time"])
        targets.append(
            {
                "bot_id": bot_id,
                "bot_uuid": uid,
                "node": "demo",
                "source_node": "host-a",
                "cards": {} if bot_id == 1 else {0: "dev-a", 1: "dev-b"},
            }
        )
    try:
        yield manager, targets, started
    finally:
        manager.shutdown_all()


def source(targets, endpoint="https://metrics.example.test"):
    return {"endpoint": endpoint, "nodes": {"host-a": ["dev-a", "dev-b"]}, "targets": targets}


def response(sample_time, *, success=1, cards=None):
    def row(metric, value, **labels):
        return {"metric": {"__name__": metric, "node": "host-a", **labels}, "value": [1, str(value)]}

    if cards is None:
        cards = {"dev-a": 0, "dev-b": 0}
    rows = [
        row(policy.COLLECTOR_OK, success, collector="accelerator"),
        row(policy.OBSERVED_AT, sample_time, collector="accelerator"),
    ]
    rows.extend(row(policy.UTILIZATION, value, device=device) for device, value in cards.items())
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": rows}})


def test_shared_source_uses_fresh_producer_time_and_resets_unknown_evidence(inventory):
    manager, targets, started = inventory
    now, collected = started, started
    config = policy.ObservationConfig.model_validate({"sources": [source(targets)]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    sample_cards = None
    request_count = 0

    def handle(request):
        nonlocal request_count
        assert request.url.path == "/api/v1/query"
        assert 'node=~"host-a"' in request.url.params["query"]
        request_count += 1
        return response(collected, cards=sample_cards)

    async def exercise():
        nonlocal now, collected, sample_cards
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for offset in range(0, 1200, 180):
                now = collected = started + offset
                await observer.poll_source(0, client)
                assert not observer.candidates
            now = started + 1200
            await observer.poll_source(0, client)
            assert not observer.candidates
            assert request_count == 8  # one batch per round, shared by both bots

            now = collected = started + 1260
            await observer.poll_source(0, client)
            assert set(observer.candidates) == {(1, "demo", None), (2, "demo", 0)}

            now = collected = started + 1440
            sample_cards = {"dev-a": 0}
            await observer.poll_source(0, client)
            assert set(observer.candidates) == {(2, "demo", 0)}

            now = started + 1800  # successful query, but producer stopped at 1440
            await observer.poll_source(0, client)
            assert not observer.candidates

            now = collected = started + 1801
            sample_cards = None
            await observer.poll_source(0, client)
            assert not observer.candidates

    asyncio.run(exercise())


def test_source_timeout_does_not_hold_allocator_or_delay_healthy_source(inventory, monkeypatch):
    manager, targets, started = inventory
    now = started
    options = {"sources": [source(targets[:1]), source(targets[1:], "https://other.example.test")]}
    observer = policy.IdleObserver(policy.ObservationConfig.model_validate(options), manager, clock=lambda: now)
    monkeypatch.setattr(policy, "REQUEST_SECONDS", 0.5)
    fail = False

    async def exercise():
        nonlocal now, fail
        waiting = asyncio.Event()

        async def handle(request):
            if fail and request.url.host == "metrics.example.test":
                waiting.set()
                await asyncio.Event().wait()
            return response(now)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for offset in range(0, 1261, 180):
                now = started + offset
                await asyncio.gather(*(observer.poll_source(index, client) for index in range(2)))
            assert len(observer.candidates) == 2

            now += 180
            fail = True
            slow = asyncio.create_task(observer.poll_source(0, client))
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert manager.inflight(1) == 0
            await observer.poll_source(1, client)
            assert not slow.done()
            bot = manager.get_instance(1).bot
            bot.book("bob", "book demo 10m")
            bot.lock("alice", "lock other 10m")
            assert bot.published_snapshot().state["other"]["status"] == "exclusive"
            bot.unlock("alice", "free other")
            assert bot.published_snapshot().state["other"]["status"] == "idle"
            assert bot.published_snapshot().state["demo"]["booking_list"][0]["user_id"] == "bob"
            await slow
            assert set(observer.candidates) == {(2, "demo", 0)}
            assert bot.published_snapshot().state["demo"]["current_users"][0]["user_id"] == "alice"

    asyncio.run(exercise())


def test_observes_committed_snapshot_while_resource_lock_is_held(inventory):
    manager, targets, started = inventory
    config = policy.ObservationConfig.model_validate({"sources": [source(targets[:1])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: started)
    bot = manager.get_instance(1).bot

    async def observe_once():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(started))) as client:
            await observer.poll_source(0, client)

    with ThreadPoolExecutor(max_workers=1) as executor, bot._lock:
        executor.submit(asyncio.run, observe_once()).result(timeout=2)
    assert observer._windows[0][(1, "demo", None)].observed_at == started


def test_invalid_opt_in_config_leaves_observer_disabled(monkeypatch, tmp_path):
    config = tmp_path / "invalid.json"
    config.write_text("invalid")
    monkeypatch.setenv(policy.CONFIG_ENV, str(config))
    assert policy.configured_observer(BotManager()) is None


def test_application_lifespan_starts_and_cancels_the_configured_observer(monkeypatch):
    from fastapi.testclient import TestClient
    from lockbot.backend.app.main import create_app

    started, stopped = Event(), Event()

    class Observer:
        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    observer = Observer()
    monkeypatch.setattr(policy, "configured_observer", lambda manager: observer)
    app = create_app()
    with TestClient(app):
        assert started.wait(2)
        assert app.state.idle_observer is observer
    assert stopped.is_set()
