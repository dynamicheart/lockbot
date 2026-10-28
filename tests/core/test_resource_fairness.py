"""Bot policy applies identically to direct chat and API dispatch, before mutation."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot
from lockbot.core.resource_policy import public_policy


@pytest.fixture(
    params=[("NODE", NodeBot), ("QUEUE", QueueBot), ("DEVICE", DeviceBot), ("QUEUE_DEVICE", QueueDeviceBot)]
)
def policy_bot(request, tmp_path):
    kind, cls = request.param
    bot = cls(
        config_dict={
            "BOT_ID": "policy",
            "BOT_TYPE": kind,
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu", "gpu"], "b": ["gpu", "gpu"]} if "DEVICE" in kind else ["a", "b"],
            "WEBHOOK_URL": "",
            "FORBID_RELOCK": True,
            "MAX_RESOURCES_PER_USER": 1,
        }
    )
    return bot, " dev0" if "DEVICE" in kind else ""


def text(reply):
    return str(reply)


def test_renewal_and_limit_share_chat_api_identity(policy_bot):
    bot, dev = policy_bot
    execute_text_command("alice", f"lock a{dev} 10m", bot, source="chat")
    before = bot.state_snapshot()[1]
    assert "❌" in text(execute_text_command("alice", f"lock a{dev} 10m", bot, source="api"))
    assert "❌" in text(execute_text_command("alice", f"lock b{dev} 10m", bot, source="api"))
    assert before == bot.state_snapshot()[1]
    execute_text_command("alice", f"free a{dev}", bot)
    assert "❌" not in text(bot.lock("alice", f"lock b{dev} 10m"))


def test_multitarget_rejects_atomically(policy_bot):
    bot, dev = policy_bot
    before = bot.state_snapshot()[1]
    assert "❌" in text(bot.lock("alice", f"lock a,b{dev} 10m"))
    assert bot.state_snapshot()[1] == before


def test_card_quota_counts_sibling_cards(policy_bot):
    bot, dev = policy_bot
    if not dev:
        pytest.skip("per-card case")
    bot.lock("alice", "lock a dev0 10m")
    before = bot.state_snapshot()[1]
    assert "❌" in text(bot.lock("alice", "lock a dev1 10m"))
    assert bot.state_snapshot()[1] == before


def test_existing_holding_survives_lowered_cap(policy_bot):
    bot, dev = policy_bot
    bot.config.set_val("MAX_RESOURCES_PER_USER", -1)
    bot.lock("alice", f"lock a,b{dev} 10m")
    before = bot.state_snapshot()[1]
    bot.config.set_val("MAX_RESOURCES_PER_USER", 1)
    assert bot.state_snapshot()[1] == before
    assert public_policy(before, bot.config, "alice")["claimed_resource_count"] == 2
    bot.unlock("alice", f"free b{dev}")
    assert public_policy(bot.state_snapshot()[1], bot.config, "alice")["claimed_resource_count"] == 1


@pytest.mark.parametrize("cards", [False, True])
def test_booking_claim_cannot_extend_and_promotion_survives_lowered_cap(tmp_path, cards):
    kind, cls = ("QUEUE_DEVICE", QueueDeviceBot) if cards else ("QUEUE", QueueBot)
    bot = cls(
        config_dict={
            "BOT_ID": "q",
            "BOT_TYPE": kind,
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu"], "b": ["gpu"]} if cards else ["a", "b"],
            "WEBHOOK_URL": "",
            "FORBID_RELOCK": True,
        }
    )
    dev = " dev0" if cards else ""
    bot.book("alice", f"book a,b{dev} 10m")
    bot.config.set_val("MAX_RESOURCES_PER_USER", 1)
    before = bot.state_snapshot()[1]
    assert "❌" in text(bot.lock("alice", f"lock a{dev} 20m"))
    assert bot.state_snapshot()[1] == before
    assert "❌" not in text(bot.lock("alice", f"lock a{dev}"))
    assert "❌" in text(bot.book("alice", f"book a{dev} 5m"))
    assert "❌" in text(bot.take("alice", f"take b{dev} 20m"))  # cannot inflate booked duration


def test_parallel_requests_do_not_exceed_bot_cap(policy_bot):
    bot, dev = policy_bot
    barrier = Barrier(2)
    original = bot.parse_command

    def parse(*args, **kwargs):
        value = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return value

    bot.parse_command = parse
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: bot.lock("alice", f"lock {n}{dev} 10m"), ["a", "b"]))
    assert sum("❌" in text(reply) for reply in results) == 1
    assert public_policy(bot.state_snapshot()[1], bot.config, "alice")["claimed_resource_count"] == 1


def test_expired_hold_and_claim_window_do_not_consume_quota():
    from lockbot.core.config import Config

    now = time.time()
    state = {
        "a": {"current_users": [{"user_id": "alice", "start_time": now - 20, "duration": 10}]},
        "b": {"booking_list": [{"user_id": "alice", "start_time": now - 400, "duration": 1000, "is_notified": True}]},
    }
    assert public_policy(state, Config(), "alice", now)["claimed_resource_count"] == 0


def test_api_machine_budget_stays_separate_from_card_quota(tmp_path):
    from lockbot.core.base_bot import AcquisitionLimitExceeded

    bot = DeviceBot(
        config_dict={
            "BOT_ID": "cards",
            "BOT_TYPE": "DEVICE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu", "gpu"], "b": ["gpu"]},
            "MAX_RESOURCES_PER_USER": 2,
            "WEBHOOK_URL": "",
        }
    )
    assert "❌" not in text(bot.lock("alice", "lock a dev0,1 10m", acquisition_limit=1))
    with pytest.raises(AcquisitionLimitExceeded):
        bot.lock("alice", "lock b dev0 10m", acquisition_limit=1)


def test_shared_card_counts_once_for_each_user(tmp_path):
    bot = DeviceBot(
        config_dict={
            "BOT_ID": "shared",
            "BOT_TYPE": "DEVICE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu", "gpu"]},
            "MAX_RESOURCES_PER_USER": 1,
            "WEBHOOK_URL": "",
        }
    )
    for user_id in ("alice", "bob"):
        assert "❌" not in text(bot.slock(user_id, "slock a dev0 10m"))
        assert "❌" in text(bot.slock(user_id, "slock a dev1 10m"))
        assert public_policy(bot.state_snapshot()[1], bot.config, user_id)["claimed_resource_count"] == 1


def test_help_and_expiry_reminder_do_not_suggest_forbidden_renewal(policy_bot):
    bot, _ = policy_bot
    bot.config.set_val("EARLY_NOTIFY", True)
    for lang in ("zh", "en"):
        bot.config.set_val("LANGUAGE", lang)
        assert "early_extend_reminder" not in bot._build_alert_header()
        assert "增加时间" not in bot._build_alert_header()
        assert "use lock/slock to extend" not in bot._build_alert_header()
        assert "repeated lock extends" not in text(bot.print_help("alice"))


def test_duplicate_targets_do_not_renew_within_one_command(policy_bot):
    bot, dev = policy_bot
    assert "❌" not in text(bot.lock("alice", f"lock a,a{dev} 10m"))
    entry = bot.state_snapshot()[1]["a"]
    if dev:
        entry = entry[0]
    assert entry["current_users"][0]["duration"] == 600
