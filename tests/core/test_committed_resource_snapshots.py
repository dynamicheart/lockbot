from concurrent.futures import ThreadPoolExecutor

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot


@pytest.fixture(params=["infoflow", "feishu"])
def platform(request):
    return request.param


@pytest.fixture(params=[NodeBot, DeviceBot, QueueBot, QueueDeviceBot])
def resource_bot(request, platform, tmp_path, monkeypatch):
    bot_class = request.param
    card_bot = bot_class in (DeviceBot, QueueDeviceBot)
    target = "node1 dev0" if card_bot else "node1"
    monkeypatch.setattr("time.time", lambda: 10000)
    bot = bot_class(
        config_dict={
            "BOT_ID": "snapshots",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"node1": ["GPU", "GPU"]} if card_bot else ["node1"],
            "PLATFORM": platform,
            "_BOT_VIEW_LINK": lambda: {"view_url": "https://lockbot.example.test/resources"},
        },
    )
    return bot, target


def test_query_uses_committed_state_without_waiting_for_writer(resource_bot):
    bot, target = resource_bot
    bot.lock("alice", f"lock {target} 1h")
    if isinstance(bot, QueueBot):
        bot.book("bob", f"book {target} 30m")
    expected = bot._query_reply(bot._msg_with_usage("query.cluster_usage_title", node_key="node1"), "reader")
    snapshot = bot.published_snapshot()

    with ThreadPoolExecutor(max_workers=1) as pool, bot._lock:
        live_node = bot.state.bot_state["node1"]
        live_entry = live_node[0] if isinstance(live_node, list) else live_node
        live_entry["current_users"][0]["user_id"] = "uncommitted"
        reply = pool.submit(bot.query, "reader", "node1").result(timeout=2)
        live_entry["current_users"][0]["user_id"] = "alice"

    reply.pop("uuid", None)  # Feishu assigns a new transport ID to each reply.
    expected.pop("uuid", None)
    assert reply == expected
    assert bot.published_snapshot() is snapshot
    published_node = snapshot.state["node1"]
    published_entry = published_node[0] if isinstance(published_node, tuple) else published_node
    with pytest.raises(TypeError):
        published_entry["current_users"][0]["user_id"] = "modified"

    bot.unlock("alice", f"free {target}")
    assert bot.published_snapshot().revision > snapshot.revision
    assert published_entry["current_users"][0]["user_id"] == "alice"
    after = bot.query("reader", "node1")
    after.pop("uuid", None)
    assert after != expected


def test_failed_persistence_keeps_previous_snapshot(resource_bot, monkeypatch):
    bot, target = resource_bot
    original = bot.published_snapshot()

    def refuse_write(*args, **kwargs):
        raise OSError("test persistence failure")

    monkeypatch.setattr("lockbot.core.io._save_bot_state_atomically", refuse_write)
    with pytest.raises(OSError, match="test persistence failure"):
        bot.lock("alice", f"lock {target} 1h")
    assert bot.published_snapshot() is original


def test_timer_publishes_expiry_and_queue_progress(resource_bot, monkeypatch):
    bot, target = resource_bot
    monkeypatch.setattr(bot.adapter, "send", lambda reply: None)
    bot.lock("alice", f"lock {target} 1m")
    if isinstance(bot, QueueBot):
        bot.book("bob", f"book {target} 30m")
    original = bot.published_snapshot()

    monkeypatch.setattr("time.time", lambda: 10061)
    bot._check_and_notify()

    snapshot = bot.published_snapshot()
    assert snapshot.revision > original.revision
    node = snapshot.state["node1"]
    entry = node[0] if isinstance(node, tuple) else node
    assert entry["status"] == "idle"
    if isinstance(bot, QueueBot):
        assert entry["booking_list"][0]["is_notified"]


def test_publication_belongs_to_the_instance_that_persisted(resource_bot):
    bot, target = resource_bot
    restored = type(bot)(config=bot.config)
    original = restored.published_snapshot()

    bot.lock("alice", f"lock {target} 1h")

    assert bot.published_snapshot().revision == 1
    assert restored.published_snapshot() is original
