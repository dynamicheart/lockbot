import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.resource_api import idle_observation as policy
from lockbot.core.handler import execute_text_command


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

            for offset in range(1981, 3062, 180):
                now = collected = started + offset
                await observer.poll_source(0, client)
            assert set(observer.candidates) == {(1, "demo", None), (2, "demo", 0)}

            sample_cards = {"dev-a": 0, "dev-b": 0, "unmapped": 30}
            await observer.poll_source(0, client)
            assert set(observer.candidates) == {(2, "demo", 0)}

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


@pytest.mark.parametrize("confirm", [False, True])
def test_recovery_waits_for_accepted_reminder_and_post_deadline_samples(inventory, confirm):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.unlock("alice", "free demo")
    execute_text_command("alice", "lock demo 1h", bot, source="chat")
    bot.book("bob", "book demo 10m")
    start = bot.published_snapshot().state["demo"]["current_users"][0]["start_time"]
    now = start
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[0], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    sent = []

    def notify(notice, kind):
        assert not bot._lock.locked()
        sent.append((notice, kind))
        return True

    observer.chat_notification = notify

    async def exercise():
        nonlocal now
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(now))) as client:
            for offset in range(0, 1261, 180):
                now = start + offset
                await observer.poll_source(0, client)
            ((notice, event),) = sent
            assert event == "warning"
            (pending,) = observer.recovery.for_holder("alice", {1}, channel="chat")
            assert pending.confirm_by == start + 1860
            assert observer.recovery.confirm(bot, 1, "bob", notice.ref, channel="chat") is None
            assert observer.recovery.confirm(bot, 1, "alice", notice.ref, channel="api") is None

            last = 2520 if confirm else 1980
            for offset in range(1440, last + 1, 180):
                if confirm and offset == 1980:
                    now = start + 1859
                    confirmed = observer.recovery.confirm(bot, 1, "alice", notice.ref, channel="chat")
                    assert confirmed.review_at == start + 2460
                    assert observer.recovery.confirm(bot, 1, "alice", notice.ref, channel="chat") == confirmed
                now = start + offset
                await observer.poll_source(0, client)
                expected = "idle" if offset == last else "exclusive"
                assert bot.published_snapshot().state["demo"]["status"] == expected
            final = bot.published_snapshot().state["demo"]
            assert final["booking_list"][0]["user_id"] == "bob"
            assert [kind for _, kind in sent] == ["warning", "released"]

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["stale", "missing_card", "source_error", "notice_failed", "renewed"])
def test_recovery_retains_holding_when_evidence_or_delivery_is_missing(inventory, failure):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.unlock("alice", "free demo")
    execute_text_command("alice", "lock demo 1h", bot, source="chat")
    start = bot.published_snapshot().state["demo"]["current_users"][0]["start_time"]
    now = start
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[0], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    observer.chat_notification = lambda notice, kind: failure != "notice_failed"

    def transport(request):
        if now < start + 1980:
            return response(now)
        if failure == "stale":
            return response(now - 360)
        if failure == "missing_card":
            return response(now, cards={"dev-a": 0})
        if failure == "source_error":
            return httpx.Response(503)
        return response(now)

    async def exercise():
        nonlocal now
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for offset in range(0, 1981, 180):
                now = start + offset
                if offset == 1980 and failure == "renewed":
                    execute_text_command("alice", "lock demo 10m", bot, source="chat")
                await observer.poll_source(0, client)
            assert bot.published_snapshot().state["demo"]["current_users"][0]["user_id"] == "alice"

    asyncio.run(exercise())


