"""Configured host-style resource names must work through the shared dispatcher."""

import pytest
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot


@pytest.fixture
def bot(tmp_path):
    return NodeBot(
        config_dict={
            "BOT_ID": "hyphenated-nodes",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["live-demo-node-1", "live-demo-node-2"],
            "DEFAULT_DURATION": 600,
            "MAX_LOCK_DURATION": 7200,
            "BOT_TYPE": "NODE",
            "WEBHOOK_URL": "",
        }
    )


@pytest.mark.parametrize("operation", ["lock", "slock"])
def test_dispatch_acquires_and_releases_hyphenated_resources(bot, operation):
    allowed = {"lock", "slock", "unlock"}
    execute_text_command(
        "verified-user",
        operation + " live-demo-node-1，live-demo-node-2 10m",
        bot,
        allowed_commands=allowed,
        acquisition_limit=2,
    )
    state = bot.state_snapshot()[1]
    assert set(state) == {"live-demo-node-1", "live-demo-node-2"}
    assert all([u["user_id"] for u in node["current_users"]] == ["verified-user"] for node in state.values())
    assert all(node["current_users"][0]["duration"] == 600 for node in state.values())
    execute_text_command("verified-user", "unlock live-demo-node-1,live-demo-node-2", bot, allowed_commands=allowed)
    assert all(not node["current_users"] for node in bot.state_snapshot()[1].values())


def test_unknown_hyphenated_resource_cannot_change_known_resources(bot):
    execute_text_command(
        "verified-user",
        "lock live-demo-node-1,unconfigured-node 10m",
        bot,
        allowed_commands={"lock"},
        acquisition_limit=2,
    )
    assert all(not node["current_users"] for node in bot.state_snapshot()[1].values())
