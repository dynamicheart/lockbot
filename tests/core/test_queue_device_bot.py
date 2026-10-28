import copy

import pytest
from fake_adapter import FakeAdapter
from lockbot.core.queue_device_bot import QueueDeviceBot


@pytest.fixture
def bot(tmp_path):
    return QueueDeviceBot(
        config_dict={
            "BOT_TYPE": "QUEUE_DEVICE",
            "BOT_ID": "cards",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"node1": ["GPU", "GPU"], "node2": ["GPU"]},
            "DEFAULT_DURATION": 3600,
            "MAX_LOCK_DURATION": -1,
            "EARLY_NOTIFY": False,
        },
        adapter=FakeAdapter(),
    )


def card(bot, index=0, node="node1"):
    return bot.state.bot_state[node][index]


def users(entry, field="current_users"):
    return [u["user_id"] for u in entry[field]]


def test_independent_card_queues_and_explicit_claim(bot):
    bot.lock("alice", "lock node1 dev0 1h")
    bot.book("bob", "book node1 dev0 30m")
    bot.lock("carol", "lock node1 dev1 2h")
    bot.lock("bob", "lock node1 dev0")
    assert users(card(bot)) == ["alice"]
    assert users(card(bot, 1)) == ["carol"]
    bot.unlock("alice", "free node1 dev0")
    bot._check_and_notify()
    assert card(bot)["booking_list"][0]["is_notified"]
    bot.lock("bob", "lock node1 dev0")
    assert users(card(bot)) == ["bob"] and not card(bot)["booking_list"]
    assert card(bot)["current_users"][0]["duration"] == 1800
    assert users(card(bot, 1)) == ["carol"]


def test_multicard_acquisition_is_atomic_and_invalid_selectors_do_not_mutate(bot):
    bot.lock("alice", "lock node1 dev0 1h")
    before = copy.deepcopy(bot.state.bot_state)
    for command in ["lock node1 dev0,1 1h", "lock node1 dev0-3 1h", "lock node1 dev2 1h"]:
        bot.lock("bob", command)
        assert bot.state.bot_state == before


def test_exact_release_and_cancel_keep_sibling_agent_work(bot):
    bot.lock("alice", "lock node1 dev0,1 1h")
    bot.book("bob", "book node1 dev0,1 30m")
    bot.unlock("bob", "free node1 dev0")
    bot.unlock("alice", "free node1 dev0")
    assert not card(bot)["booking_list"] and not card(bot)["current_users"]
    assert users(card(bot, 1)) == ["alice"]
    assert users(card(bot, 1), "booking_list") == ["bob"]
    bot.unlock("alice", "free")
    assert not card(bot, 1)["current_users"]
    assert users(card(bot, 1), "booking_list") == ["bob"]


def test_expiry_and_missed_claim_only_advance_the_affected_card(bot, monkeypatch):
    clock = [10000]
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: clock[0])
    bot.lock("alice", "lock node1 dev0 1m")
    bot.lock("other", "lock node1 dev1 2h")
    bot.book("bob", "book node1 dev0 30m")
    bot.book("carol", "book node1 dev0 20m")
    clock[0] += 61
    bot._check_and_notify()
    assert not card(bot)["current_users"]
    assert card(bot)["booking_list"][0]["is_notified"]
    clock[0] += 301
    bot._check_and_notify()
    assert users(card(bot), "booking_list") == ["carol"]
    assert card(bot)["booking_list"][0]["is_notified"]
    assert users(card(bot, 1)) == ["other"]
    assert bot.adapter.sent


def test_take_kicklock_and_kickout_have_per_card_semantics(bot):
    bot.lock("alice", "lock node1 dev0,1 1h")
    bot.book("bob", "book node1 dev0 30m")
    bot.take("carol", "take node1 dev0 20m")
    assert users(card(bot)) == ["carol"]
    assert users(card(bot), "booking_list") == ["alice", "bob"]
    bot.kicklock("op", "kicklock node1 dev0")
    assert not card(bot)["current_users"]
    assert users(card(bot), "booking_list") == ["alice", "bob"]
    assert users(card(bot, 1)) == ["alice"]
    bot.kickout("op", "kickout node1 dev0")
    assert not card(bot)["booking_list"]
    assert users(card(bot, 1)) == ["alice"]


def test_operator_kick_preserves_waiters_and_other_cards(bot):
    bot.lock("alice", "lock node1 dev0,1 1h")
    bot.book("bob", "book node1 dev0 30m")
    result = bot.do_opkick("alice", node_key="node1", dev_ids=[0], reason="maintenance")
    assert result["freed"] == ["node1/dev0"]
    assert not card(bot)["current_users"] and users(card(bot), "booking_list") == ["bob"]
    assert users(card(bot, 1)) == ["alice"]


def test_reload_retains_card_ownership_and_queue_order(bot):
    bot.lock("alice", "lock node1 dev0 1h")
    bot.book("bob", "book node1 dev0 30m")
    bot.book("carol", "book node1 dev1 20m")
    reloaded = QueueDeviceBot(config=bot.config, adapter=FakeAdapter())
    assert users(card(reloaded)) == ["alice"]
    assert users(card(reloaded), "booking_list") == ["bob"]
    assert users(card(reloaded, 1), "booking_list") == ["carol"]
    help_text = reloaded.adapter.extract_reply_text(reloaded.print_help("alice"))
    assert "book node1 dev0" in help_text and "lock node1 dev0" in help_text


def test_sharing_is_not_supported(bot):
    bot.slock("alice", "slock node1 dev0 1h")
    assert not card(bot)["current_users"]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("display_model", [False, True])
