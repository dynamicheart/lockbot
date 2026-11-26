from unittest.mock import patch

import pytest
from fake_adapter import FakeAdapter
from lockbot.core.device_bot import DeviceBot
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot

BOT_TYPES = ((NodeBot, "NODE"), (DeviceBot, "DEVICE"), (QueueBot, "QUEUE"), (QueueDeviceBot, "QUEUE_DEVICE"))


def make_bot(tmp_path, bot_class, bot_type, *, group_api=None, early=False):
    devices = bot_type in {"DEVICE", "QUEUE_DEVICE"}
    return bot_class(
        config_dict={
            "BOT_TYPE": bot_type,
            "BOT_ID": "reminders",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"first": ["GPU"], "second": ["GPU"]} if devices else ["first", "second"],
            **({"GROUP_REMIND_API_RESOURCES": group_api} if group_api is not None else {}),
            "EARLY_NOTIFY": early,
            "TIME_ALERT": 30,
        },
        adapter=FakeAdapter(),
    )


def run(bot, user, command, *, source="chat"):
    return execute_text_command(user, command, bot, source=source)


@pytest.mark.parametrize("bot_class,bot_type", BOT_TYPES)
@pytest.mark.parametrize("group_api", [None, False, True])
@pytest.mark.parametrize("early", [False, True])
def test_mixed_holdings_remind_only_the_selected_resources(tmp_path, bot_class, bot_type, group_api, early):
    bot = make_bot(tmp_path, bot_class, bot_type, group_api=group_api, early=early)
    card = " dev0" if bot_type in {"DEVICE", "QUEUE_DEVICE"} else ""
    with patch("time.time", return_value=100000) as clock:
        run(bot, "same-user", f"lock first{card} 1m")
        run(bot, "same-user", f"lock second{card} 1m", source="api")
        clock.return_value += 35 if early else 61
        bot._check_and_notify()
        (notice,) = bot.adapter.sent
        text = bot.adapter.extract_reply_text(notice)
        assert "first" in text
        assert ("second" in text) is bool(group_api)
        assert notice["envelope"]["notify"] == ["same-user"]
        clock.return_value += 61
        bot._check_and_notify()
        for state in bot.state.bot_state.values():
            for entry in state if isinstance(state, list) else [state]:
                assert entry["status"] == "idle"
        assert len(bot.adapter.sent) == 1


@pytest.mark.parametrize("bot_class,bot_type", BOT_TYPES[2:])
@pytest.mark.parametrize("group_api", [False, True])
def test_booking_readiness_and_timeout_follow_the_booked_users_channels(tmp_path, bot_class, bot_type, group_api):
    bot = make_bot(tmp_path, bot_class, bot_type, group_api=group_api)
    card = " dev0" if bot_type == "QUEUE_DEVICE" else ""
    with patch("time.time", return_value=100000) as clock:
        run(bot, "holder", f"lock first{card} 1h")
        run(bot, "agent-waiter", f"book first{card} 15m", source="api")
        run(bot, "chat-waiter", f"book first{card} 15m")
        run(bot, "holder", f"free first{card}", source="api")
        bot._check_and_notify()
        state = bot.state.bot_state["first"]
        entry = state[0] if isinstance(state, list) else state
        assert entry["booking_list"][0]["is_notified"] is True
        assert len(bot.adapter.sent) == int(group_api)
        if group_api:
            assert bot.adapter.sent[0]["envelope"]["notify"] == ["agent-waiter"]

        bot.adapter.sent.clear()
        clock.return_value += 301
        bot._check_and_notify()
        assert [booking["user_id"] for booking in entry["booking_list"]] == ["chat-waiter"]
        assert entry["booking_list"][0]["is_notified"] is True
        (notice,) = bot.adapter.sent
        recipients = set(notice["envelope"]["notify"])
        assert recipients == ({"chat-waiter", "agent-waiter"} if group_api else {"chat-waiter"})
        assert ("agent-waiter" in bot.adapter.extract_reply_text(notice)) is group_api


@pytest.mark.parametrize("group_api", [False, True])
def test_delay_reply_targets_bookings_by_source(tmp_path, group_api):
    bot = make_bot(tmp_path, QueueBot, "QUEUE", group_api=group_api)
    run(bot, "holder", "book first 15m")
    run(bot, "agent-waiter", "book first 15m", source="api")
    run(bot, "chat-waiter", "book first 15m")

    result = run(bot, "holder", "lock first 1h")

    recipients = set(result["envelope"]["notify"])
    assert recipients == ({"holder", "chat-waiter", "agent-waiter"} if group_api else {"holder", "chat-waiter"})


