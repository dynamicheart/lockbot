import copy
import os
import re
import time

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.io import (
    create_or_load_device_state,
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.utils import (
    format_duration,
)


def mock_user_info(user_id, duration_secs):
    """Create a user info dict with the given user_id and duration."""
    return {"user_id": user_id, "start_time": int(time.time()), "duration": duration_secs, "is_notified": False}


@pytest.fixture(autouse=True)
def bot(tmp_path):
    """Create an isolated DeviceBot instance for testing."""
    test_bot_id = "test_device_bot"
    data_dir = str(tmp_path)

    config_dict = {
        "BOT_ID": test_bot_id,
        "DATA_DIR": data_dir,
        "CLUSTER_CONFIGS": {"test": ["A100"] * 4, "test2": ["H100"] * 2, "test3": ["B100"] * 3},
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": 10800,
        "EARLY_NOTIFY": True,
        "TIME_ALERT": 300,
        "BOT_TYPE": "DEVICE",
        "WEBHOOK_URL": "",
    }

    bot = DeviceBot(config_dict=config_dict)

    bot.state.bot_state = {
        "test": [{"dev_id": i, "dev_model": "A100", "status": "idle", "current_users": []} for i in range(4)],
        "test2": [{"dev_id": i, "dev_model": "H100", "status": "idle", "current_users": []} for i in range(2)],
        "test3": [{"dev_id": i, "dev_model": "B100", "status": "idle", "current_users": []} for i in range(3)],
    }

    yield bot


def test_parse_command(bot):
    """Test parse command."""

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev0-1 2h", True)
    assert ok and node[0] == "test" and devs == [[0, 1]] and dur == 7200

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev0，2", True)
    assert ok and sorted(devs[0]) == [0, 2] and dur == 3600

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test", True)
    assert ok and devs == [[0, 1, 2, 3]]

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test 2h", True)
    assert ok and devs == [[0, 1, 2, 3]] and dur == 7200

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev1-2", False)
    assert ok and dur == 0

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock nonexistent dev0-1", True)
    assert not ok and "节点" in err["message"]["body"][0]["content"]

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev3-1", True)
    assert not ok and "min(" in err["message"]["body"][0]["content"]

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev10", True)
    assert not ok and "dev_id有误" in err["message"]["body"][0]["content"]

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "locktestdev01", True)
    assert not ok and "命令格式" in err["message"]["body"][0]["content"]

    ok, err, node, devs, dur = bot.parse_command("user1", "lock", "lock test dev0 0h", True)
    assert not ok and "时间应大于0" in err["message"]["body"][0]["content"]

    multi_node_cmd = "lock test,test2 dev0-1 3h"
    ok, err, nodes, dev_lists, dur = bot.parse_command("user1", "lock", multi_node_cmd, True)
    assert ok
    assert nodes == ["test", "test2"]
    assert dev_lists == [[0, 1], [0, 1]]
    assert dur == 10800

    multi_node_cmd2 = "lock test， test2 1h"
    ok, err, nodes, dev_lists, dur = bot.parse_command("user1", "lock", multi_node_cmd2, True)
    assert ok
    assert nodes == ["test", "test2"]
    assert dev_lists == [[0, 1, 2, 3], [0, 1]]
    assert dur == 3600

    multi_node_cmd3 = "lock test、 test2 dev0-3 2h"
    ok, err, nodes, dev_lists, dur = bot.parse_command("user1", "lock", multi_node_cmd3, True)
    assert not ok
    assert "test2" in err["message"]["body"][0]["content"] and "dev_id有误" in err["message"]["body"][0]["content"]

    multi_node_cmd2 = "lock test， test2"
    ok, err, nodes, dev_lists, dur = bot.parse_command("user1", "lock", multi_node_cmd2, True)
    assert ok
    assert nodes == ["test", "test2"]
    assert dev_lists == [[0, 1, 2, 3], [0, 1]]
    assert dur == 3600

    multi_node_cmd3 = "slock test， test2 、 test3 3h"
    ok, err, nodes, dev_lists, dur = bot.parse_command("user1", "slock", multi_node_cmd3, True)
    assert ok
    assert nodes == ["test", "test2", "test3"]
    assert dev_lists == [[0, 1, 2, 3], [0, 1], [0, 1, 2]]
    assert dur == 10800


def test_query(bot):
    """Test query."""
    result = bot.query("user1")
    assert "message" in result and "test使用情况" in result["message"]["body"][0]["content"]


