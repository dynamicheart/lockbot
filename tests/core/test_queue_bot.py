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
from lockbot.core.queue_bot import QueueBot
from lockbot.core.utils import (
    format_duration,
)


def mock_user_info(user_id, duration_secs):
    """Create a user info dict with the given user_id and duration."""
    return {"user_id": user_id, "start_time": int(time.time()), "duration": duration_secs, "is_notified": False}


@pytest.fixture(autouse=True)
def bot(tmp_path):
    """Create an isolated QueueBot instance for testing."""
    test_bot_id = "test_queue_bot"
    data_dir = str(tmp_path)

    config_dict = {
        "BOT_ID": test_bot_id,
        "DATA_DIR": data_dir,
        "CLUSTER_CONFIGS": ["test"],
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": 10800,
        "BOT_TYPE": "QUEUE",
        "WEBHOOK_URL": "",
        "EARLY_NOTIFY": False,
        "TIME_ALERT": 300,
    }

    bot = QueueBot(config_dict=config_dict)

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
    assert ok, f"解析命令失败: {err}"
    assert node == ["test"], f"Parsed node name incorrect, expected 'test', got {node}"
    assert dur == 7200, "Parsed duration incorrect"


def test_query(bot):
    """Test query."""
    result = bot.query("user1")
    assert "message" in result, "Missing 'message' field in result"
    assert "集群使用详情" in result["message"]["body"][0]["content"], "Missing usage description in query info"
    assert "test" in result["message"]["body"][0]["content"], "Missing node name in query info"


def test_lock_unlock(bot):
    """Test lock unlock."""
    reply = bot.lock("user1", "lock test 1h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"], "Failed to lock resource"

    reply2 = bot.unlock("user1", "unlock test")
    assert "✅【资源释放成功】" in reply2["message"]["body"][0]["content"], "Failed to release resource"


def test_unlock_all(bot):
    """Test unlock all."""
    bot.lock("user1", "lock test 1h")
    reply = bot.unlock("user1", "unlock")
    assert "✅【资源释放成功】" in reply["message"]["body"][0]["content"], "Failed to release all resources"


def test_unlock_all_after_book(bot):
    """Test unlock all after book."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])

    reply_book = bot.book("user1", "book test 1h")
    assert "🗓️【排队成功】" in reply_book["message"]["body"][0]["content"], "Failed to queue"

    reply_unlock = bot.unlock("user1", "unlock")
    assert "✅【资源释放成功】" in reply_unlock["message"]["body"][0]["content"], "Failed to release all resources"

    node_status = bot.state.bot_state.get("test", {})
    assert all(user["user_id"] != "user1" for user in node_status.get("booking_list", [])), "User still in booking list"


def test_book_multiple_nodes(bot):
    """Test book multiple nodes."""
    bot.config.set_val(
        "CLUSTER_CONFIGS",
        {
            "node1": "Node One",
            "node2": "Node Two",
            "node3": "Node Three",
        },
    )

    bot.state.bot_state = {
        "node1": {"status": "idle", "current_users": [], "booking_list": []},
        "node2": {"status": "idle", "current_users": [], "booking_list": []},
        "node3": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        },
    }

    command = "book node1，node2 1h"
    reply = bot.book("user1", command)

    for node_key in ["node1", "node2"]:
        booking_list = bot.state.bot_state[node_key]["booking_list"]
        assert any(user["user_id"] == "user1" for user in booking_list), f"user1 应该在 {node_key} 的排队列表中"

    assert "🗓️【排队成功】" in reply["message"]["body"][0]["content"], "Failed to queue or wrong prompt"


def test_free_cancels_booking(bot):
    """Test free cancels booking."""
    bot.lock("user1", "lock test 1h")
    reply_book = bot.book("user2", "book test 2h")
    assert "排队成功" in reply_book["message"]["body"][0]["content"], "Failed to queue"

    reply_free = bot.unlock("user2", "free test")

    assert "✅【资源释放成功】" in reply_free["message"]["body"][0]["content"], "Failed to cancel booking"

    node_state = bot.state.bot_state["test"]
    assert len(node_state["booking_list"]) == 0, "free 后 booking_list 未清空"

    assert len(node_state["current_users"]) == 1 and node_state["current_users"][0]["user_id"] == "user1", (
        "free 不应影响当前锁定的用户"
    )


def test_lock_then_book(bot):
    """Test lock then book."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    user = {"user_id": "user1", "start_time": int(time.time()) - 5000, "duration": 3600, "is_notified": False}
    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user)],
            "booking_list": [],
        }
    }

    reply = bot.lock("user2", "lock test 1h")
    assert "❌" in reply["message"]["body"][0]["content"], "Should reject duplicate lock"

    reply2 = bot.book("user2", "book test 1h")
    assert "🗓️【排队成功】" in reply2["message"]["body"][0]["content"], (
        f"应允许排队, 但是显示{reply2['message']['body'][0]['content']}"
    )

    reply3 = bot.book("user3", "book test 1h")
    assert "🗓️【排队成功】" in reply3["message"]["body"][0]["content"], (
        f"应允许排队, 但是显示{reply3['message']['body'][0]['content']}"
    )


