"""Exercise the current privacy/history baseline through a local Feishu adapter."""

# ruff: noqa: F811
import json

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_auth import service
from lockbot.core.platforms.feishu import FeishuAdapter, FeishuClient

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture
def notification_queue():
    # App lifespan startup is patched in backend tests; isolate the real queue state
    # from the previous TestClient's shutdown without starting network workers.
    from lockbot.backend.app.resource_api.notifications import notification_budget

    notification_budget.reset()
    yield
    notification_budget.reset()


def test_feishu_api_notifications_and_history_keep_identity_and_task_privacy(
    client, api, db_session, monkeypatch, notification_queue
):
    bot_id, bot = api["bot_id"], api["bot"]
    stored = db_session.get(Bot, bot_id)
    stored.platform, stored.group_id, stored.api_key = "Feishu", "oc_demo", "lbk_test_release_integration"
    db_session.commit()
    platform = FeishuClient("cli_demo", "test-only-secret")
    bot.config.set_val("PLATFORM", "feishu")
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["oc_demo"])
    bot.config.set_val("_FEISHU_CLIENT", platform)
    bot.adapter = FeishuAdapter(bot.config)
    monkeypatch.setattr(
        "lockbot.backend.app.feishu.service.bot_application_config", lambda *args: {"_FEISHU_CLIENT": platform}
    )
    monkeypatch.setattr("lockbot.backend.app.resource_api.operator.bot_manager", api["manager"])
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", api["manager"])
    sent = []
    monkeypatch.setattr(FeishuClient, "send", lambda self, payload: sent.append(payload) or [(200, "accepted")])
    # No contact request: model the cold-cache lookup result before executing the command.
    monkeypatch.setattr(
        FeishuClient,
        "warm_user_name",
        lambda self, uid: self._names.update({uid: ("Example user", 100000000000)}),
    )

    def enroll(actor):
        issued = client.post("/api/resource-auth/bindings", json={"label": "test client"}).json()
        service.handle_chat_binding(
            platform="feishu", external_user_id=actor, bot_id=bot_id, action="bind", argument=issued["bind_code"]
        )
        return {"Authorization": "Bearer " + issued["credential"]}

    owner, observer = enroll("ou_example"), enroll("ou_observer")
    response = client.post(
        f"/api/bots/{bot_id}/commands",
        headers=owner,
        json={
            "command": "lock n1 30m",
            "task_category": "unit_testing",
            "purpose": "private-test-note",
            "agent_label": "private-test-client",
        },
    )
    assert response.status_code == 200, response.text
    assert sent and sent[0]["receive_id"] == "oc_demo"
    assert "Example user" in sent[0]["content"]
    assert "private-test" not in json.dumps(sent)
    own = client.get(f"/api/bots/{bot_id}/resources", headers=owner)
    assert own.status_code == 200 and "private-test-note" in own.text
    other = client.get(f"/api/bots/{bot_id}/resources", headers=observer)
    assert other.status_code == 200 and "private-test" not in other.text
    holder = other.json()["resources"][0]["holders"][0]
    assert holder["external_user_id"] == "ou_example" and holder["display_name"] == "Example user"
    assert "unit_testing" in other.text
    history = client.get(
        f"/api/bots/{bot_id}/op/history", headers={"Authorization": "Bearer lbk_test_release_integration"}
    )
    assert history.status_code == 200 and "private-test" not in history.text
    assert history.json()["items"][0]["actor"]["external_user_id"] == "ou_example"
    assert history.json()["visibility"] == "public"
    assert len(history.json()["items"]) == 1  # Notifications are not a second command execution.
    state = client.get(f"/api/bots/{bot_id}/state?include_usage=true", headers=api["auth_header"])
    assert state.status_code == 200 and "private-test" not in state.text
    assert "unit_testing" in state.text