def test_lock_unlock(bot):
    """Test lock unlock."""
    reply = bot.lock("user1", "lock test dev0 1h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]

    reply = bot.unlock("user1", "unlock test dev0")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    reply = bot.lock("user1", "lock test,test2 dev0 1h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]

    reply = bot.unlock("user1", "unlock test, test2")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    reply = bot.lock("user1", "lock test, test2")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]

    reply = bot.unlock("user1", "free test, test2")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    reply = bot.lock("user1", "lock test, test2 3h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]

    reply = bot.unlock("user1", "free test")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    reply = bot.unlock("user1", "free")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    reply = bot.slock("user1", "slock test, test2 3h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]


def test_slock(bot):
    """Test slock."""
    reply = bot.slock("user1", "slock test dev1 30m")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]

    reply = bot.slock("user1", "slock test, test2 dev1 30m")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"]


def test_unlock_all(bot):
    """Test unlock all."""
    bot.lock("user1", "lock test dev0 1h")
    reply = bot.unlock("user1", "unlock")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]

    bot.lock("user1", "lock test, test2 dev0 1h")
    reply = bot.unlock("user1", "unlock")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"]


def test_usage_display_after_lock_and_slock(bot):
    """Test usage display after lock and slock."""
    bot.config.set_val(
        "DEFAULT_USER_INFO",
        {
            "user_id": "xxx",
            "start_time": 0,
            "duration": 0,
            "is_notified": False,
        },
    )

    cluster_state = {
        "test": [
            {"dev_id": 0, "dev_model": "A100", "status": "idle", "current_users": []},
            {"dev_id": 1, "dev_model": "V100", "status": "idle", "current_users": []},
            {"dev_id": 2, "dev_model": "A100", "status": "idle", "current_users": []},
            {"dev_id": 3, "dev_model": "A100", "status": "idle", "current_users": []},
        ]
    }
    bot.state.bot_state = cluster_state

    bot.lock("lock_user", "lock test dev0 1h")

    bot.slock("slock_user", "slock test dev1 30m")

    result = bot._current_usage("test").replace(" ", "")

    assert "dev0A100" in result
    assert "lock_user(独占)" in result
    assert re.search(r"lock_user\(独占\).*1[.]?0?小时|60分钟", result)

    assert "dev1V100" in result
    assert "slock_user(共享)" in result
    assert re.search(r"slock_user\(共享\).*30分钟", result)


def test_kickout(bot):
    """Test kickout."""
    bot.lock("user1", "lock test dev0 1h")
    reply = bot.kickout("admin", "kickout test dev0")
    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"]

    bot.lock("user1", "lock test, test2 dev0 1h")
    reply = bot.kickout("admin", "kickout test, test2 dev0")
    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"]

    bot.lock("user1", "lock test, test2 dev0 1h")
    reply = bot.kickout("admin", "kickout test, test2")
    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"]

    reply = bot.lock("admin", "lock test, test2")
    assert "admin" in reply["message"]["body"][1]["atuserids"]
    assert "user1" not in reply["message"]["body"][1]["atuserids"]


def test_show_error(bot):
    """Test show error."""
    msg = bot.show_error("user1", "错误信息")
    assert "❌错误信息" in msg["message"]["body"][0]["content"]


def test_print_help(bot):
    """Test print help."""
    msg = bot.print_help("user1")
    assert "📖【使用方法】" in msg["message"]["body"][0]["content"]


def test_current_usage_hetero_node_hint(bot):
    """Test current usage hetero node hint (auto-detected from mixed models)."""
    bot.state.bot_state = {
        "test": [
            {"dev_id": 0, "dev_model": "A100", "status": "idle", "current_users": []},
            {"dev_id": 1, "dev_model": "V100", "status": "idle", "current_users": []},
        ]
    }
    result = bot._current_usage("test")
    assert "❗️【注意test的GPU顺序】" in result


def test_timer_routine_trigger(bot, monkeypatch):
    """Test timer routine trigger."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", False)
    bot.config.set_val("TIME_ALERT", 300)

    user = {
        "user_id": "user1",
        "start_time": int(time.time()) - 5000,
        "duration": 3600,
        "is_notified": False,
    }

    bot.state.bot_state = {
        "test": [
            {
                "dev_id": 0,
                "dev_model": "A100",
                "status": "exclusive",
                "current_users": [copy.deepcopy(user)],
            }
        ]
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    bot._check_and_notify()

    assert "user1" in sent_payload["json"]["message"]["body"][1]["atuserids"]
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"]


def test_timer_routine_no_trigger_devicebot(bot, monkeypatch):
    """Test timer routine no trigger devicebot."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", False)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {"user_id": "user3", "start_time": now - 1000, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": [
            {
                "dev_id": 2,
                "dev_model": "A100",
                "status": "exclusive",
                "current_users": [copy.deepcopy(user)],
            }
        ]
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert sent_payload == {}, "should not send notification before timeout"


def test_timer_routine_trigger_early_notification_devicebot(bot, monkeypatch):
    """Test timer routine trigger early notification devicebot."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    overtime = 100

    user = {"user_id": "user2", "start_time": now - duration + overtime, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": [
            {
                "dev_id": 1,
                "dev_model": "A100",
                "status": "exclusive",
                "current_users": [copy.deepcopy(user)],
            }
        ]
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert "user2" in sent_payload["json"]["message"]["body"][1]["atuserids"], "notification missing user ID"
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"], "notification missing release prompt"
    alert_dur = format_duration(bot.config.get_val("TIME_ALERT"))
    assert alert_dur in sent_payload["json"]["message"]["body"][0]["content"], (
        "early notification missing alert duration"
    )


def test_timer_routine_no_trigger_early_notification_devicebot(bot, monkeypatch):
    """Test timer routine no trigger early notification devicebot."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {"user_id": "user4", "start_time": now - 500, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": [
            {
                "dev_id": 3,
                "dev_model": "A100",
                "status": "exclusive",
                "current_users": [copy.deepcopy(user)],
            }
        ]
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert sent_payload == {}, "should not send notification before early reminder time"