def test_forbid_duplicate_book(bot):
    """Test forbid duplicate book."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    user = {"user_id": "user1", "start_time": int(time.time()) - 5000, "duration": 3600, "is_notified": False}
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [copy.deepcopy(user)],
        }
    }

    reply = bot.book("user1", "book test 1h")
    assert "❌" in reply["message"]["body"][0]["content"], "Should reject duplicate booking"

    reply2 = bot.book("user2", "book test 1h")
    assert "🗓️【排队成功】" in reply2["message"]["body"][0]["content"], (
        f"不同用户应允许排队, 但是显示{reply2['message']['body'][0]['content']}"
    )


def test_locked_user_cannot_book_again(bot):
    """Test locked user cannot book again."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])

    locked_user = {"user_id": "user1", "start_time": int(time.time()) - 5000, "duration": 3600, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(locked_user)],
            "booking_list": [],
        }
    }

    reply = bot.book("user1", "book test 1h")
    assert "❌" in reply["message"]["body"][0]["content"], "User who locked node should not book again"

    reply2 = bot.book("user2", "book test 1h")
    assert "🗓️【排队成功】" in reply2["message"]["body"][0]["content"], "Other users should be able to book"


def test_current_usage_display(bot):
    """Test current usage display."""
    bot.state.bot_state = {"test": {"status": "idle", "current_users": [], "booking_list": []}}

    output = bot._current_usage()
    assert "空闲" in output, f"空闲节点显示异常：{output}"
    assert "排队" not in output, f"空闲节点不应显示排队：{output}"

    bot.lock("user1", "lock test 1h")
    output = bot._current_usage()
    assert "user1" in output and "小时" in output, f"锁定用户未显示或时间显示异常：{output}"
    assert "排队" not in output, f"无人排队时不应显示排队：{output}"

    bot.book("user2", "book test 1h")
    output = bot._current_usage()
    assert "⌛️ 排队" in output, f"有排队用户时应显示排队区块：{output}"
    assert "user2" in output, f"排队用户未显示：{output}"

    bot.unlock("user1", "unlock test")
    output = bot._current_usage()
    assert "空闲" in output, f"解锁后应显示空闲：{output}"
    assert "user2" in output and "⌛️ 排队" in output, f"排队用户未显示或排队区块缺失：{output}"

    bot.book("user3", "book test 2h")
    bot.book("user4", "book test 3h")
    output = bot._current_usage()
    assert all(uid in output for uid in ["user2", "user3", "user4"]), f"多用户排队显示异常：{output}"

    pos2, pos3 = output.find("user2"), output.find("user3")
    assert 0 <= pos2 < pos3, f"排队顺序显示错误：{output}"

    print("✅ 所有节点状态显示测试通过")


