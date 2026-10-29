# ruff: noqa: F811
import pytest

from backend.test_resource_api_resources import _clear_cache, api  # noqa: F401


def post(client, api, command, **meta):
    return client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command, **meta})


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_admin_state_keeps_metadata_outside_editable_state(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", api["manager"])
    target = "n1 dev0" if api["bot"].config.get_val("BOT_TYPE") == "DEVICE" else "n1"
    assert (
        post(
            client, api, f"lock {target} 1h", agent_label="dev-agent", purpose="<script>Untrusted task</script>"
        ).status_code
        == 200
    )
    path = f"/api/bots/{api['bot_id']}/state"
    raw = client.get(path, headers=api["auth_header"])
    view = client.get(path + "?include_usage=true", headers=api["auth_header"])
    assert raw.status_code == view.status_code == 200
    assert view.json()["state"] == raw.json()
    assert "Untrusted task" not in raw.text
    assert "agent_label" not in view.json()["usage"][0]
    assert "Untrusted task" not in view.text
    assert client.get(path + "?include_usage=true", headers=api["headers"]).status_code == 401
    assert post(client, api, f"free {target}").status_code == 200
    assert client.get(path + "?include_usage=true", headers=api["auth_header"]).json()["usage"] == []


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_admin_state_shows_booker_task_and_hides_task_after_native_release(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", api["manager"])
    assert post(client, api, "book n1 10m", agent_label="queue-agent", purpose="Next regression").status_code == 200
    path = f"/api/bots/{api['bot_id']}/state?include_usage=true"
    assert "purpose" not in client.get(path, headers=api["auth_header"]).json()["usage"][0]
    api["bot"].unlock("alice", "free n1")
    assert client.get(path, headers=api["auth_header"]).json()["usage"] == []
