"""Optional membership events never authorize a route or mutate allocator state."""

# ruff: noqa: F811

import copy
import json
import time
from types import SimpleNamespace

import pytest
from lockbot.backend.app.bots.models import Bot, BotLog
from lockbot.backend.app.feishu import membership, service, unbound
from lockbot.backend.app.feishu.models import (
    FeishuApplication,
    FeishuGroupMembership,
    FeishuGroupRoute,
    FeishuMembershipEvent,
    FeishuMembershipPolicy,
    FeishuReceivedMessage,
)
from lockbot.core.platforms.feishu import FeishuClient
from sqlalchemy.orm import sessionmaker

from backend.test_feishu_ingress import setup  # noqa: F401
from backend.test_feishu_unbound import Deferred


def event(*, kind="added", group=0, event_id="event_1", offset=0):
    return {
        "schema": "2.0",
        "header": {
            "app_id": "cli_demo",
            "tenant_key": "tenant",
            "token": "SECRET_VERIFY",
            "event_id": event_id,
            "event_type": f"im.chat.member.bot.{kind}_v1",
            "create_time": str(int(time.time() * 1000) + offset),
        },
        "event": {
            "chat_id": f"oc_group{group}",
            "external": False,
            "operator_tenant_key": "tenant",
            "operator_id": {"open_id": "ou_private_operator"},
            "name": "private_group_name",
        },
    }


def enable_bot(db, bot):
    row = db.get(Bot, bot.config.get_val("BOT_ID"))
    row.config_overrides = json.dumps({"FEISHU_MEMBERSHIP_NOTICES": True})
    db.commit()
    return row


def submit(client, db, app_id, transport, value):
    if transport == "http":
        response = client.post(f"/api/feishu/events/{app_id}", json=value)
        assert response.status_code == 200, response.text
        return response.json()
    app = db.get(FeishuApplication, app_id)
    app.transport = "websocket"
    db.commit()
    return service.accept_sdk_event(db, app, value)


@pytest.fixture
def hints(setup, db_session, monkeypatch):
    sent, executor = [], Deferred()
    worker = unbound.UnboundHints(executor=executor, clock=lambda: 100.0)
    monkeypatch.setattr(unbound, "unbound_hints", worker)
    monkeypatch.setattr(FeishuClient, "token", lambda *_: "fixture")
    monkeypatch.setattr(FeishuClient, "send", lambda _self, payload: sent.append(payload) or [(200, "accepted")])
    return SimpleNamespace(worker=worker, executor=executor, sent=sent)


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_all_membership_behavior_is_off_by_default(client, setup, db_session, transport, hints):
    app_id, bots, commands = setup
    for group in (0, 99):
        assert submit(client, db_session, app_id, transport, event(group=group)) == {"accepted": False}
    assert not commands and not hints.executor.jobs
    assert db_session.query(FeishuMembershipEvent).count() == db_session.query(FeishuGroupMembership).count() == 0
    assert db_session.query(FeishuMembershipPolicy).count() == 0


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_removal_is_scoped_deduplicated_and_preserves_resources(client, setup, db_session, transport, monkeypatch):
    app_id, bots, commands = setup
    bot = enable_bot(db_session, bots[0])
    state = copy.deepcopy(bots[0].state.bot_state)
    received = event(kind="deleted")
    assert submit(client, db_session, app_id, transport, received) == {"accepted": True}
    assert submit(client, db_session, app_id, transport, received) == {"accepted": True, "duplicate": True}
    assert db_session.query(FeishuGroupRoute).count() == 2
    assert state == bots[0].state.bot_state
    assert not commands
    assert db_session.query(FeishuReceivedMessage).count() == 0
    assert db_session.query(BotLog).filter_by(bot_id=bot.id, level="WARNING").count() == 1
    assert db_session.query(BotLog).filter_by(bot_id=bots[1].config.get_val("BOT_ID"), level="WARNING").count() == 0
    app = db_session.get(FeishuApplication, app_id)
    assert membership.membership_status(db_session, app, bot, bot.group_id)["status"] == "removed"
    other = db_session.get(Bot, bots[1].config.get_val("BOT_ID"))
    assert membership.membership_status(db_session, app, other, bot.group_id) is None
    row = db_session.get(FeishuGroupMembership, (app_id, "oc_group0"))
    assert "private" not in repr(row.__dict__)