def test_book_when_no_lock(bot):
    """Test book when no lock."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        }
    }

    # user1 book
    reply = bot.book("user1", "book test 1h")
    assert "🗓️【排队成功】" in reply["message"]["body"][0]["content"], "Should allow booking"


def test_lock_when_free_or_first_in_queue(bot):
    """Test lock when free or first in queue."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [],
        }
    }

    reply = bot.lock("user1", "lock test 1h")
    assert "✅【资源申请成功】" in reply["message"]["body"][0]["content"], "Should lock directly when idle"

    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [
                {"user_id": "user2", "start_time": int(time.time()), "duration": 3600, "is_notified": False}
            ],
        }
    }

    reply2 = bot.lock("user2", "lock test 1h")
    assert "✅【资源申请成功】" in reply2["message"]["body"][0]["content"], "First in queue should be able to lock"


def test_extend_lock_should_notify_waiting_users(bot):
    """Test extend lock should notify waiting users."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())
    user_lock = {"user_id": "user1", "start_time": now - 1800, "duration": 3600, "is_notified": False}
    booking_users = [
        {"user_id": "user2", "start_time": now - 100, "duration": 1800, "is_notified": False},
        {"user_id": "user3", "start_time": now - 50, "duration": 1800, "is_notified": False},
    ]
    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user_lock)],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.lock("user1", "lock test 2h")

    assert (
        "✅【资源申请成功】" in reply["message"]["body"][0]["content"]
        and "请注意等待时间已增加" in reply["message"]["body"][0]["content"]
    ), "应提示延长成功"

    notified_users = set(reply["message"]["body"][1]["atuserids"])
    expected_users = {"user1", "user2", "user3"}
    assert notified_users == expected_users, f"应通知排队用户 {expected_users}，实际 {notified_users}"


def test_first_booking_user_lock_with_larger_duration_notifies_others(bot):
    """Test first booking user lock with larger duration notifies others."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    booking_users = [
        {"user_id": "user1", "start_time": now - 10, "duration": 300, "is_notified": True},
        {"user_id": "user2", "start_time": now - 5, "duration": 300, "is_notified": False},
        {"user_id": "user3", "start_time": now - 2, "duration": 300, "is_notified": False},
    ]
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.lock("user1", "lock test 15m")  # 15m = 900s

    assert "✅" in reply["message"]["body"][0]["content"], "lock 应成功"
    notified_users = set(reply["message"]["body"][1]["atuserids"])
    expected_users = {"user1", "user2", "user3"}
    assert notified_users == expected_users, f"应通知排队用户 {expected_users}，实际 {notified_users}"


def test_first_booking_user_lock_within_booking_duration_no_notify(bot):
    """Test first booking user lock within booking duration no notify."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    booking_users = [
        {"user_id": "user1", "start_time": now - 10, "duration": 600, "is_notified": True},
        {"user_id": "user2", "start_time": now - 5, "duration": 600, "is_notified": False},
        {"user_id": "user3", "start_time": now - 2, "duration": 600, "is_notified": False},
    ]
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.lock("user1", "lock test 5m")  # 5m = 300s

    assert "✅" in reply["message"]["body"][0]["content"], "lock 应成功"
    notified = set(reply["message"]["body"][1]["atuserids"])
    assert not ({"user2", "user3"} & notified), f"不应通知其它排队用户，实际通知：{notified}"


def test_lock_without_duration_uses_booking_duration_and_behaves_accordingly(bot):
    """Test lock without duration uses booking duration and behaves accordingly."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    booking_users = [
        {"user_id": "user1", "start_time": now - 10, "duration": 300, "is_notified": True},
        {"user_id": "user2", "start_time": now - 5, "duration": 300, "is_notified": False},
    ]
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.lock("user1", "lock test")

    assert "✅" in reply["message"]["body"][0]["content"], "lock 应成功"

    assert "5 分钟" in reply["message"]["body"][0]["content"], "Duration should be 5m"
    notified = set(reply["message"]["body"][1]["atuserids"])
    assert "user2" not in notified, f"不应通知 user2，实际通知：{notified}, {reply['message']['body'][0]['content']}"


