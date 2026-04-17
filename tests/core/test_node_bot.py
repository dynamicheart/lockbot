import copy
import os
import re
import time

import pytest
from lockbot.core.io import (
    create_or_load_node_state,
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.node_bot import NodeBot
from lockbot.core.utils import (
    format_duration,
)


def mock_user_info(user_id, duration_secs):
    """Create a user info dict with the given user_id and duration."""
    return {"user_id": user_id, "start_time": int(time.time()), "duration": duration_secs, "is_notified": False}


@pytest.fixture(autouse=True)
def bot(tmp_path):
    """Create an isolated NodeBot instance for testing."""
    test_bot_id = "test_node_bot"
    data_dir = str(tmp_path)

    config_dict = {
        "BOT_ID": test_bot_id,
        "DATA_DIR": data_dir,
        "CLUSTER_CONFIGS": ["test"],
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": 10800,
        "EARLY_NOTIFY": True,
        "TIME_ALERT": 300,
        "BOT_TYPE": "NODE",
        "WEBHOOK_URL": "",
    }

    bot = NodeBot(config_dict=config_dict)

    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        }
    }

    yield bot


def test_parse_command(bot):
    """Test parse command."""
    ok, err, node, dur = bot.parse_command("user1", "lock", "lock test 2h", True)
    assert ok, f"parse command failed: {err}"
    assert node == ["test"], f"parsed node name is incorrect, expected 'test', got {node}"
    assert dur == 7200, "parsed duration is incorrect"


def test_query(bot):
    """Test query."""
    result = bot.query("user1")
    assert "message" in result, "result missing 'message' field"
    assert "集群使用详情" in result["message"]["body"][0]["content"], "query info missing usage description"
    assert "test" in result["message"]["body"][0]["content"], "query info missing node name"


def test_lock_unlock(bot):
    """Test lock unlock."""
    reply = bot.lock("user1", "lock test 1h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"], "lock resource failed"

    reply2 = bot.unlock("user1", "unlock test")
    assert "✅【资源释放成功】" in reply2["message"]["body"][0]["content"], "unlock resource failed"


def test_slock(bot):
    """Test slock."""
    reply = bot.slock("user1", "slock test 30m")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"], "shared lock resource failed"


def test_unlock_all(bot):
    """Test unlock all."""
    bot.lock("user1", "lock test 1h")
    reply = bot.unlock("user1", "unlock")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"], "unlock all resources failed"


def test_usage_display_after_lock_and_slock(bot):
    """Test usage display after lock and slock."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test", "test2"])
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        },
        "test2": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        },
    }

    bot.lock("lock_user", "lock test 1h")
    bot.slock("slock_user", "slock test2 30m")

    result1 = bot._current_usage("test").replace(" ", "")
    result2 = bot._current_usage("test2").replace(" ", "")

    assert "test" in result1, "usage display error for node test"
    assert "lock_user(独占)" in result1, "exclusive lock display error"
    assert re.search(r"lock_user\(独占\).*1[.]?0?小时|60分钟", result1), "exclusive lock duration display error"

    assert "test2" in result2, "usage display error for node test2"
    assert "slock_user(共享)" in result2, "shared lock display error"
    assert re.search(r"slock_user\(共享\).*30分钟", result2), "shared lock duration display error"


def test_kickout(bot):
    """Test kickout."""
    bot.lock("user1", "lock test 1h")
    reply = bot.kickout("admin", "kickout test")
    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"], "force release resource failed"

    reply = bot.lock("admin", "lock test")
    assert "admin" in reply["message"]["body"][1]["atuserids"]
    assert "user1" not in reply["message"]["body"][1]["atuserids"]


def test_show_error(bot):
    """Test show error."""
    msg = bot.show_error("user1", "错误信息")
    assert "❌错误信息" in msg["message"]["body"][0]["content"], "error message display incorrect"


def test_print_help(bot):
    """Test print help."""
    msg = bot.print_help("user1")
    assert "📖【使用方法】" in msg["message"]["body"][0]["content"], "help message display incorrect"


def test_timer_routine_trigger(bot, monkeypatch):
    """Test timer routine trigger."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", False)
    bot.config.set_val("TIME_ALERT", 300)

    user = {"user_id": "user1", "start_time": int(time.time()) - 5000, "duration": 3600, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user)],
            "booking_list": [],
        }
    }

    sent_payload = {}

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert "user1" in sent_payload["json"]["message"]["body"][1]["atuserids"], "notification missing user ID"
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"], "notification missing release prompt"