def test_out_of_order_events_and_same_millisecond_removal_win(client, setup, db_session):
    app_id, bots, _ = setup
    bot = enable_bot(db_session, bots[0])
    removed = event(kind="deleted", event_id="new_removed", offset=100)
    assert submit(client, db_session, app_id, "http", removed) == {"accepted": True}
    for value in (event(event_id="older_join", offset=-100), event(event_id="same_join")):
        value["header"]["create_time"] = str(
            int(removed["header"]["create_time"]) - (100 if value["header"]["event_id"] == "older_join" else 0)
        )
        assert submit(client, db_session, app_id, "http", value)["ignored"] == "older_observation"
    app = db_session.get(FeishuApplication, app_id)
    assert membership.membership_status(db_session, app, bot, bot.group_id)["status"] == "removed"
    assert submit(client, db_session, app_id, "http", event(event_id="later_join", offset=300)) == {"accepted": True}
    assert membership.membership_status(db_session, app, bot, bot.group_id)["status"] == "present"


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_opted_in_unbound_greeting_uses_only_current_group_and_site_url(client, setup, db_session, transport, hints):
    from lockbot.backend.app.settings.models import SiteSetting

    app_id, _bots, commands = setup
    membership.set_greeting_policy(db_session, app_id, True)
    db_session.add(SiteSetting(key="platform_url", value="https://lockbot.example.test"))
    db_session.commit()
    payload = event(group=99)
    assert submit(client, db_session, app_id, transport, payload) == {"accepted": True}
    assert submit(client, db_session, app_id, transport, payload)["duplicate"]
    assert len(hints.executor.jobs) == 1
    hints.executor.run()
    assert len(hints.sent) == 1
    text = json.loads(hints.sent[0]["content"])["text"]
    assert hints.sent[0]["receive_id"] == "oc_group99"
    assert "https://lockbot.example.test" in text and "oc_group99" in text
    assert "private" not in text and "node_a" not in text
    assert db_session.query(FeishuGroupRoute).count() == 2 and not commands


@pytest.mark.parametrize("change", ["disable", "removed", "route", "rotate"])
def test_queued_greeting_rechecks_policy_membership_route_and_credentials(client, setup, db_session, hints, change):
    from lockbot.backend.app.bots.encryption import encrypt

    app_id, bots, _ = setup
    membership.set_greeting_policy(db_session, app_id, True)
    db_session.commit()
    assert submit(client, db_session, app_id, "http", event(group=99)) == {"accepted": True}
    assert len(hints.executor.jobs) == 1
    if change == "disable":
        membership.set_greeting_policy(db_session, app_id, False)
    elif change == "removed":
        assert submit(
            client, db_session, app_id, "http", event(group=99, kind="deleted", event_id="removed", offset=50)
        ) == {"accepted": True}
    elif change == "route":
        route = db_session.query(FeishuGroupRoute).filter_by(bot_id=bots[1].config.get_val("BOT_ID")).one()
        route.chat_id = "oc_group99"
    else:
        db_session.get(FeishuApplication, app_id).app_secret = encrypt("new-secret")
    db_session.commit()
    hints.executor.run()
    assert not hints.sent


def test_sdk_first_join_can_greet_but_cannot_pin_unknown_tenant(client, setup, db_session, hints):
    app_id, _bots, _commands = setup
    app = db_session.get(FeishuApplication, app_id)
    app.transport, app.tenant_key = "websocket", ""
    membership.set_greeting_policy(db_session, app_id, True)
    db_session.commit()
    assert service.accept_sdk_event(db_session, app, event(group=99)) == {"accepted": True}
    hints.executor.run()
    assert len(hints.sent) == 1
    db_session.refresh(app)
    assert app.tenant_key == ""


