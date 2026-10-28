# ruff: noqa: F811
import pytest
from lockbot.backend.app.bots.router import _default_state_for
from lockbot.core.command_context import command_source
from lockbot.core.handler import execute_text_command

from backend.test_resource_api_resources import _clear_cache, api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_origin_is_server_recorded_without_task_metadata(client, api):
    target = "n1 dev0" if api["bot"].config.get_val("BOT_TYPE") == "DEVICE" else "n1"
    path = f"/api/bots/{api['bot_id']}/commands"
    response = client.post(path, headers=api["headers"], json={"command": f"lock {target} 1h"})
    assert response.status_code == 200
    state = api["bot"].state_snapshot()[1]["n1"]
    state = state[0] if isinstance(state, list) else state
    assert state["current_users"][0]["source"] == "api"
    # A later human renewal doesn't relabel who originally acquired the holding.
    execute_text_command("alice", f"lock {target} 1m", api["bot"])
    state = api["bot"].state_snapshot()[1]["n1"]
    state = state[0] if isinstance(state, list) else state
    assert state["current_users"][0]["source"] == "api"
    execute_text_command("alice", f"free {target}", api["bot"])
    execute_text_command("alice", f"lock {target} 1m", api["bot"])
    state = api["bot"].state_snapshot()[1]["n1"]
    state = state[0] if isinstance(state, list) else state
    assert state["current_users"][0]["source"] == "chat"
    assert command_source.get() == "unknown"
    assert client.post(path, headers=api["headers"], json={"command": "query", "source": "chat"}).status_code == 422


def test_default_state_helper_does_not_require_view_context():
    assert _default_state_for({"n1": "model"}, "NODE")["n1"]["status"] == "idle"
    assert _default_state_for({"n1": ["gpu"]}, "DEVICE")["n1"][0]["dev_id"] == 0
