"""Published response compatibility and new-bot policy defaults."""
# ruff: noqa: F811

import json

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.schemas import BotUpdate

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("kind", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"])
def test_new_bot_defaults_are_explicit_but_old_bots_are_not_changed(client, auth_header, db_session, kind):
    config = {"n1": ["gpu"]} if "DEVICE" in kind else ["n1"]
    response = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "policy",
            "bot_type": kind,
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": config,
        },
    )
    assert response.status_code == 201, response.text
    bot = db_session.get(Bot, response.json()["id"])
    assert json.loads(bot.config_overrides).get("FORBID_RELOCK", False) is (kind in {"QUEUE", "QUEUE_DEVICE"})
    # An existing record lacking new fields retains its old renewal behavior on load.
    bot.config_overrides = "{}"
    db_session.commit()
    from lockbot.backend.app.bots.router import _build_config_dict

    assert _build_config_dict(bot).get("FORBID_RELOCK", False) is False


@pytest.mark.parametrize("value", [0, -2, True, 1.5, "2"])
def test_invalid_resource_count_is_rejected(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BotUpdate(config_overrides={"MAX_RESOURCES_PER_USER": value})


def test_policy_exposed_in_existing_resource_response(client, api):
    bot = api["bot"]
    bot.config.set_val("FORBID_RELOCK", True)
    bot.config.set_val("MAX_RESOURCES_PER_USER", 1)
    response = client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
    assert response.status_code == 200
    policy = response.json()["resource_policy"]
    assert policy == {
        "forbid_relock": True,
        "max_resources_per_user": 1,
        "resource_unit": "node",
        "claimed_resource_count": 0,
    }
    assert "_claimed_resources" not in str(response.json())


def test_explicit_new_bot_renewal_override_is_respected(client, auth_header, db_session):
    response = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "legacy-queue",
            "bot_type": "QUEUE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["n1"],
            "config_overrides": {"FORBID_RELOCK": False},
        },
    )
    assert response.status_code == 201
    assert json.loads(db_session.get(Bot, response.json()["id"]).config_overrides)["FORBID_RELOCK"] is False


def test_http_agent_cannot_bypass_existing_chat_allocation(client, api):
    bot = api["bot"]
    bot.config.set_val("FORBID_RELOCK", True)
    bot.config.set_val("MAX_RESOURCES_PER_USER", 1)
    bot.lock("alice", "lock n1 10m")
    before = bot.state_snapshot()[1]
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": "lock n1 10m", "agent_label": "another-agent"},
    )
    assert response.status_code == 200
    assert "❌" in str(response.json()["reply"])
    assert bot.state_snapshot()[1] == before