@pytest.mark.parametrize(
    "case", ["wrong_app", "wrong_tenant", "operator_tenant", "stale", "future", "bad_chat", "bad_event_id"]
)
def test_invalid_identity_or_event_cannot_change_observation(client, setup, db_session, case):
    app_id, bots, _ = setup
    enable_bot(db_session, bots[0])
    value = event()
    if case == "wrong_app":
        value["header"]["app_id"] = "cli_other"
    if case == "wrong_tenant":
        value["header"]["tenant_key"] = "other"
    if case == "operator_tenant":
        value["event"]["operator_tenant_key"] = "other"
    if case == "stale":
        value["header"]["create_time"] = "1000"
    if case == "future":
        value["header"]["create_time"] = str(int(time.time() * 1000) + 700000)
    if case == "bad_chat":
        value["event"]["chat_id"] = "oc_wrong/path"
    if case == "bad_event_id":
        value["header"]["event_id"] = "event/invalid"
    response = client.post(f"/api/feishu/events/{app_id}", json=value)
    assert response.status_code == 401
    assert db_session.query(FeishuMembershipEvent).count() == db_session.query(FeishuGroupMembership).count() == 0


@pytest.mark.parametrize("case", ["external", "missing_external", "user_event"])
def test_non_internal_bot_events_are_ignored(client, setup, db_session, case):
    app_id, bots, _ = setup
    enable_bot(db_session, bots[0])
    value = event()
    if case == "external":
        value["event"]["external"] = True
    if case == "missing_external":
        value["event"].pop("external")
    if case == "user_event":
        value["header"]["event_type"] = "im.chat.member.user.added_v1"
    assert submit(client, db_session, app_id, "http", value) == {"accepted": False}
    assert db_session.query(FeishuGroupMembership).count() == 0


def test_disabled_or_reconfigured_scope_hides_old_observations(client, setup, db_session, auth_header, monkeypatch):
    app_id, bots, _ = setup
    bot = enable_bot(db_session, bots[0])
    monkeypatch.setattr(FeishuClient, "group_name", lambda *_: {"name": "fixture", "status": "ok"})
    assert submit(client, db_session, app_id, "http", event(kind="deleted")) == {"accepted": True}
    path = f"/api/feishu/bots/{bot.id}/group"
    assert client.get(path, headers=auth_header).json()["membership"]["status"] == "removed"
    bot.identity_revision += 1
    db_session.commit()
    assert client.get(path, headers=auth_header).json().get("membership") is None
    bot.identity_revision -= 1
    bot.config_overrides = "{}"
    db_session.commit()
    assert client.get(path, headers=auth_header).json().get("membership") is None


def test_policy_management_requires_super_admin_and_default_legacy_rows_are_off(client, setup, db_session, auth_header):
    from lockbot.backend.app.auth.models import User

    app_id, _bots, _commands = setup
    rows = client.get("/api/feishu/applications", headers=auth_header).json()
    assert next(row for row in rows if row["id"] == app_id)["unbound_greeting_enabled"] is False
    assert (
        client.patch(
            f"/api/feishu/applications/{app_id}",
            headers=auth_header,
            json={"name": "Test app", "unbound_greeting_enabled": True},
        ).status_code
        == 200
    )
    assert membership.greeting_enabled(db_session, app_id)
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "admin"
    db_session.commit()
    assert (
        client.patch(
            f"/api/feishu/applications/{app_id}",
            headers=auth_header,
            json={"name": "Test app", "unbound_greeting_enabled": False},
        ).status_code
        == 403
    )
    db_session.expire_all()
    assert membership.greeting_enabled(db_session, app_id)


def test_replay_claim_survives_a_new_session(client, setup, db_session):
    app_id, bots, _ = setup
    enable_bot(db_session, bots[0])
    value = event(kind="deleted")
    assert submit(client, db_session, app_id, "http", value) == {"accepted": True}
    factory = sessionmaker(bind=db_session.get_bind())
    with factory() as new:
        app = new.get(FeishuApplication, app_id)
        assert membership.accept_membership(new, app, value) == {"accepted": True, "duplicate": True}
    assert db_session.query(BotLog).filter_by(level="WARNING").count() == 1


