import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu import router as ingress
from lockbot.backend.app.feishu.models import FeishuReceivedMessage
from lockbot.core.bot_instance import BotInstance
from lockbot.core.platforms.feishu import FeishuClient


@pytest.fixture
def setup(client, auth_header, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(FeishuClient, "bot_identity", lambda self: "ou_bot")
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    created = client.post(
        "/api/feishu/applications",
        headers=auth_header,
        json={
            "name": "Test app",
            "app_id": "cli_demo",
            "tenant_key": "tenant",
            "bot_open_id": "ou_bot",
            "app_secret": "SECRET_APP",
            "verification_token": "SECRET_VERIFY",
            "transport": "http",
        },
    )
    assert created.status_code == 201, created.text
    assert "SECRET" not in created.text
    application_id = created.json()["id"]
    bots = []
    for index in range(2):
        response = client.post(
            "/api/bots",
            headers=auth_header,
            json={
                "name": f"feishu-{index}",
                "bot_type": "QUEUE",
                "platform": "Feishu",
                "group_id": f"oc_group{index}",
                "feishu_application_id": application_id,
                "webhook_url": "",
                "cluster_configs": ["node_a"],
            },
        )
        assert response.status_code == 201, response.text
        bot_id = response.json()["id"]
        # Creation cannot start an un-routed bot; route enrollment requires stopped status.
        row = db_session.get(Bot, bot_id)
        row.status = "stopped"
        db_session.commit()
        instance = BotInstance(
            "QUEUE",
            {
                "BOT_ID": bot_id,
                "BOT_NAME": row.name,
                "PLATFORM": "feishu",
                "CLUSTER_CONFIGS": ["node_a"],
                "DATA_DIR": str(tmp_path / str(bot_id)),
                "_IDENTITY_REVISION": 0,
                "_AUTHORIZED_GROUP_IDS": [f"oc_group{index}"],
                "_FEISHU_CLIENT": FeishuClient("cli_demo", "secret"),
            },
        )
        bots.append(instance.bot)
    calls = []

    class Executor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(ingress, "_executor", Executor())

    @contextmanager
    def admit(bot_id):
        yield SimpleNamespace(bot=bots[[b.config.get_val("BOT_ID") for b in bots].index(bot_id)])

    monkeypatch.setattr(ingress.bot_manager, "admit", admit)
    monkeypatch.setattr(FeishuClient, "send", lambda *args: [(200, "accepted")])
    monkeypatch.setattr(FeishuClient, "warm_user_name", lambda *args: None)
    yield application_id, bots, calls
    # Execute any accepted work so bounded capacity is released after every test.
    while calls:
        fn, args = calls.pop(0)
        fn(*args)


def message(group=0, message_id="om_first", command="lock node_a 5m"):
    return {
        "header": {
            "app_id": "cli_demo",
            "tenant_key": "tenant",
            "token": "SECRET_VERIFY",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "tenant_key": "tenant", "sender_id": {"open_id": "ou_alice"}},
            "message": {
                "message_id": message_id,
                "create_time": str(int(time.time() * 1000)),
                "chat_type": "group",
                "chat_id": f"oc_group{group}",
                "message_type": "text",
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
                "content": json.dumps({"text": "@_user_1 " + command}),
            },
        },
    }


def test_group_isolation_and_durable_duplicate_claim(client, setup, db_session):
    application_id, bots, calls = setup
    url = f"/api/feishu/events/{application_id}"
    assert client.post(url, json=message()).json() == {"accepted": True}
    assert client.post(url, json=message()).json() == {"accepted": True, "duplicate": True}
    assert len(calls) == 1
    fn, args = calls.pop()
    fn(*args)
    assert bots[0].state.bot_state["node_a"]["current_users"][0]["user_id"] == "ou_alice"
    assert bots[1].state.bot_state["node_a"]["current_users"] == []
    db_session.expire_all()
    assert db_session.get(FeishuReceivedMessage, (application_id, "om_first")).state == "completed"