def test_take_when_no_lock_succeeds_and_notify_all(bot):
    """Test take when no lock succeeds and notify all."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    booking_users = [
        {"user_id": "user2", "start_time": now - 100, "duration": 1800, "is_notified": False},
        {"user_id": "user3", "start_time": now - 50, "duration": 1800, "is_notified": False},
    ]

    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.take("user1", "take test")

    assert "🏁【资源抢占成功】" in reply["message"]["body"][0]["content"], "Take should succeed"

    notified = set(reply["message"]["body"][1]["atuserids"])
    expected = {"user1", "user2", "user3"}
    assert notified == expected, f"应通知抢占者和所有排队用户，实际通知: {notified}"

    current_users = bot.state.bot_state["test"]["current_users"]
    assert any(u["user_id"] == "user1" for u in current_users), "Take user should become current user"


def test_take_removes_self_from_booking_list_and_notify(bot):
    """Test take removes self from booking list and notify."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    booking_users = [
        {"user_id": "user1", "start_time": now - 200, "duration": 1800, "is_notified": False},
        {"user_id": "user2", "start_time": now - 100, "duration": 1800, "is_notified": False},
    ]

    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.take("user1", "take test")

    current_users = bot.state.bot_state["test"]["current_users"]
    assert any(u["user_id"] == "user1" for u in current_users), "Take user should become current user"

    booking_list = bot.state.bot_state["test"]["booking_list"]
    assert all(u["user_id"] != "user1" for u in booking_list), "Take user should be removed from booking list"

    notified = set(reply["message"]["body"][1]["atuserids"])
    expected = {u["user_id"] for u in booking_list}.union({"user1"})
    assert notified == expected, f"应通知剩余排队用户，实际通知: {notified}"