def test_timer_routine_no_trigger(bot, monkeypatch):
    """Test timer routine no trigger."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", False)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {"user_id": "user3", "start_time": now - 1000, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user)],
            "booking_list": [],
        }
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert sent_payload == {}, "should not send notification when condition is not met"


def test_timer_routine_trigger_early_notify(bot, monkeypatch):
    """Test timer routine trigger early notification mode."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {"user_id": "user2", "start_time": now - duration + 100, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user)],
            "booking_list": [],
        }
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert "user2" in sent_payload["json"]["message"]["body"][1]["atuserids"], "early notification missing user ID"
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"], "early notification missing release prompt"
    alert_dur = format_duration(bot.config.get_val("TIME_ALERT"))
    assert alert_dur in sent_payload["json"]["message"]["body"][0]["content"], (
        "early notification missing alert duration"
    )


def test_timer_routine_no_trigger_early_notification(bot, monkeypatch):
    """Test timer routine no trigger early notification."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")
    bot.config.set_val("EARLY_NOTIFY", True)
    bot.config.set_val("TIME_ALERT", 300)

    now = int(time.time())
    duration = 3600
    user = {"user_id": "user4", "start_time": now - 500, "duration": duration, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user)],
            "booking_list": [],
        }
    }

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)
    bot._check_and_notify()

    assert sent_payload == {}, "should not send notification before early notification time"


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

    bot.state.bot_state = {"test": {"status": "exclusive", "current_users": [copy.deepcopy(user)], "booking_list": []}}

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

    bot.state.bot_state = {"test": {"status": "exclusive", "current_users": [copy.deepcopy(user)], "booking_list": []}}

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
    status = create_or_load_node_state(config=bot.config)
    assert "test" in status, "created state missing 'test' node"
    save_bot_state_to_file(status, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    assert os.path.exists(os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot_state.json")), (
        "state file not created"
    )


def test_max_lock_duration_exceeded(bot):
    """Test max lock duration exceeded."""
    bot.config.set_val("MAX_LOCK_DURATION", 3600)

    reply1 = bot.lock("user1", "lock test 30m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"], "first lock failed"

    reply2 = bot.lock("user1", "lock test 45m")
    assert "❌" in reply2["message"]["body"][0]["content"], "exceeding max lock duration was not rejected"


def test_max_slock_duration_exceeded(bot):
    """Test max slock duration exceeded."""
    bot.config.set_val("MAX_LOCK_DURATION", 3600)

    reply1 = bot.slock("user1", "slock test 30m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"], "first shared lock failed"

    reply2 = bot.slock("user1", "slock test 45m")
    assert "❌" in reply2["message"]["body"][0]["content"], "exceeding max shared lock duration was not rejected"


def test_slock_multiple_users(bot):
    """Test slock multiple users."""
    reply1 = bot.slock("userA", "slock test 20m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"], "shared lock failed for user A"

    reply2 = bot.slock("userB", "slock test 25m")
    assert "✅【资源申请成功】" in reply2["message"]["body"][0]["content"], "shared lock failed for user B"

    reply3 = bot.lock("userC", "lock test 15m")
    assert "❌" in reply3["message"]["body"][0]["content"], "exclusive lock still allowed under shared state"


def test_io_log_to_file(bot):
    """Test io log to file."""
    log_to_file("user1", "lock", "test", 3600, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    log_file = os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot.log")
    assert os.path.exists(log_file), "log file not created"
    with open(log_file, encoding="utf-8") as f:
        lines = f.readlines()
    assert any("user1" in line and "lock" in line and "test" in line for line in lines), "log file content incorrect"
