"""First-run callback contract with synthetic Infoflow inputs and an intercepted reply."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
import requests
from lockbot.backend.app.bots.manager import BotManager
from lockbot.core.msg_utils import AESCipher
from lockbot.core.platforms.infoflow import InfoflowAdapter

GROUP_ID = 25101
ACTOR = "first-run-operator"
TOKEN = "synthetic-infoflow-token"
AES_KEY = b"k" * 32
OUTBOUND = "https://infoflow.example.invalid/webhook"


@pytest.fixture
def running_infoflow(client, auth_header, monkeypatch, session_factory):
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", manager)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    sent = []

    def capture_reply(_session, request, **kwargs):
        assert request.method == "POST" and request.url == OUTBOUND
        assert request.headers["Content-Type"] == "application/json"
        sent.append(json.loads(request.body)["message"])
        return SimpleNamespace(status_code=200, text='{"errcode":0}')

    monkeypatch.setattr(requests.Session, "send", capture_reply)
    try:
        created = client.post(
            "/api/bots",
            headers=auth_header,
            json={
                "name": "Infoflow first run",
                "bot_type": "NODE",
                "platform": "Infoflow",
                "group_id": str(GROUP_ID),
                "webhook_url": OUTBOUND,
                "token": TOKEN,
                "aes_key": base64.b64encode(AES_KEY).decode(),
                "cluster_configs": {"demo-node": "demo"},
                "config_overrides": {"LANGUAGE": "zh", "SHOW_NODE_ALIAS": False},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "running"
        bot_id = created.json()["id"]
        bot = manager.get_instance(bot_id).bot
        assert isinstance(bot.adapter, InfoflowAdapter)
        yield f"/api/bots/webhook/{bot_id}", bot, sent
    finally:
        manager.shutdown_all()


def test_infoflow_first_run_callback_queries_locks_and_releases(client, running_infoflow):
    callback, bot, sent = running_infoflow
    args = {"rn": "251", "timestamp": "1780000000"}
    args["signature"] = hashlib.md5(f"{args['rn']}{args['timestamp']}{TOKEN}".encode()).hexdigest()

    verification = client.post(callback, data={**args, "echostr": "verify-test-callback"})
    assert (verification.status_code, verification.text) == (200, "verify-test-callback")
    assert sent == []

    def group_message(command, *, group=GROUP_ID, signed_args=args):
        payload = {
            "message": {
                "header": {"fromuserid": ACTOR, "toid": group},
                "body": [{"type": "AT", "atuserids": ["test-bot"]}, {"type": "TEXT", "content": command}],
            }
        }
        return client.post(callback, params=signed_args, content=AESCipher(AES_KEY).encrypt(json.dumps(payload)))

    def reply_for(command):
        assert sent == []
        response = group_message(command)
        assert (response.status_code, response.text) == (200, "command succeed")
        assert len(sent) == 1
        reply = sent.pop()
        assert reply["header"]["toid"] == [GROUP_ID]
        assert {"type": "AT", "atuserids": [ACTOR]} in reply["body"]
        return "".join(part["content"] for part in reply["body"] if part["type"] == "TEXT")

    lock = "lock demo-node 5m"
    assert group_message(lock, signed_args={**args, "signature": "invalid"}).status_code == 401
    assert group_message(lock, group=GROUP_ID + 1).status_code == 403
    assert sent == []
    assert "demo-node 空闲" in reply_for("query")

    assert "资源申请成功" in reply_for(lock)
    _, state = bot.state_snapshot()
    holder = state["demo-node"]["current_users"]
    assert [(user["user_id"], user["duration"]) for user in holder] == [(ACTOR, 300)]
    assert f"demo-node {ACTOR}" in reply_for("query")

    assert "资源释放成功" in reply_for("unlock demo-node")
    assert "demo-node 空闲" in reply_for("query")
