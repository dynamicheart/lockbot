import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

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
        spare = "other" if bot_id == 1 else "demo dev1"
        bot.book("alice", f"book {spare} 1h")
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


@pytest.mark.parametrize("failure", ["stale", "missing_card", "source_error", "notice_failed", "disabled"])
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
                if offset == 1980 and failure == "disabled":
                    (notice,) = observer.recovery.for_holder("alice", {1}, channel="chat")
                    bot.config.set_val("IDLE_UTILIZATION_CHECK_ENABLED", False)
                    assert observer.recovery.confirm(bot, 1, "alice", notice.ref, channel="chat") is None
                    prior = observer.candidates[(1, "demo", None)]
                    assert not observer.recovery.reclaim_ready(
                        {(1, "demo", None): policy.replace(prior, observed_at=now)}, 240
                    )
                await observer.poll_source(0, client)
            assert bot.published_snapshot().state["demo"]["current_users"][0]["user_id"] == "alice"

    asyncio.run(exercise())


@pytest.mark.parametrize("channel,group_enabled", [("chat", False), ("api", False), ("api", True)])
def test_chat_reminder_and_keep_stay_with_the_holder(inventory, monkeypatch, channel, group_enabled):
    manager, targets, _ = inventory
    bot = manager.get_instance(2).bot
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["123"])
    bot.config.set_val("GROUP_REMIND_AGENT_IDLE", group_enabled)
    bot.unlock("alice", "free demo dev0")
    execute_text_command("alice", "lock demo dev0 1h", bot, source=channel)
    waiting = channel == "chat"
    if waiting:
        bot.book("bob", "book demo dev0 1h")
    start = bot.published_snapshot().state["demo"][0]["current_users"][0]["start_time"]
    now = start
    sample_cards = {"dev-a": 1.2, "dev-b": 82}
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
    if channel == "api" and not group_enabled:
        assert messages == []
        assert "无法保留资源" in keep("alice", "123", 0)
        return
    (warning,) = messages
    assert warning["header"]["toid"] == [123]
    reason = "当前资源有人排队" if waiting else "本机器人无空闲资源"
    expected = (
        "❗【资源利用率偏低】\n\ndemo dev0 的利用率已连续 20 分钟低于 5%（当前 1.2%）。\n"
        "请在 10 分钟内恢复使用。如需多保留 10 分钟，请在本群 @ 机器人并发送 `keep demo dev0`。\n\n"
        f"触发原因：{reason}。"
    )
    assert warning["body"] == [
        {"type": "TEXT", "content": expected + "\n\n"},
        {"type": "AT", "atuserids": ["alice"]},
    ]
    assert keep("alice", "123", 0).startswith("✅【已确认保留】\n\ndemo dev0 将于")
    (notice,) = observer.recovery.for_holder("alice", {2}, channel="chat")
    assert notice.review_at == start + 2460
    if channel == "api":
        api_notice = observer.recovery.delivered(notice)
        assert api_notice.confirm_by == start + 1860
        assert observer.recovery.confirm(bot, 2, "alice", notice.ref, channel="api", resource=notice.resource) == notice

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
    if channel == "api":
        assert messages == []
        return
    assert len(messages) == 2
    assert "demo 的利用率已连续 20 分钟低于 5%（当前 0%）" in messages[0][0]["content"]
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


def test_gateway_issuer_checks_live_identity_endpoint_and_mappings(inventory, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    _, targets, _ = inventory
    config = policy.Source.model_validate({**source(targets[:1]), "use_bot_metrics_grant": True})
    bot = SimpleNamespace(uuid=str(config.targets[0].bot_uuid), is_deleted=False)
    source_config = {"endpoint": "https://metrics.example.test", "nodeMapping": {"demo": "host-a"}, "deviceMapping": {}}
    monkeypatch.setattr(policy, "SessionLocal", lambda: nullcontext(SimpleNamespace(get=lambda *_: bot)))
    monkeypatch.setattr(policy, "agent_metrics_source", lambda _: source_config)
    monkeypatch.setattr(policy, "issue_metrics_access", lambda *_: {"token": "test-only"})
    assert policy._bot_metrics_token(config) == "test-only"
    source_config["nodeMapping"]["other"] = "host-a"
    assert policy._bot_metrics_token(config) is None
    del source_config["nodeMapping"]["other"]
    source_config["endpoint"] = "https://another.example.test"
    assert policy._bot_metrics_token(config) is None
    source_config["endpoint"] = "https://metrics.example.test"
    bot.uuid = str(uuid4())
    assert policy._bot_metrics_token(config) is None


def test_chat_keep_confirms_each_listed_resource(inventory):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["123"])
    bot.unlock("alice", "free demo")
    execute_text_command("alice", "lock demo,other 1h", bot, source="chat")
    state = bot.published_snapshot().state
    start = state["demo"]["current_users"][0]["start_time"]
    now = start + policy.IDLE_SECONDS
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[0], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    bot.idle_confirmation_hook = observer.confirm_chat
    evidence = {}
    for node in ("demo", "other"):
        version = bot.op_holding_version(state, node, None, "alice", at=now)
        evidence[1, node, None] = policy.IdleEvidence(version, start, now, "alice", "chat", 1, 0, 5)
    for notice in observer.recovery.reconcile(0, evidence):
        observer.recovery.delivered(notice, group=True)
    reply = execute_text_command("alice", "keep demo, other，demo,unknown", bot, group_id="123")
    text = bot.adapter.extract_reply_text(reply)
    assert text.count("已确认保留") == 2
    assert "unknown 当前没有你的待确认提醒" in text
    assert all(notice.confirmed for notice in observer.recovery.for_holder("alice", {1}, channel="chat"))