def test_query_groups_machine_once_and_keeps_each_cards_queue(bot, language, display_model):
    bot.config.set_val("LANGUAGE", language)
    bot.config.set_val("DISPLAY_DEVICE_MODEL", display_model)
    bot.lock("alice", "lock node1 dev0 1h")
    bot.book("bob", "book node1 dev0 30m")
    bot.book("carol", "book node1 dev1 20m")
    before = copy.deepcopy(bot.state.bot_state)
    usage = bot._current_usage("node1")
    assert usage.count("node1") == 1 and "node2" not in usage
    first, second = usage.split("  dev1", 1)
    assert "  dev0" in first and "alice" in first and "bob" in first and "carol" not in first
    assert "carol" in second and "bob" not in second
    model = bot.config.get_val("CLUSTER_CONFIGS")["node1"][0]
    first_card_line = next(line for line in usage.splitlines() if line.startswith("  dev0"))
    assert (model in first_card_line) is display_model
    assert bot.state.bot_state == before


def test_identical_cards_merge_without_mutating_state_and_success_replies(bot, monkeypatch):
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    bot.config.set_val("DISPLAY_DEVICE_MODEL", True)
    reply = bot.lock("alice", "lock node1 dev0-1 1h")
    assert "dev0-1 GPU alice" in bot.adapter.extract_reply_text(reply)
    bot.book("bob", "book node1 dev0-1 30m")
    assert card(bot)["current_users"][0]["holding_id"] != card(bot, 1)["current_users"][0]["holding_id"]
    assert card(bot)["booking_list"][0]["holding_id"] != card(bot, 1)["booking_list"][0]["holding_id"]
    before = copy.deepcopy(bot.state.bot_state)
    text = bot._current_usage("node1")
    assert "dev0-1 GPU alice" in text and text.count("bob") == 1
    assert "node2" not in text
    assert bot.state.bot_state == before


@pytest.mark.parametrize(
    "difference",
    ["lease_start", "duration", "queue_order", "queue_clock", "queue_notified", "model", "source", "purpose"],
)
def test_different_card_state_never_merges_even_when_display_time_matches(bot, monkeypatch, difference):
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    bot.lock("alice", "lock node1 dev0-1 2h")
    bot.book("bob", "book node1 dev0-1 30m")
    bot.book("carol", "book node1 dev0-1 30m")
    second = card(bot, 1)
    if difference == "lease_start":
        second["current_users"][0]["start_time"] -= 1
    elif difference == "duration":
        second["current_users"][0]["duration"] += 1
    elif difference == "queue_order":
        second["booking_list"].reverse()
    elif difference == "queue_clock":
        second["booking_list"][0]["start_time"] += 1
    elif difference == "queue_notified":
        second["booking_list"][0]["is_notified"] = True
    elif difference == "model":
        second["dev_model"] = "different-model"
    elif difference == "source":
        second["current_users"][0]["source"] = "chat"
    else:
        second["current_users"][0]["usage"] = {"purpose": "different task", "agent_label": "other-agent"}
    text = bot._current_usage("node1")
    assert "dev0-1" not in text and "  dev0 " in text and "  dev1 " in text


def test_pending_notifications_merge_and_claim_stays_per_card(bot, monkeypatch):
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    bot.config.set_val("DISPLAY_DEVICE_MODEL", True)
    bot.book("bob", "book node1 dev0-1 30m")
    bot._check_and_notify()
    text = bot.adapter.extract_reply_text(bot.adapter.sent[-1])
    assert "node1 dev0-1 GPU bob" in text
    assert text.count("bob") >= 1
    assert all(entry["booking_list"][0]["is_notified"] for entry in bot.state.bot_state["node1"])
    bot.lock("bob", "lock node1 dev0")
    assert users(card(bot)) == ["bob"] and not card(bot)["booking_list"]
    assert not card(bot, 1)["current_users"] and users(card(bot, 1), "booking_list") == ["bob"]


def test_different_pending_queue_tail_stays_separate(bot, monkeypatch):
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    bot.book("bob", "book node1 dev0-1 30m")
    bot.book("carol", "book node1 dev1 30m")
    bot._check_and_notify()
    text = bot.adapter.extract_reply_text(bot.adapter.sent[-1])
    assert "dev0-1" not in text and "dev0" in text and "dev1" in text


def test_nonconsecutive_actual_card_ids_never_merge(bot):
    card(bot, 1)["dev_id"] = 2
    text = bot._current_usage("node1")
    assert "  dev0 " in text and "  dev2 " in text and "dev0-2" not in text


def test_api_multicard_ranges_compare_source_without_losing_lease_ids(bot, monkeypatch):
    from lockbot.core.handler import execute_text_command

    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    reply = execute_text_command("alice", "lock node1 dev0-1 1h", bot, source="api")
    assert "dev0-1 alice" in bot.adapter.extract_reply_text(reply)
    execute_text_command("bob", "book node1 dev0-1 30m", bot, source="api")
    assert "dev0-1 alice" in bot._current_usage("node1")
    card(bot, 1)["booking_list"][0]["source"] = "chat"
    assert "dev0-1" not in bot._current_usage("node1")


def test_notification_only_groups_cards_notified_in_this_sweep(bot, monkeypatch):
    monkeypatch.setattr("lockbot.core.queue_bot.time.time", lambda: 10000)
    bot.book("bob", "book node1 dev0-1 30m")
    card(bot, 1)["booking_list"][0].update(is_notified=True, start_time=10000, booked_start_time=10000)
    bot._check_and_notify()
    text = bot.adapter.extract_reply_text(bot.adapter.sent[-1])
    assert "node1 dev0 " in text and "dev0-1" not in text and "dev1" not in text