@pytest.mark.parametrize("bot_class,bot_type", BOT_TYPES[2:])
def test_auto_lock_promotes_in_fifo_order_and_preserves_booking_origin(tmp_path, bot_class, bot_type):
    bot = make_bot(tmp_path, bot_class, bot_type)
    bot.config.set_val("QUEUE_AUTO_LOCK", True)
    card = " dev0" if bot_type == "QUEUE_DEVICE" else ""
    with patch("time.time", return_value=100000) as clock:
        run(bot, "holder", f"lock first{card} 1m")
        run(bot, "first-waiter", f"book first{card} 15m", source="api")
        run(bot, "second-waiter", f"book first{card} 10m")
        assert bot._check_and_notify() == 60
        clock.return_value += 60
        assert bot._check_and_notify() == 900
        entry = bot._resource_state()["first:dev0" if card else "first"]
        (holder,) = entry["current_users"]
        (waiter,) = entry["booking_list"]
        assert entry["status"] == "exclusive"
        assert holder["user_id"] == "first-waiter"
        assert holder["source"] == "api"
        assert holder["start_time"] == clock.return_value
        assert holder["duration"] == 900
        assert not holder["is_notified"]
        assert waiter["user_id"] == "second-waiter" and not waiter["is_notified"]
        restored = bot_class(config=bot.config, adapter=FakeAdapter())
        assert restored.state.bot_state == bot.state.bot_state

        run(bot, "first-waiter", f"free first{card}", source="api")
        assert bot._check_and_notify() == 600
        assert entry["current_users"][0]["user_id"] == "second-waiter"
        assert not entry["booking_list"]


def test_enabling_auto_lock_skips_an_expired_claim_and_obeys_duration_cap(tmp_path):
    bot = make_bot(tmp_path, QueueBot, "QUEUE")
    with patch("time.time", return_value=100000) as clock:
        run(bot, "missed", "book first 15m")
        run(bot, "next", "book first 20m")
        assert bot._check_and_notify() == 300
        clock.return_value += 300
        bot.config.set_val("QUEUE_AUTO_LOCK", True)
        bot.config.set_val("MAX_LOCK_DURATION", 600)
        assert bot._check_and_notify() == 600
        entry = bot.state.bot_state["first"]
        assert not entry["booking_list"]
        (holder,) = entry["current_users"]
        assert holder["user_id"] == "next"
        assert holder["duration"] == 600


@pytest.mark.parametrize("bot_class,bot_type", BOT_TYPES[2:])
def test_auto_lock_group_notice_targets_only_group_bookings(tmp_path, bot_class, bot_type, monkeypatch):
    bot = make_bot(tmp_path, bot_class, bot_type, group_api=True)
    bot.config.set_val("QUEUE_AUTO_LOCK", True)
    card = " dev0" if bot_type == "QUEUE_DEVICE" else ""
    send = bot.adapter.send

    def assert_committed(reply):
        assert not bot._lock.locked()
        restored = bot_class(config=bot.config, adapter=FakeAdapter())
        assert all(entry["status"] == "exclusive" for entry in restored._resource_state().values())
        send(reply)

    monkeypatch.setattr(bot.adapter, "send", assert_committed)
    with patch("time.time", return_value=100000):
        run(bot, "agent-waiter", f"book first{card} 20m", source="api")
        run(bot, "chat-waiter", f"book second{card} 15m")
        bot._check_and_notify()

    (notice,) = bot.adapter.sent
    assert notice["envelope"]["notify"] == ["chat-waiter"]
    text = bot.adapter.extract_reply_text(notice)
    assert text == f"📢【排队资源已自动锁定】\n\nsecond{card}  chat-waiter  15 分钟\n\n"


def test_delivery_failure_occurs_after_resource_release(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, NodeBot, "NODE")
    calls = []

    def reject_send(_reply):
        assert not bot._lock.locked()
        assert bot.state.bot_state["first"]["status"] == "idle"
        calls.append("send")
        raise OSError("fixture delivery failure")

    monkeypatch.setattr(bot.adapter, "send", reject_send)
    with patch("time.time", return_value=100000) as clock:
        run(bot, "holder", "lock first 1m")
        clock.return_value += 61
        bot._check_and_notify()
        bot._check_and_notify()
    assert calls == ["send"]