def test_removal_status_is_not_visible_to_another_bots_owner(client, setup, db_session, auth_header, monkeypatch):
    from lockbot.backend.app.auth.models import User

    app_id, bots, _ = setup
    own = enable_bot(db_session, bots[0])
    foreign = enable_bot(db_session, bots[1])
    user = db_session.query(User).filter_by(username="testuser").one()
    foreign.user_id = user.id + 100
    user.role = "user"
    db_session.commit()
    assert submit(client, db_session, app_id, "http", event(kind="deleted")) == {"accepted": True}
    assert submit(client, db_session, app_id, "http", event(group=1, kind="deleted", event_id="foreign")) == {
        "accepted": True
    }
    monkeypatch.setattr(FeishuClient, "group_name", lambda *_: {"name": "fixture", "status": "ok"})
    assert (
        client.get(f"/api/feishu/bots/{own.id}/group", headers=auth_header).json()["membership"]["status"] == "removed"
    )
    response = client.get(f"/api/feishu/bots/{foreign.id}/group", headers=auth_header)
    assert response.status_code == 404 and "membership" not in response.json()


def test_encrypted_http_verification_precedes_membership_processing(client, setup, db_session):
    from core.test_feishu_transport import encrypted
    from lockbot.backend.app.bots.encryption import encrypt

    app_id, bots, _ = setup
    enable_bot(db_session, bots[0])
    app = db_session.get(FeishuApplication, app_id)
    app.encrypt_key = encrypt("encrypt")
    db_session.commit()
    value = event(kind="deleted")
    body, headers = encrypted(value, now=int(time.time()))
    path = f"/api/feishu/events/{app_id}"
    forged = dict(headers, **{"x-lark-signature": "invalid"})
    assert client.post(path, content=body, headers=forged).status_code == 401
    assert client.post(path, json=value).status_code == 401
    assert db_session.query(FeishuGroupMembership).count() == 0
    assert client.post(path, content=body, headers=headers).json() == {"accepted": True}


def test_application_delete_clears_membership_metadata(client, setup, db_session, auth_header):
    app_id, _bots, _ = setup
    membership.set_greeting_policy(db_session, app_id, True)
    db_session.commit()
    assert submit(client, db_session, app_id, "http", event(kind="deleted", group=99)) == {"accepted": True}
    db_session.query(FeishuGroupRoute).filter_by(application_id=app_id).delete()
    db_session.commit()
    assert client.delete(f"/api/feishu/applications/{app_id}", headers=auth_header).status_code == 204
    for model in (FeishuMembershipPolicy, FeishuMembershipEvent, FeishuGroupMembership):
        assert db_session.query(model).filter_by(application_id=app_id).count() == 0


def test_panel_opt_in_can_invalidate_without_enabling_removal_notices(client, setup, db_session, monkeypatch):
    import sys
    from types import ModuleType

    app_id, bots, _ = setup
    bot = db_session.get(Bot, bots[0].config.get_val("BOT_ID"))
    bot.config_overrides = json.dumps({"FEISHU_QUERY_PANEL": True})
    db_session.commit()
    calls = []
    module = ModuleType("lockbot.backend.app.feishu.query_panel")
    module.invalidate_query_panel = lambda db, bot_id: calls.append((db, bot_id))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    value = event(kind="deleted")
    assert submit(client, db_session, app_id, "http", value) == {"accepted": True}
    assert [identifier for _db, identifier in calls] == [bot.id]
    assert db_session.query(BotLog).filter_by(level="WARNING").count() == 0
    app = db_session.get(FeishuApplication, app_id)
    assert membership.membership_status(db_session, app, bot, bot.group_id) is None
    assert submit(client, db_session, app_id, "http", value)["duplicate"]
    assert len(calls) == 1
