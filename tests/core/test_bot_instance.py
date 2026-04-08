"""
test_bot_instance.py - Multi-instance isolation tests.

Verify BotInstance creates independent Bot instances that don't interfere.
"""

from lockbot.core.bot_instance import BotInstance
from lockbot.core.config import Config


class TestBotInstanceIsolation:
    def test_node_bot_config_isolation(self):
        b1 = BotInstance("NODE", {"BOT_NAME": "a", "CLUSTER_CONFIGS": {"n1": "m1"}}, auto_start=False)
        b2 = BotInstance("NODE", {"BOT_NAME": "b", "CLUSTER_CONFIGS": {"n2": "m2"}}, auto_start=False)

        assert b1.config.get_val("BOT_NAME") == "a"
        assert b2.config.get_val("BOT_NAME") == "b"

        b1.config.set_val("PORT", 9999)
        assert b2.config.get_val("PORT") == 5000

    def test_node_bot_state_isolation(self):
        b1 = BotInstance("NODE", {"BOT_NAME": "a", "CLUSTER_CONFIGS": {"n1": "m1"}}, auto_start=False)
        b2 = BotInstance("NODE", {"BOT_NAME": "b", "CLUSTER_CONFIGS": {"n2": "m2"}}, auto_start=False)

        assert "n1" in b1.state.bot_state
        assert "n2" in b2.state.bot_state
        assert "n2" not in b1.state.bot_state
        assert "n1" not in b2.state.bot_state

        b1.state.bot_state["n1"]["status"] = "exclusive"
        assert b2.state.bot_state["n2"]["status"] == "idle"

    def test_device_bot_state_isolation(self):
        b1 = BotInstance(
            "DEVICE",
            {"BOT_NAME": "d1", "CLUSTER_CONFIGS": {"h1": ["gpu"] * 4}},
            auto_start=False,
        )
        b2 = BotInstance(
            "DEVICE",
            {"BOT_NAME": "d2", "CLUSTER_CONFIGS": {"h2": ["gpu"] * 8}},
            auto_start=False,
        )

        assert len(b1.state.bot_state["h1"]) == 4
        assert len(b2.state.bot_state["h2"]) == 8

        b1.state.bot_state["h1"][0]["status"] = "exclusive"
        assert b2.state.bot_state["h2"][0]["status"] == "idle"

    def test_queue_bot_state_isolation(self):
        b1 = BotInstance("QUEUE", {"BOT_NAME": "q1", "CLUSTER_CONFIGS": {"a": "a"}}, auto_start=False)
        b2 = BotInstance("QUEUE", {"BOT_NAME": "q2", "CLUSTER_CONFIGS": {"b": "b"}}, auto_start=False)

        assert "a" in b1.state.bot_state
        assert "b" in b2.state.bot_state

        b1.state.bot_state["a"]["booking_list"].append({"user_id": "u1"})
        assert len(b2.state.bot_state["b"]["booking_list"]) == 0

    def test_lock_isolation(self):
        b1 = BotInstance("NODE", {"BOT_NAME": "a", "CLUSTER_CONFIGS": {"n1": "m1"}}, auto_start=False)
        b2 = BotInstance("NODE", {"BOT_NAME": "b", "CLUSTER_CONFIGS": {"n2": "m2"}}, auto_start=False)

        assert b1.bot._lock is not b2.bot._lock

    def test_bot_instance_does_not_affect_global_config(self):
        old_name = Config.get("BOT_NAME")
        BotInstance("NODE", {"BOT_NAME": "isolated", "CLUSTER_CONFIGS": {"x": "x"}}, auto_start=False)
        assert Config.get("BOT_NAME") == old_name

    def test_mixed_bot_types(self):
        node = BotInstance("NODE", {"BOT_NAME": "nb", "CLUSTER_CONFIGS": {"n": "n"}}, auto_start=False)
        device = BotInstance(
            "DEVICE",
            {"BOT_NAME": "db", "CLUSTER_CONFIGS": {"h": ["p800"]}},
            auto_start=False,
        )
        queue = BotInstance("QUEUE", {"BOT_NAME": "qb", "CLUSTER_CONFIGS": {"q": "q"}}, auto_start=False)

        assert node.bot_type == "NODE"
        assert device.bot_type == "DEVICE"
        assert queue.bot_type == "QUEUE"

        assert "n" in node.state.bot_state
        assert "h" in device.state.bot_state
        assert "q" in queue.state.bot_state


class TestConfigHelp:
    def test_help_text_contains_all_keys(self):
        text = Config.help(as_text=True)
        for key in ["WEBHOOK_URL", "BOT_NAME", "BOT_TYPE", "PORT", "CLUSTER_CONFIGS"]:
            assert key in text

    def test_help_text_contains_env_markers(self):
        text = Config.help(as_text=True)
        assert "[ENV]" in text

    def test_help_structured_output(self):
        data = Config.help(as_text=False)
        assert isinstance(data, list)
        assert len(data) > 0
        assert all("key" in item and "description" in item for item in data)