def test_early_notify_no_double_notification_on_expiry(bot, monkeypatch):
    """EARLY_NOTIFY=True: once early warning is sent (is_notified=True), expiry must NOT send a second notification."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {
        "user_id": "userX",
        "start_time": now - duration - 10,  # already expired
        "duration": duration,
        "is_notified": True,  # early warning was already sent on a previous tick
    }

    bot.state.bot_state = {
        "test": [{"dev_id": 0, "dev_model": "A100", "status": "exclusive", "current_users": [copy.deepcopy(user)]}]
    }

    send_count = {"n": 0}

    def fake_send(msg):
        send_count["n"] += 1
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert send_count["n"] == 0, "should NOT send a second notification at expiry when early warning was already sent"


def test_early_notify_fallback_on_scheduler_delay(bot, monkeypatch):
    """EARLY_NOTIFY=True: if scheduler delayed past early window without notifying, send one notification at expiry."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {
        "user_id": "userY",
        "start_time": now - duration - 10,  # expired, early window already passed
        "duration": duration,
        "is_notified": False,  # early warning was never sent due to scheduler delay
    }

    bot.state.bot_state = {
        "test": [{"dev_id": 0, "dev_model": "A100", "status": "exclusive", "current_users": [copy.deepcopy(user)]}]
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["msg"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert "msg" in sent_payload, "should send fallback notification at expiry when early warning was never sent"
    assert "userY" in sent_payload["msg"]["message"]["body"][1]["atuserids"]


def test_io_create_and_save(bot):
    """Test io create and save."""
    status = create_or_load_device_state(config=bot.config)
    assert "test" in status
    save_bot_state_to_file(status, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    assert os.path.exists(os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot_state.json"))


def test_max_lock_duration_exceeded(bot):
    """Test max lock duration exceeded."""
    bot.config.set_val("MAX_LOCK_DURATION", 3600)

    reply1 = bot.lock("user1", "lock test dev0 30m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"]

    reply2 = bot.lock("user1", "lock test dev0 45m")
    assert "❌" in reply2["message"]["body"][0]["content"]


def test_max_slock_duration_exceeded(bot):
    """Test max slock duration exceeded."""
    bot.config.set_val("MAX_LOCK_DURATION", 3600)

    reply1 = bot.slock("user1", "slock test dev1 30m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"]

    reply2 = bot.slock("user1", "slock test dev1 45m")
    assert "❌" in reply2["message"]["body"][0]["content"]


def test_slock_multiple_users(bot):
    """Test slock multiple users."""
    reply1 = bot.slock("userA", "slock test dev2 20m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"]

    reply2 = bot.slock("userB", "slock test dev2 25m")
    assert "✅【资源申请成功】" in reply2["message"]["body"][0]["content"]

    reply3 = bot.lock("userC", "lock test dev2 15m")
    assert "❌" in reply3["message"]["body"][0]["content"]

    reply3 = bot.lock("userC", "lock test, test2 dev2 15m")
    assert "❌" in reply3["message"]["body"][0]["content"]


def test_io_log_to_file(bot):
    """Test io log to file."""
    log_to_file("user1", "lock", "test", [0, 1], 3600, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    log_file = os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot.log")
    assert os.path.exists(log_file)
    with open(log_file, encoding="utf-8") as f:
        lines = f.readlines()
    assert any("user1" in line and "lock" in line for line in lines)


# ── _notify_state_changed callback ───────────────────────────────────────────


def test_lock_calls_notify_state_changed(bot):
    """Successful lock() must invoke _on_state_changed so the scheduler wakes up."""
    calls = []
    bot._on_state_changed = lambda: calls.append(1)

    bot.lock("user1", "lock test dev0 1h")
    assert len(calls) == 1, "lock() should have called _on_state_changed once"


def test_slock_calls_notify_state_changed(bot):
    """Successful slock() must invoke _on_state_changed."""
    calls = []
    bot._on_state_changed = lambda: calls.append(1)

    bot.slock("user1", "slock test dev0 1h")
    assert len(calls) == 1, "slock() should have called _on_state_changed once"


def test_failed_lock_does_not_call_notify(bot):
    """An error lock (e.g. device already held) must NOT call _on_state_changed."""
    bot.lock("user1", "lock test dev0 1h")  # dev0 now held by user1

    calls = []
    bot._on_state_changed = lambda: calls.append(1)

    # user2 tries to take an exclusively held device → error
    bot.lock("user2", "lock test dev0 1h")
    assert len(calls) == 0, "failed lock() must not call _on_state_changed"


def test_notify_not_set_does_not_raise(bot):
    """lock() must not raise when _on_state_changed is None (default)."""
    assert bot._on_state_changed is None
    bot.lock("user1", "lock test dev0 1h")  # must not raise
