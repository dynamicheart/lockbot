"""Tests for do_opkick() API-driven operator kick across all bot types."""

import time

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot


def _node_config(tmp_path):
    return {
        "BOT_ID": "test_opkick",
        "DATA_DIR": str(tmp_path),
        "CLUSTER_CONFIGS": ["gpu01", "gpu02"],
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": -1,
        "EARLY_NOTIFY": False,
        "TIME_ALERT": 300,
        "BOT_TYPE": "NODE",
        "WEBHOOK_URL": "",
    }


def _device_config(tmp_path):
    return {
        "BOT_ID": "test_opkick_dev",
        "DATA_DIR": str(tmp_path),
        "CLUSTER_CONFIGS": {"gpu01": ["A100", "A100", "A100", "A100"]},
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": -1,
        "EARLY_NOTIFY": False,
        "TIME_ALERT": 300,
        "BOT_TYPE": "DEVICE",
        "WEBHOOK_URL": "",
    }


def _queue_config(tmp_path):
    return {
        "BOT_ID": "test_opkick_queue",
        "DATA_DIR": str(tmp_path),
        "CLUSTER_CONFIGS": ["gpu01", "gpu02"],
        "DEFAULT_DURATION": 3600,
        "MAX_LOCK_DURATION": -1,
        "EARLY_NOTIFY": False,
        "TIME_ALERT": 300,
        "BOT_TYPE": "QUEUE",
        "WEBHOOK_URL": "",
    }


# ── NodeBot opkick tests ──


class TestNodeBotOpkick:
    @pytest.fixture
    def bot(self, tmp_path):
        bot = NodeBot(config_dict=_node_config(tmp_path))
        bot.state.bot_state = {
            "gpu01": {"status": "idle", "current_users": [], "booking_list": []},
            "gpu02": {"status": "idle", "current_users": [], "booking_list": []},
        }
        return bot

    def test_kick_specific_node(self, bot):
        """Kick user from a specific node."""
        bot.lock("victim", "lock gpu01 2h")
        result = bot.do_opkick("victim", node_key="gpu01")
        assert result["ok"] is True
        assert "gpu01" in result["freed"]
        assert bot.state.bot_state["gpu01"]["status"] == "idle"
        assert bot.state.bot_state["gpu01"]["current_users"] == []

    def test_kick_all_nodes(self, bot):
        """Kick user from all nodes when no node specified."""
        bot.lock("victim", "lock gpu01 2h")
        bot.lock("victim", "lock gpu02 2h")
        result = bot.do_opkick("victim")
        assert result["ok"] is True
        assert set(result["freed"]) == {"gpu01", "gpu02"}
        assert bot.state.bot_state["gpu01"]["status"] == "idle"
        assert bot.state.bot_state["gpu02"]["status"] == "idle"

    def test_kick_nonexistent_user(self, bot):
        """Kicking a user who holds no locks raises ValueError."""
        with pytest.raises(ValueError):
            bot.do_opkick("nobody")

    def test_kick_wrong_node(self, bot):
        """Kicking user from a node they don't hold raises ValueError."""
        bot.lock("victim", "lock gpu01 2h")
        with pytest.raises(ValueError):
            bot.do_opkick("victim", node_key="gpu02")

    def test_shared_lock_only_removes_target(self, bot):
        """Shared lock: kicking one user doesn't affect others."""
        bot.slock("user_a", "slock gpu01 2h")
        bot.slock("victim", "slock gpu01 2h")
        result = bot.do_opkick("victim", node_key="gpu01")
        assert result["ok"] is True
        assert len(bot.state.bot_state["gpu01"]["current_users"]) == 1
        assert bot.state.bot_state["gpu01"]["current_users"][0]["user_id"] == "user_a"
        assert bot.state.bot_state["gpu01"]["status"] == "shared"


# ── DeviceBot opkick tests ──


class TestDeviceBotOpkick:
    @pytest.fixture
    def bot(self, tmp_path):
        return DeviceBot(config_dict=_device_config(tmp_path))

    def test_kick_specific_device(self, bot):
        """Kick user from specific device."""
        bot.lock("victim", "lock gpu01 dev 0 2h")
        result = bot.do_opkick("victim", node_key="gpu01", dev_ids=[0])
        assert result["ok"] is True
        assert "gpu01/dev0" in result["freed"]
        assert bot.state.bot_state["gpu01"][0]["current_users"] == []

    def test_kick_device_range(self, bot):
        """Kick user from multiple devices."""
        bot.lock("victim", "lock gpu01 dev 0-2 2h")
        result = bot.do_opkick("victim", node_key="gpu01", dev_ids=[0, 1, 2])
        assert result["ok"] is True
        assert len(result["freed"]) == 3
        for i in range(3):
            assert bot.state.bot_state["gpu01"][i]["current_users"] == []

    def test_kick_all_devices_on_node(self, bot):
        """Kick user from all devices when no dev_ids specified."""
        bot.lock("victim", "lock gpu01 dev 0-1 2h")
        result = bot.do_opkick("victim", node_key="gpu01")
        assert result["ok"] is True
        assert bot.state.bot_state["gpu01"][0]["current_users"] == []
        assert bot.state.bot_state["gpu01"][1]["current_users"] == []

    def test_kick_nonexistent_user(self, bot):
        """Kicking user with no device locks raises ValueError."""
        with pytest.raises(ValueError):
            bot.do_opkick("nobody", node_key="gpu01")


# ── QueueBot opkick tests ──


class TestQueueBotOpkick:
    @pytest.fixture
    def bot(self, tmp_path):
        bot = QueueBot(config_dict=_queue_config(tmp_path))
        bot.state.bot_state = {
            "gpu01": {"status": "idle", "current_users": [], "booking_list": []},
            "gpu02": {"status": "idle", "current_users": [], "booking_list": []},
        }
        return bot

    def test_kick_preserves_booking_list(self, bot):
        """Opkick on QueueBot only removes from current_users, not booking_list."""
        bot.lock("victim", "lock gpu01 2h")
        bot.state.bot_state["gpu01"]["booking_list"] = [
            {"user_id": "waiter", "start_time": int(time.time()), "duration": 3600, "is_notified": False}
        ]
        result = bot.do_opkick("victim", node_key="gpu01")
        assert result["ok"] is True
        assert bot.state.bot_state["gpu01"]["current_users"] == []
        assert len(bot.state.bot_state["gpu01"]["booking_list"]) == 1

    def test_kick_all_nodes(self, bot):
        """Kick from all nodes without specifying node_key."""
        bot.lock("victim", "lock gpu01 2h")
        bot.lock("victim", "lock gpu02 2h")
        result = bot.do_opkick("victim")
        assert result["ok"] is True
        assert set(result["freed"]) == {"gpu01", "gpu02"}

    def test_kick_nonexistent_user(self, bot):
        """Kicking user not holding locks raises ValueError."""
        with pytest.raises(ValueError):
            bot.do_opkick("nobody")