def test_unknown_group_wrong_app_and_stale_event_do_not_execute(client, setup):
    application_id, bots, calls = setup
    url = f"/api/feishu/events/{application_id}"
    assert client.post(url, json=message(group=99)).json() == {"accepted": False}
    wrong = message()
    wrong["header"]["app_id"] = "cli_other"
    assert client.post(url, json=wrong).status_code == 401
    stale = message()
    stale["event"]["message"]["create_time"] = "1000"
    assert client.post(url, json=stale).status_code == 401
    assert calls == []


def test_queued_work_cannot_use_reconfigured_identity(client, setup, db_session):
    application_id, bots, calls = setup
    assert client.post(f"/api/feishu/events/{application_id}", json=message()).status_code == 200
    bot = db_session.get(Bot, bots[0].config.get_val("BOT_ID"))
    bot.identity_revision = 1
    db_session.commit()
    fn, args = calls.pop()
    fn(*args)
    assert bots[0].state.bot_state["node_a"]["current_users"] == []
    db_session.expire_all()
    assert db_session.get(FeishuReceivedMessage, (application_id, "om_first")).state == "identity_changed"


def test_binding_code_is_not_stored_in_received_message(client, setup, db_session):
    application_id, bots, calls = setup
    assert (
        client.post(f"/api/feishu/events/{application_id}", json=message(command="bind PRIVATE_CODE")).status_code
        == 200
    )
    row = db_session.get(FeishuReceivedMessage, (application_id, "om_first"))
    assert "PRIVATE_CODE" not in str(row.__dict__)


def test_app_configuration_requires_super_admin_and_never_returns_secrets(client, auth_header):
    assert client.get("/api/feishu/applications", headers=auth_header).status_code == 403
    assert client.post("/api/feishu/applications", headers=auth_header, json={}).status_code == 403


def test_delivery_failure_does_not_reexecute_lock_on_retry(client, setup, db_session, monkeypatch):
    application_id, bots, calls = setup
    url = f"/api/feishu/events/{application_id}"
    assert client.post(url, json=message()).status_code == 200
    from lockbot.core.platforms.feishu import FeishuAPIError

    def reject(*args):
        raise FeishuAPIError("delivery uncertain")

    monkeypatch.setattr(FeishuClient, "send", reject)
    fn, args = calls.pop()
    fn(*args)
    first = bots[0].state.bot_state["node_a"]["current_users"][0].copy()
    assert client.post(url, json=message()).json()["duplicate"] is True
    assert calls == []
    assert bots[0].state.bot_state["node_a"]["current_users"][0] == first
    db_session.expire_all()
    assert db_session.get(FeishuReceivedMessage, (application_id, "om_first")).state == "delivery_uncertain"


def test_sdk_pins_tenant_only_after_authorized_group_and_exact_identity(setup, db_session):
    from lockbot.backend.app.feishu.models import FeishuApplication
    from lockbot.backend.app.feishu.service import accept_sdk_event
    from lockbot.core.platforms.feishu_events import InvalidFeishuEvent

    app = db_session.get(FeishuApplication, setup[0])
    app.transport, app.tenant_key = "websocket", ""
    db_session.commit()
    assert accept_sdk_event(db_session, app, message(group=99)) == {"accepted": False}
    assert app.tenant_key == ""
    invalid = message()
    invalid["header"]["app_id"] = "cli_other"
    with pytest.raises(InvalidFeishuEvent):
        accept_sdk_event(db_session, app, invalid)
    assert app.tenant_key == ""
    assert accept_sdk_event(db_session, app, message()) == {"accepted": True}
    assert app.tenant_key == "tenant"
    invalid = message(message_id="om_other")
    invalid["header"]["tenant_key"] = "other"
    invalid["event"]["sender"]["tenant_key"] = "other"
    with pytest.raises(InvalidFeishuEvent):
        accept_sdk_event(db_session, app, invalid)


def test_chat_budget_is_per_verified_actor_and_application():
    from lockbot.backend.app.feishu.traffic import IngressBudget

    budget = IngressBudget(app_limit=3, actor_limit=2)
    assert budget.claim(1, "alice", now=1)
    assert budget.claim(1, "alice", now=2)
    assert not budget.claim(1, "alice", now=3)
    assert budget.claim(1, "bob", now=3)
    assert not budget.claim(1, "charlie", now=4)
    assert budget.claim(2, "alice", now=4)
    assert budget.claim(1, "alice", now=63)
