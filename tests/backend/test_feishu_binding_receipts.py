"""Credential confirmation and group-message delivery have separate outcomes."""

# ruff: noqa: F811

from types import SimpleNamespace

import pytest
import requests
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu import router as ingress
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuReceivedMessage
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.core.platforms.feishu import API_ROOT, FeishuClient

from backend.test_feishu_ingress import (  # noqa: F401
    _disable_unassociated_group_delivery,
    message,
    setup,
)

_ordinary_send = FeishuClient.send


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("delivery", ["accepted", "rejected", "timeout"])
@pytest.mark.parametrize("existing_key", [True, False])
def test_confirmed_recovery_survives_group_receipt_failure_without_replaying(
    client, setup, auth_header, db_session, session_factory, monkeypatch, caplog, transport, delivery, existing_key
):
    application_id, bots, calls = setup
    bot = bots[0]
    bot_id = bot.config.get_val("BOT_ID")
    enabled = client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)

    identity, original_key = None, None
    if existing_key:
        issued = client.post("/api/resource-auth/bindings").raise_for_status().json()
        original_key = issued["credential"]
        service.handle_chat_binding(
            platform="feishu", external_user_id="ou_alice", bot_id=bot_id, action="bind", argument=issued["bind_code"]
        )
        db_session.expire_all()
        original = db_session.query(ResourceBinding).one()
        identity = (original.public_id, original.credential_digest, original.credential_generation)
    else:
        assert db_session.query(ResourceBinding).count() == 0
    challenge = client.post("/api/resource-auth/credential-challenges").raise_for_status().json()
    assert challenge["bind_code"].startswith("REC-")
    result_url = f"/api/resource-auth/credential-challenges/{challenge['challenge_id']}"
    proof = {"Authorization": "Bearer " + challenge["proof"]}
    assert client.get(result_url, headers=proof).json() == {"status": "pending"}

    hook = ingress.bot_manager._make_binding_hook(bot_id, {"PLATFORM": "feishu", "_IDENTITY_REVISION": 0})
    dispatched, texts, command_logs, reactions = [], [], [], []

    def track_binding(**args):
        dispatched.append(args["action"])
        return hook(**args)

    bot.binding_hook = track_binding
    bot.config.set_val("FEISHU_REACTION_REPLIES", True)
    bot.config.set_val("FEISHU_QUERY_PANEL", True)
    monkeypatch.setattr(FeishuClient, "send", _ordinary_send)
    monkeypatch.setattr(FeishuClient, "token", lambda self: "fixture-feishu-token")
    monkeypatch.setattr(FeishuClient, "react", lambda self, target: reactions.append(target))
    monkeypatch.setattr("lockbot.backend.app.bots.router._write_log", lambda *args, **kwargs: command_logs.append(args))

    def post(url, **kwargs):
        assert url == API_ROOT + "/im/v1/messages"
        texts.append(kwargs["json"])
        private_detail = " ".join((challenge["bind_code"], challenge["proof"], original_key or ""))
        if delivery == "timeout":
            raise requests.Timeout("Synthetic platform failure " + private_detail)
        body = {"code": 0} if delivery == "accepted" else {"code": 99991663, "msg": private_detail}
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.post", post)
    payload = message(message_id="om_recovery_confirmation", command="bind " + challenge["bind_code"])
    if transport == "websocket":
        app = db_session.get(FeishuApplication, application_id)
        app.transport = "websocket"
        db_session.commit()

    def submit():
        if transport == "http":
            return client.post(f"/api/feishu/events/{application_id}", json=payload).json()
        from lockbot.backend.app.feishu.service import accept_sdk_event

        return accept_sdk_event(db_session, db_session.get(FeishuApplication, application_id), payload)

    assert submit() == {"accepted": True}
    assert len(calls) == 1
    fn, args = calls.pop()
    fn(*args)

    # A successful credential receipt is private confirmation, not IM delivery proof.
    recovered = client.get(result_url, headers=proof)
    assert recovered.status_code == 200
    recovered_key = recovered.json()["credential"]
    assert recovered.json() == {
        "status": "confirmed",
        "credential": original_key if existing_key else recovered_key,
        "outcome": "recovered" if existing_key else "created",
    }
    assert recovered_key.startswith("lbr_") and recovered_key != challenge["proof"]
    assert recovered.headers["cache-control"] == "no-store"
    assert client.get(result_url).status_code == 404
    db_session.expire_all()
    current = db_session.query(ResourceBinding).one()
    if existing_key:
        assert (current.public_id, current.credential_digest, current.credential_generation) == identity
    assert credentials.credential_matches(current.credential_digest, recovered_key)
    assert current.revoked_at is None
    claim = db_session.get(FeishuReceivedMessage, (application_id, "om_recovery_confirmation"))
    assert claim.state == ("completed" if delivery == "accepted" else "delivery_uncertain")
    assert dispatched == ["bind"]
    assert len(texts) == 1
    assert texts[0]["receive_id"] == "oc_group0"
    assert ("已确认找回 Key" if existing_key else "已创建个人 Key") in texts[0]["content"]
    assert reactions == []  # Binding keeps its text even when both optional reply modes are enabled.
    assert "bind [redacted]" in str(command_logs)
    observed = str(texts) + str(command_logs) + caplog.text + str(claim.__dict__)
    for secret in (challenge["bind_code"], challenge["proof"], recovered_key):
        assert secret not in observed

    # Duplicate delivery never reruns the hook or tries to repair the missing reply.
    assert submit() == {"accepted": True, "duplicate": True}
    assert calls == []
    assert dispatched == ["bind"]
    assert len(texts) == 1
    assert client.get(result_url, headers=proof).json() == recovered.json()
    assert not bot.state.bot_state["node_a"]["current_users"]
    assert db_session.get(Bot, bot_id).resource_auth_enabled is True