@pytest.mark.parametrize("channel", ["chat", "api"])
def test_chat_reminder_and_keep_stay_with_the_chat_holder(inventory, monkeypatch, channel):
    manager, targets, _ = inventory
    bot = manager.get_instance(2).bot
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["123"])
    bot.unlock("alice", "free demo dev0")
    execute_text_command("alice", "lock demo dev0 1h", bot, source=channel)
    start = bot.published_snapshot().state["demo"][0]["current_users"][0]["start_time"]
    now = start
    sample_cards = None
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[1], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    bot.idle_confirmation_hook = observer.confirm_chat
    messages = []

    def send(message):
        assert not bot._lock.locked()
        messages.append(message["message"])
        return "accepted", "test"

    monkeypatch.setattr(bot.adapter, "send_notification", send)

    async def exercise():
        nonlocal now
        transport = httpx.MockTransport(lambda request: response(now, cards=sample_cards))
        async with httpx.AsyncClient(transport=transport) as client:
            for offset in range(0, 1261, 180):
                now = start + offset
                await observer.poll_source(0, client)

    asyncio.run(exercise())

    def keep(holder, group, card):
        reply = execute_text_command(holder, f"keep demo dev{card}", bot, group_id=group)
        return bot.adapter.extract_reply_text(reply)

    assert "无法保留资源" in keep("other", "123", 0)
    assert "无法保留资源" in keep("alice", "456", 0)
    assert "无法保留资源" in keep("alice", "123", 1)
    if channel == "api":
        assert messages == []
        assert "无法保留资源" in keep("alice", "123", 0)
        return
    (warning,) = messages
    assert warning["header"]["toid"] == [123]
    assert warning["body"] == [
        {
            "type": "TEXT",
            "content": "❗【资源利用率偏低】\n\ndemo dev0 已连续 20 分钟处于低利用率。\n"
            "请在 10 分钟内恢复使用。如还需要时间，发送 `keep demo dev0`，可多保留 10 分钟。\n\n",
        },
        {"type": "AT", "atuserids": ["alice"]},
    ]
    assert keep("alice", "123", 0).startswith("✅【已确认保留】\n\ndemo dev0 将于")
    (notice,) = observer.recovery.for_holder("alice", {2}, channel="chat")
    assert notice.review_at == start + 2460

    async def recheck():
        nonlocal now, sample_cards
        transport = httpx.MockTransport(lambda request: response(now, cards=sample_cards))
        async with httpx.AsyncClient(transport=transport) as client:
            for offset in range(1440, 2521, 180):
                now = start + offset
                if offset == 2520:
                    sample_cards = {"dev-a": 0}
                await observer.poll_source(0, client)

    asyncio.run(recheck())
    assert bot.published_snapshot().state["demo"][0]["status"] == "exclusive"
    assert observer.recovery.for_holder("alice", {2}, channel="chat") == []
    assert len(messages) == 1


@pytest.mark.parametrize("channel", ["chat", "api"])
def test_recovery_broadcasts_only_after_commit(inventory, monkeypatch, channel):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["123"])
    bot.unlock("alice", "free demo")
    execute_text_command("alice", "lock demo 1h", bot, source=channel)
    start = bot.published_snapshot().state["demo"]["current_users"][0]["start_time"]
    now = start
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[0], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    messages = []

    def send(message):
        assert not bot._lock.locked()
        messages.append(message["message"]["body"])
        if bot.published_snapshot().state["demo"]["status"] == "idle":
            raise OSError("test delivery failed")
        return "accepted", "test"

    monkeypatch.setattr(bot.adapter, "send_notification", send)

    async def exercise():
        nonlocal now
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(now))) as client:
            for offset in range(0, 1981, 180):
                now = start + offset
                await observer.poll_source(0, client)
                if channel == "api" and offset == 1260:
                    assert messages == []
                    (notice,) = observer.recovery.for_holder("alice", {1}, channel="api")
                    observer.recovery.delivered(notice)
            assert bot.published_snapshot().state["demo"]["status"] == "idle"

    asyncio.run(exercise())
    assert len(messages) == (2 if channel == "chat" else 1)
    assert messages[-1] == [
        {
            "type": "TEXT",
            "content": "🤖【机器人自动回收低利用率资源】\n\n用户：alice\n资源：demo\n操作：已释放锁定\n\n",
        }
    ]


def test_gateway_observer_uses_scoped_post_and_fails_closed(inventory, monkeypatch):
    from urllib.parse import parse_qs

    from lockbot.metrics.gateway import validate_query

    manager, targets, started = inventory
    config = policy.ObservationConfig.model_validate(
        {"sources": [{**source(targets[:1]), "use_bot_metrics_grant": True}]}
    )
    observer = policy.IdleObserver(config, manager, clock=lambda: started)
    observed = []
    token = "test-only"
    monkeypatch.setattr(policy, "_bot_metrics_token", lambda source: token)

    def handler(request):
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-only"
        params = parse_qs(request.content.decode())
        names, nodes = validate_query(params["query"][0], {"host-a"})
        assert names == {policy.OBSERVED_AT, policy.COLLECTOR_OK, policy.UTILIZATION} and nodes == {"host-a"}
        observed.append(True)
        return response(started)

    async def exercise():
        nonlocal token
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await observer.poll_source(0, client)
            token = None
            await observer.poll_source(0, client)
        assert observed == [True] and not observer.candidates

    asyncio.run(exercise())