def test_take_when_lock_exists_preempt_and_notify(bot):
    """Test take when lock exists preempt and notify."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])
    now = int(time.time())

    current_user = {"user_id": "user0", "start_time": now - 900, "duration": 3600, "is_notified": False}
    booking_users = [
        {"user_id": "user2", "start_time": now - 100, "duration": 1800, "is_notified": False},
        {"user_id": "user3", "start_time": now - 50, "duration": 1800, "is_notified": False},
    ]

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(current_user)],
            "booking_list": copy.deepcopy(booking_users),
        }
    }

    reply = bot.take("user1", "take test")

    booking_list = bot.state.bot_state["test"]["booking_list"]
    first_booking = booking_list[0]
    remaining = current_user["duration"] - (now - current_user["start_time"])
    assert first_booking["user_id"] == current_user["user_id"], f"原锁定人应排队最前 {booking_list}"
    assert abs(first_booking["duration"] - remaining) < 5, (
        f"剩余时间应更新，期待约{remaining}，实际{first_booking['duration']}"
    )

    current_users = bot.state.bot_state["test"]["current_users"]
    assert len(current_users) == 1 and current_users[0]["user_id"] == "user1", "Take user should become current user"

    notified = set(reply["message"]["body"][1]["atuserids"])
    expected = {current_user["user_id"]} | {u["user_id"] for u in booking_list} | {"user1"}
    assert notified == expected, f"应通知锁定人和所有排队用户，实际通知: {notified}"


def test_locked_user_cannot_take(bot):
    """Test locked user cannot take."""
    bot.config.set_val("CLUSTER_CONFIGS", ["test"])

    locked_user = {"user_id": "user1", "start_time": int(time.time()) - 5000, "duration": 3600, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(locked_user)],
            "booking_list": [],
        }
    }

    reply = bot.take("user1", "take test")
    assert "❌" in reply["message"]["body"][0]["content"], "User who already locked should not be allowed to take again"

    reply2 = bot.take("user2", "take test")
    assert "🏁【资源抢占成功】" in reply2["message"]["body"][0]["content"], "Other users should be able to take"


def test_kickout(bot):
    """Test kickout."""
    bot.lock("user1", "lock test 1h")
    reply = bot.kickout("admin", "kickout test")
    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"], (
        "Failed to force release resource"
    )

    reply = bot.lock("admin", "lock test")
    assert "admin" in reply["message"]["body"][1]["atuserids"]
    assert "user1" not in reply["message"]["body"][1]["atuserids"]


def test_kickout_clears_booking_and_notifies_all(bot):
    """Test kickout clears booking and notifies all."""

    bot.lock("user1", "lock test 1h")
    bot.book("user2", "book test 1h")
    bot.book("user3", "book test 2h")

    state_before = bot.state.bot_state["test"]
    assert state_before["status"] != "idle"
    assert len(state_before["current_users"]) == 1
    assert len(state_before["booking_list"]) == 2

    reply = bot.kickout("admin", "kickout test")

    assert "✅【资源强制释放成功】by admin" in reply["message"]["body"][0]["content"], "kickout 未成功执行"

    atusers = set(reply["message"]["body"][1]["atuserids"])
    expected_users = {"admin", "user1", "user2", "user3"}
    assert atusers == expected_users, f"通知用户不正确: {atusers} != {expected_users}"

    state_after = bot.state.bot_state["test"]
    assert state_after["status"] == "idle", "kickout 后节点仍处于占用状态"
    assert len(state_after["current_users"]) == 0, "kickout 后 current_users 未清空"
    assert len(state_after["booking_list"]) == 0, "kickout 后 booking_list 未清空"


def test_kicklock(bot):
    """Test kicklock."""
    bot.lock("user1", "lock test 1h")
    bot.book("user2", "book test 1h")
    bot.book("user3", "book test 2h")

    state_before = bot.state.bot_state["test"]
    assert state_before["status"] != "idle", "Initial in_use should be True"
    assert len(state_before["current_users"]) == 1, "Initial current_users should be 1"
    assert len(state_before["booking_list"]) == 2, "Initial booking_list should be 2"

    reply = bot.kicklock("admin", "kicklock test")

    assert "✅【锁定已清空】by admin" in reply["message"]["body"][0]["content"], "kicklock 未成功执行"

    atusers = set(reply["message"]["body"][1]["atuserids"])
    expected_users = {"admin", "user1"}
    assert atusers == expected_users, f"通知用户不正确: {atusers} != {expected_users}"

    state_after = bot.state.bot_state["test"]
    assert state_after["status"] == "idle", "kicklock 后节点应为空闲状态"
    assert len(state_after["current_users"]) == 0, "kicklock 后 current_users 应被清空"
    assert len(state_after["booking_list"]) == 2, "kicklock 不应清空 booking_list"

    booking_users = [u["user_id"] for u in state_after["booking_list"]]
    assert booking_users == ["user2", "user3"], f"排队顺序被修改: {booking_users}"


def test_show_error(bot):
    """Test show error."""
    msg = bot.show_error("user1", "错误信息")
    assert "❌错误信息" in msg["message"]["body"][0]["content"], "Error message displayed incorrectly"


def test_print_help(bot):
    """Test print help."""
    msg = bot.print_help("user1")
    assert "📖【使用方法】" in msg["message"]["body"][0]["content"], "Help message displayed incorrectly"


def test_timer_routine_trigger(bot, monkeypatch):
    """Test timer routine trigger."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")

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

    assert "user1" in sent_payload["json"]["message"]["body"][1]["atuserids"], "Missing user ID in notification"
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"], "Missing release prompt in notification"