@pytest.mark.parametrize("action", ["relock", "renew", "new_holder", "busy"])
def test_low_utilization_window_follows_the_same_holder(inventory, monkeypatch, action):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.unlock("alice", "free demo")
    execute_text_command("alice", "lock demo 1h", bot, source="chat")
    start = bot.published_snapshot().state["demo"]["current_users"][0]["start_time"]
    now = start
    collected = start
    cards = {"dev-a": 0, "dev-b": 0}
    monkeypatch.setattr("time.time", lambda: now)
    config = policy.ObservationConfig.model_validate({"sources": [source([{**targets[0], "recover_idle": True}])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    sent = []
    observer.chat_notification = lambda notice, kind: sent.append(kind) is None

    async def exercise():
        nonlocal now, collected
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response(collected, cards=cards))
        ) as client:
            for offset in range(0, 1261, 180):
                now = collected = start + offset
                await observer.poll_source(0, client)
            (first,) = observer.recovery.for_holder("alice", {1}, channel="chat")
            assert first.review_at == start + 1860

            now = collected = start + 1380
            who = "bob" if action == "new_holder" else "alice"
            if action != "renew":
                bot.unlock("alice", "free demo")
                await observer.poll_source(0, client)
                assert not observer.recovery.for_holder("alice", {1}, channel="chat")
                now += 1
            execute_text_command(who, "lock demo 1h", bot, source="chat")
            await observer.poll_source(0, client)  # The latest sample predates a fresh acquisition.
            if action == "busy":
                cards["dev-a"] = 10
            for offset in (1500, 1680, 1860):
                now = collected = start + offset
                await observer.poll_source(0, client)
                if offset == 1500 and action in {"relock", "renew"}:
                    (current,) = observer.recovery.for_holder("alice", {1}, channel="chat")
                    assert (current.ref, current.review_at) == (first.ref, first.review_at)
                    assert current.version != first.version
            if action in {"relock", "renew"}:
                assert bot.published_snapshot().state["demo"]["status"] == "idle"
                assert sent == ["warning", "released"]
            else:
                assert bot.published_snapshot().state["demo"]["current_users"][0]["user_id"] == who
                assert sent == ["warning"]

    asyncio.run(exercise())


@pytest.mark.parametrize("bot_id,target,spare,card", [(1, "demo", "other", None), (2, "demo dev0", "demo dev1", 0)])
def test_idle_recovery_requires_bot_capacity_pressure_or_a_waiter(inventory, monkeypatch, bot_id, target, spare, card):
    manager, targets, _ = inventory
    bot = manager.get_instance(bot_id).bot
    bot.unlock("alice", f"free {spare}")
    bot.unlock("alice", f"free {target}")
    now = start = int(time.time())
    monkeypatch.setattr("time.time", lambda: now)
    execute_text_command("alice", f"lock {target} 1h", bot, source="chat")
    monitored = {**targets[bot_id - 1], "recover_idle": True}
    config = policy.ObservationConfig.model_validate({"sources": [source([monitored])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    sent = []
    observer.chat_notification = lambda notice, kind: sent.append(kind) is None
    resource = (bot_id, "demo", card)

    async def exercise():
        nonlocal now
        transport = httpx.MockTransport(lambda _: response(now))
        async with httpx.AsyncClient(transport=transport) as client:
            for offset in range(0, 1261, 180):
                now = start + offset
                await observer.poll_source(0, client)
            assert resource not in observer.candidates
            assert sent == []

            bot.book("bob", f"book {target} 10m")
            await observer.poll_source(0, client)
            (first,) = observer.recovery.for_holder("alice", {bot_id}, channel="chat")
            assert first.since == start
            assert sent == ["warning"]

            bot.unlock("bob", f"free {target}")
            assert not observer.recovery.for_holder("alice", {bot_id}, channel="chat")
            for offset in (1440, 1620, 1800, 1860):
                now = start + offset
                await observer.poll_source(0, client)
            assert resource not in observer.candidates
            assert sent == ["warning"]

            other_bot = manager.get_instance(3 - bot_id).bot
            other_spare = "demo dev1" if bot_id == 1 else "other"
            other_bot.unlock("alice", f"free {other_spare}")
            bot.lock("charlie", f"lock {spare} 1h")
            await observer.poll_source(0, client)
            (second,) = observer.recovery.for_holder("alice", {bot_id}, channel="chat")
            assert second.ref != first.ref
            assert second.review_at == now + 600
            for offset in (180, 360, 540):
                now = second.delivered_at + offset
                await observer.poll_source(0, client)

            if card is None:
                reclaim_ready = observer.recovery.reclaim_ready

                def release_spare_before_reclaim(evidence, max_age):
                    assert evidence[resource].actionable
                    bot.unlock("charlie", f"free {spare}")
                    return reclaim_ready(evidence, max_age)

                monkeypatch.setattr(observer.recovery, "reclaim_ready", release_spare_before_reclaim)
            now = second.review_at
            await observer.poll_source(0, client)
            if card is None:
                assert sent == ["warning", "warning"]
                assert bot.published_snapshot().state["demo"]["status"] == "exclusive"
            else:
                assert sent == ["warning", "warning", "released"]

    asyncio.run(exercise())


@pytest.mark.parametrize("pause_enabled,poll_at_night", [(True, True), (True, False), (False, True)])
def test_overnight_pause_restarts_observation_at_morning_cutoff(inventory, monkeypatch, pause_enabled, poll_at_night):
    manager, targets, _ = inventory
    bot = manager.get_instance(1).bot
    bot.unlock("alice", "free demo")
    bot.config.set_val("OVERNIGHT_ENABLED", not pause_enabled)
    if pause_enabled:
        bot.config.set_val("OVERNIGHT_PAUSE_IDLE_CHECK", True)
    bot.config.set_val("OVERNIGHT_START", "23:59")
    bot.config.set_val("OVERNIGHT_END", "00:00")
    start = int(datetime(2026, 11, 25, 23, 28, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    night_start = start + 31 * 60
    morning = night_start + 60
    now = collected = start
    monkeypatch.setattr("time.time", lambda: now)
    execute_text_command("alice", "lock demo 1h", bot, source="chat")
    target = {**targets[0], "recover_idle": True}
    config = policy.ObservationConfig.model_validate({"sources": [source([target])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    sent = []
    observer.chat_notification = lambda notice, kind: sent.append(kind) is None

    async def exercise():
        nonlocal now, collected
        transport = httpx.MockTransport(lambda _: response(collected))
        async with httpx.AsyncClient(transport=transport) as client:
            for offset in range(0, 1801, 180):
                now = collected = start + offset
                await observer.poll_source(0, client)
            (first,) = observer.recovery.for_holder("alice", {1}, channel="chat")
            assert first.review_at == night_start

            now = collected = night_start
            if not pause_enabled:
                assert observer.recovery.for_holder("alice", {1}, channel="chat") == [first]
                await observer.poll_source(0, client)
                assert sent == ["warning", "released"]
                assert bot.published_snapshot().state["demo"]["status"] == "idle"
                return

            assert not observer.recovery.for_holder("alice", {1}, channel="chat")
            if poll_at_night:
                await observer.poll_source(0, client)
                assert not observer.candidates
                assert observer.sampling_status(1, target["bot_uuid"])["fresh_nodes"] == 1
                assert bot.published_snapshot().state["demo"]["status"] == "exclusive"

                now = morning  # The latest available sample was taken during the pause.
                await observer.poll_source(0, client)
                assert not observer.candidates

            for offset in (0, 180, 360, 540, 720, 900, 1080, 1200):
                now = collected = morning + offset
                await observer.poll_source(0, client)
                if offset < 1200:
                    assert not observer.candidates
                    assert sent == ["warning"]
            (second,) = observer.recovery.for_holder("alice", {1}, channel="chat")
            assert second.since == morning
            assert second.ref != first.ref
            assert sent == ["warning", "warning"]
            assert bot.published_snapshot().state["demo"]["status"] == "exclusive"

    asyncio.run(exercise())


def test_sampling_status_tracks_fresh_complete_data_without_a_lock(inventory):
    manager, targets, now = inventory
    bot = manager.get_instance(1).bot
    bot.unlock("alice", "free demo")
    bot.config.set_val("IDLE_UTILIZATION_CHECK_ENABLED", False)
    config = policy.ObservationConfig.model_validate({"sources": [source(targets[:1])]})
    observer = policy.IdleObserver(config, manager, clock=lambda: now)
    uuid = targets[0]["bot_uuid"]
    cards = {"dev-a": 12, "dev-b": 0}

    async def exercise():
        nonlocal now
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(now, cards=cards))) as client:
            assert observer.sampling_status(1, uuid) == {"configured_nodes": 1, "fresh_nodes": 0}
            await observer.poll_source(0, client)
            assert observer.sampling_status(1, uuid) == {"configured_nodes": 1, "fresh_nodes": 1}
            assert observer.sampling_status(1, str(uuid4())) == {"configured_nodes": 0, "fresh_nodes": 0}
            now += 241
            assert observer.sampling_status(1, uuid) == {"configured_nodes": 1, "fresh_nodes": 0}
            del cards["dev-b"]
            await observer.poll_source(0, client)
            assert observer.sampling_status(1, uuid) == {"configured_nodes": 1, "fresh_nodes": 0}

    asyncio.run(exercise())