def test_timer_routine_no_trigger(bot, monkeypatch):
    """Test timer routine no trigger."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")

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

    assert sent_payload == {}, "Should not send notification when condition not met"


def test_io_create_and_save(bot):
    """Test io create and save."""
    status = create_or_load_node_state(config=bot.config)
    assert "test" in status, "Missing test node in created state"
    save_bot_state_to_file(status, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    assert os.path.exists(os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot_state.json")), (
        "State file not created"
    )


def test_max_lock_duration_exceeded(bot):
    """Test max lock duration exceeded."""
    bot.config.set_val("MAX_LOCK_DURATION", 3600)

    reply1 = bot.lock("user1", "lock test 30m")
    assert "✅【资源申请成功】" in reply1["message"]["body"][0]["content"], "First lock failed"

    reply2 = bot.lock("user1", "lock test 45m")
    assert "❌" in reply2["message"]["body"][0]["content"], "Exceeding max lock duration not rejected"


def test_check_and_notify(bot, monkeypatch):
    """Test check and notify."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)

    now = int(time.time())
    user_expired = {
        "user_id": "user1",
        "start_time": now - 5000,
        "duration": 3600,
        "is_notified": False,
    }

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(user_expired)],
            "booking_list": [],
        }
    }

    bot._check_and_notify()

    msg = sent_payload["json"]["message"]
    assert "user1" in msg["body"][1]["atuserids"], "Missing user ID in release notification"
    assert "释放" in msg["body"][0]["content"], "Missing release prompt"

    now = int(time.time())
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [{"user_id": "user2", "start_time": 0, "duration": 3600, "is_notified": False}],
        }
    }

    sent_payload.clear()
    bot._check_and_notify()

    msg = sent_payload["json"]["message"]
    assert "user2" in msg["body"][1]["atuserids"], "First booked user not notified"
    assert "可用" in msg["body"][0]["content"], "Available reminder content not generated"

    now = int(time.time())
    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [
                {"user_id": "user2", "start_time": now - 200 * 60, "duration": 3600, "is_notified": True},
                {"user_id": "user3", "start_time": 0, "duration": 3600, "is_notified": False},
            ],
        }
    }

    sent_payload.clear()
    bot._check_and_notify()

    msg = sent_payload["json"]["message"]
    body_text = msg["body"][0]["content"]
    assert "user3" in msg["body"][1]["atuserids"], "Second booked user not notified"
    assert "user2" not in bot.state.bot_state["test"]["booking_list"][0]["user_id"], "超时未响应用户未被移除"
    assert "可用" in body_text or "提醒" in body_text, "Reminder content not generated"

    print("✅ 所有 _check_and_notify 测试通过")


def test_check_and_notify_combined(bot, monkeypatch):
    """Test check and notify combined."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)

    now = int(time.time())
    expired_user = {
        "user_id": "user1",
        "start_time": now - 5000,
        "duration": 3600,
        "is_notified": False,
    }
    booking_user = {"user_id": "user2", "start_time": 0, "duration": 3600, "is_notified": False}

    bot.state.bot_state = {
        "test": {
            "status": "exclusive",
            "current_users": [copy.deepcopy(expired_user)],
            "booking_list": [copy.deepcopy(booking_user)],
        }
    }

    bot._check_and_notify()

    msg = sent_payload["json"]["message"]
    content = msg["body"][0]["content"]
    atuserids = msg["body"][1]["atuserids"]

    assert "释放" in content, f"释放提示缺失: {content}"
    assert "可用" in content or "提醒" in content, f"预约提醒缺失: {content}"
    assert "user1" in atuserids, f"释放用户未通知: {atuserids}"
    assert "user2" in atuserids, f"预约用户未通知: {atuserids}"

    node = bot.state.bot_state["test"]
    assert node["status"] == "idle", "Node not marked idle after release"
    assert len(node["current_users"]) == 0, "Locked user list not cleared after release"

    first_booking = node["booking_list"][0]
    assert first_booking["user_id"] == "user2", "First booked user incorrect"
    assert first_booking["is_notified"] is True, "First booked user not marked as notified"

    print("✅ 复合场景测试通过")


def test_check_and_notify_no_duplicate_reminder(bot, monkeypatch):
    """Test check and notify no duplicate reminder."""
    bot.config.set_val("WEBHOOK_URL", "http://fake")

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)

    now = int(time.time())

    booking_user = {"user_id": "user2", "start_time": now, "duration": 3600, "is_notified": True}

    bot.state.bot_state = {
        "test": {
            "status": "idle",
            "current_users": [],
            "booking_list": [copy.deepcopy(booking_user)],
        }
    }

    bot._check_and_notify()
    assert sent_payload == {}, "Should not re-notify already notified booked user"

    new_booking_user = {"user_id": "user3", "start_time": 0, "duration": 3600, "is_notified": False}
    bot.state.bot_state["test"]["booking_list"] = [
        {"user_id": "user2", "start_time": now - 6 * 60, "duration": 3600, "is_notified": True},
        new_booking_user,
    ]

    sent_payload.clear()
    bot._check_and_notify()

    msg = sent_payload["json"]["message"]
    assert "user3" in msg["body"][1]["atuserids"], "Should notify new booked user"
    assert all(
        u not in [user["user_id"] for user in bot.state.bot_state["test"]["booking_list"]]
        for u in ["user2"]
        if u != "user3"
    ), "超时旧预约用户未被移除"

    print("✅ 不重复提醒测试通过")


def test_release_expired_booking_and_notify_next(bot, monkeypatch):
    """Test release expired booking and notify next."""
    TIME_TO_LOCK = 300

    bot.config.set_val("WEBHOOK_URL", "http://fake")

    sent_payload = {}

    def fake_send(msg):
        sent_payload["json"] = msg
        return type("Resp", (), {"status_code": 200})()

    monkeypatch.setattr(bot.adapter, "send", fake_send)

    now = int(time.time())

    expired_user = {
        "user_id": "user_old",
        "start_time": now - (TIME_TO_LOCK + 10),
        "duration": 3600,
        "is_notified": True,
    }
    next_user = {"user_id": "user_next", "start_time": now, "duration": 3600, "is_notified": False}

    bot.state.bot_state = {
        "node01": {
            "status": "idle",
            "current_users": [],
            "booking_list": [copy.deepcopy(expired_user), copy.deepcopy(next_user)],
        }
    }

    sent_payload.clear()
    bot._check_and_notify()

    msg = sent_payload.get("json", {}).get("message", {})
    at_users = msg.get("body", [{}, {}])[1].get("atuserids", [])
    content = msg.get("body", [{}, {}])[0].get("content", "")

    booking_users = [u["user_id"] for u in bot.state.bot_state["node01"]["booking_list"]]
    assert "user_old" not in booking_users

    assert "user_next" in at_users

    expected_order = [
        r"资源已空闲，请在 [\d\.]+ (分钟|天|小时) 内lock:",
        r"⚠️ 以下预约已失效，请重新预约：",
        r"- user_old 的预约 node01 超时失效",
        r"🗓️ 目前待抢锁的预约：",
        r"- node01 user_next [\d\.]+ (分钟|天|小时)",
    ]

    idx = 0
    for pattern in expected_order:
        if pattern == r"- user_old 的预约 node01 超时失效":
            continue
        match = re.search(pattern, content)
        assert match is not None, f"通知内容缺失或格式不符: {pattern}, {content}"
        pos = match.start()
        assert pos > idx, "Notification content order incorrect"
        idx = pos

    print("✅ 释放超时预约并提醒下一位用户，且通知格式与顺序验证通过")


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

    assert "user2" in sent_payload["json"]["message"]["body"][1]["atuserids"], "Missing user ID in early notification"
    assert "释放" in sent_payload["json"]["message"]["body"][0]["content"], (
        "Missing release prompt in early notification"
    )
    alert_dur = format_duration(bot.config.get_val("TIME_ALERT"))
    assert alert_dur in sent_payload["json"]["message"]["body"][0]["content"], (
        "Missing alert time in early notification"
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

    assert sent_payload == {}, "Should not send notification before early alert time"


def test_io_log_to_file(bot):
    """Test io log to file."""
    log_to_file("user1", "lock", "test", 3600, config=bot.config)
    data_dir = bot.config.get_val("DATA_DIR")
    log_file = os.path.join(data_dir, bot.config.get_val("BOT_ID"), "bot.log")
    assert os.path.exists(log_file), "Log file not created"
    with open(log_file, encoding="utf-8") as f:
        lines = f.readlines()
    assert any("user1" in line and "lock" in line and "test" in line for line in lines), "Log file content incorrect"
