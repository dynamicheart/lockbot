import base64
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from lockbot.core.config import Config
from lockbot.core.handler import execute_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.platforms.feishu import FeishuAdapter, FeishuAPIError, FeishuClient
from lockbot.core.platforms.feishu_events import (
    FeishuIdentity,
    InvalidFeishuEvent,
    normalize_message,
    verify_http,
)


def identity(**overrides):
    return FeishuIdentity(
        **dict(app_id="cli_demo", tenant_key="tenant", bot_open_id="ou_bot", verification_token="proof", **overrides)
    )


def event(text="@_user_1 lock node_a 5m"):
    return {
        "header": {
            "app_id": "cli_demo",
            "tenant_key": "tenant",
            "token": "proof",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "tenant_key": "tenant", "sender_id": {"open_id": "ou_alice"}},
            "message": {
                "message_id": "om_1",
                "create_time": str(int(time.time() * 1000)),
                "chat_type": "group",
                "chat_id": "oc_demo",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
            },
        },
    }


def encrypted(data, key="encrypt", now=1000):
    iv = b"0123456789abcdef"
    cipher = AES.new(hashlib.sha256(key.encode()).digest(), AES.MODE_CBC, iv)
    body = json.dumps(
        {"encrypt": base64.b64encode(iv + cipher.encrypt(pad(json.dumps(data).encode(), 16))).decode()}
    ).encode()
    signature = hashlib.sha256((str(now) + "nonce" + key).encode() + body).hexdigest()
    return body, {"x-lark-request-timestamp": str(now), "x-lark-request-nonce": "nonce", "x-lark-signature": signature}


def test_encrypted_signature_tampering_and_plaintext_downgrade():
    data = event()
    body, headers = encrypted(data)
    config = identity(encrypt_key="encrypt")
    assert verify_http(body, headers, config, now=1000) == data
    for rejected in (dict(headers, **{"x-lark-signature": "bad"}), dict(headers, **{"x-lark-request-nonce": "bad"})):
        with pytest.raises(InvalidFeishuEvent):
            verify_http(body, rejected, config, now=1000)
    with pytest.raises(InvalidFeishuEvent):
        verify_http(body, headers, config, now=1400)
    with pytest.raises(InvalidFeishuEvent):
        verify_http(json.dumps(event()).encode(), headers, config, now=1000)


def test_challenge_still_requires_token_and_configured_encryption():
    challenge = {"type": "url_verification", "token": "proof", "challenge": "challenge"}
    assert verify_http(json.dumps(challenge).encode(), {}, identity()) == {"challenge": "challenge"}
    body, _ = encrypted(challenge)
    assert verify_http(body, {}, identity(encrypt_key="encrypt")) == {"challenge": "challenge"}
    for data in (dict(challenge, token="wrong"), []):
        with pytest.raises(InvalidFeishuEvent):
            verify_http(json.dumps(data).encode(), {}, identity())
    with pytest.raises(InvalidFeishuEvent):
        verify_http(b"{}", {}, FeishuIdentity("cli_demo", "tenant", "ou_bot", ""))


def test_scope_and_exact_bot_mention_are_required():
    for key in ("app_id", "tenant_key"):
        data = event()
        data["header"][key] = "other"
        with pytest.raises(InvalidFeishuEvent):
            verify_http(json.dumps(data).encode(), {}, identity())
    data = event()
    data["event"]["message"]["mentions"][0]["id"]["open_id"] = "ou_other"
    assert normalize_message(data, identity()) is None
    data = event()
    data["event"]["message"]["chat_type"] = "p2p"
    assert normalize_message(data, identity()) is None
    data = event()
    data["event"]["sender"]["sender_type"] = "app"
    assert normalize_message(data, identity()) is None


def test_verified_message_runs_original_core_and_binding_hook(tmp_path):
    adapter = FeishuAdapter(Config({"_AUTHORIZED_GROUP_IDS": ["oc_demo"]}))
    bot = NodeBot(
        config_dict={"BOT_NAME": "feishu-test", "CLUSTER_CONFIGS": {"node_a": "gpu"}, "DATA_DIR": str(tmp_path)},
        adapter=adapter,
    )
    normalized = normalize_message(verify_http(json.dumps(event()).encode(), {}, identity()), identity())
    reply = execute_command(normalized, bot)
    assert "node_a" in adapter.extract_reply_text(reply)
    assert bot.state.bot_state["node_a"]["current_users"][0]["user_id"] == "ou_alice"
    calls = []
    bot.binding_hook = lambda **kw: calls.append(kw) or "bound"
    execute_command(normalize_message(event("@_user_1 bind SECRET"), identity()), bot)
    assert calls[0]["platform"] == "feishu"
    assert calls[0]["external_user_id"] == "ou_alice"


def test_rich_post_preserves_blank_lines_links_and_structured_mentions():
    adapter = FeishuAdapter()
    reply = adapter.build_reply(
        ["✅Title\n\nText <at user_id=all>\n", ("Link: ", "https://example.com"), "\n"], ["ou_alice"]
    )
    rows = json.loads(reply["content"])["zh_cn"]["content"]
    assert rows[1] == [{"tag": "text", "text": " "}]
    assert rows[2][0]["text"] == "Text <at user_id=all>"
    assert [node for row in rows for node in row if node["tag"] == "at"] == [{"tag": "at", "user_id": "ou_alice"}]
    assert "https://example.com" in adapter.extract_reply_text(reply)
    assert any(node.get("tag") == "a" for row in rows for node in row)


def test_token_refresh_is_coalesced_and_send_does_not_leak_failure(monkeypatch):
    calls = []
    lock = threading.Lock()

    class Response:
        status_code = 200

        def json(self):
            return {"code": 0, "tenant_access_token": "token", "expire": 7200}

    def post(*args, **kwargs):
        with lock:
            calls.append(kwargs)
        return Response()

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.post", post)
    client = FeishuClient("cli_demo", "secret")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: client.token(), range(8))) == ["token"] * 8
    assert len(calls) == 1
    adapter = FeishuAdapter(Config({"_FEISHU_CLIENT": client, "_AUTHORIZED_GROUP_IDS": ["oc_demo"]}))
    reply = adapter.build_reply("hello", [], "oc_other")
    with pytest.raises(FeishuAPIError, match="not authorized"):
        adapter.send(reply)
    assert len(calls) == 1


def test_agent_notification_uses_single_configured_group_without_mentions(monkeypatch):
    from lockbot.backend.app.resource_api.notifications import notification_budget, notify_group

    sent = []
    monkeypatch.setattr(FeishuClient, "send", lambda self, payload: sent.append(payload) or [(200, "accepted")])
    config = Config({"_AUTHORIZED_GROUP_IDS": ["oc_demo"], "_FEISHU_CLIENT": FeishuClient("cli_demo", "secret")})
    bot = type("Bot", (), {"config": config, "adapter": FeishuAdapter(config)})()
    notification_budget.reset()
    assert notify_group(
        bot,
        bot_id=987,
        command="lock node_a 5m",
        actor_id="mock_user_a",
        command_word="lock",
        agent_label="codex",
        purpose="Model performance test",
        task_category="performance_testing",
    )
    assert sent[0]["receive_id"] == "oc_demo"
    assert "性能测试" in sent[0]["content"]
    assert "Model performance test" not in sent[0]["content"]
    assert "codex" not in sent[0]["content"]
    rows = json.loads(sent[0]["content"])["zh_cn"]["content"]
    assert all(node["tag"] != "at" for row in rows for node in row)


def test_cached_display_labels_do_not_replace_identity_or_forward_contact_data():
    from lockbot.core.platforms.feishu import decorate_people

    client = FeishuClient("cli_demo", "secret")
    client._names["ou_alice"] = ("Alice", 100000000000)
    config = Config({"_FEISHU_CLIENT": client})
    resources = [
        {
            "holders": [{"external_user_id": "ou_alice", "source": "api"}],
            "devices": [{"booking_list": [{"external_user_id": "ou_unknown"}]}],
        }
    ]
    decorate_people(resources, config)
    assert resources[0]["holders"][0] == {"external_user_id": "ou_alice", "display_name": "Alice", "source": "api"}
    assert resources[0]["devices"][0]["booking_list"][0] == {"external_user_id": "ou_unknown"}


@pytest.mark.parametrize(
    "kind,clusters,command",
    [
        ("NODE", ["node_a"], "lock node_a 5m"),
        ("QUEUE", ["node_a"], "lock node_a 5m"),
        ("DEVICE", {"node_a": ["gpu"]}, "lock node_a dev0 5m"),
        ("QUEUE_DEVICE", {"node_a": ["gpu"]}, "lock node_a dev0 5m"),
    ],
)
def test_feishu_dispatch_reuses_each_existing_bot_command_parser(kind, clusters, command, tmp_path):
    from lockbot.core.bot_instance import BotInstance

    bot = BotInstance(
        kind, {"PLATFORM": "feishu", "BOT_NAME": "parser-test", "CLUSTER_CONFIGS": clusters, "DATA_DIR": str(tmp_path)}
    ).bot
    reply = execute_command({"user_id": "ou_alice", "group_id": "oc_demo", "command": command}, bot)
    assert "✅" in bot.adapter.extract_reply_text(reply)
    state = bot.state.bot_state["node_a"]
    if isinstance(state, list):
        state = state[0]
    assert state["current_users"][0]["user_id"] == "ou_alice"


def test_group_name_is_scoped_cached_and_preserved_on_transient_error(monkeypatch):
    from types import SimpleNamespace

    calls = []
    responses = iter(
        [
            {"code": 0, "data": {"name": "测试群"}},
            {"code": 99991672},
            {"code": 0, "data": {"name": "改名后的群"}},
        ]
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: next(responses))

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.get", get)
    monkeypatch.setattr(FeishuClient, "token", lambda self: "private-token")
    client = FeishuClient("cli_app", "secret")
    assert client.group_name("oc_group") == {"name": "测试群", "status": "ok"}
    assert client.group_name("oc_group")["name"] == "测试群"
    assert len(calls) == 1
    assert client.group_name("oc_group", True) == {"name": "测试群", "status": "permission"}
    assert client.group_name("oc_group", True)["name"] == "改名后的群"
    assert all(url.endswith("/im/v1/chats/oc_group") for url, _ in calls)
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)
    with pytest.raises(ValueError):
        client.group_name("oc_group/../../users")
    assert len(calls) == 3


def test_known_user_name_survives_temporary_lookup_failure(monkeypatch):
    from types import SimpleNamespace

    client = FeishuClient("cli_app", "secret")
    monkeypatch.setattr(FeishuClient, "token", lambda self: "token")
    monkeypatch.setattr(
        "lockbot.core.platforms.feishu.requests.get",
        lambda *a, **k: SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 0, "data": {"user": {"name": "Alice"}}},
        ),
    )
    client.warm_user_name("ou_alice")
    assert client.display_text("holder: ou_alice") == "holder: Alice"
    client._names["ou_alice"] = ("Alice", 0)
    monkeypatch.setattr(
        "lockbot.core.platforms.feishu.requests.get",
        lambda *a, **k: SimpleNamespace(
            status_code=403,
            json=lambda: {"code": 99991672},
        ),
    )
    client.warm_user_name("ou_alice")
    assert client.display_text("holder: ou_alice") == "holder: Alice"


def test_cached_person_and_group_labels_are_exact_non_network_reads(monkeypatch):
    import requests

    client = FeishuClient("cli_fixture", "secret")
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("cached lookup attempted network"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("cached lookup attempted token request"))
    client._names.update({"ou_alice": ("Same name", 10**12), "ou_bob": ("Same name", 10**12)})
    client._groups["oc_fixture"] = ("Example group", "ok", 10**12)
    assert client.cached_user_name("ou_alice") == client.cached_user_name("ou_bob") == "Same name"
    assert client.cached_group_name("oc_fixture") == "Example group"
    for invalid in (None, {}, "ou_alice extra", "Alice", "ou_unknown"):
        assert client.cached_user_name(invalid) is None
    for invalid in (None, {}, "oc_fixture/../../", "oc_unknown"):
        assert client.cached_group_name(invalid) is None
    client._names["ou_alice"] = ("Expired name", 0)
    client._groups["oc_fixture"] = ("Expired group", "ok", 0)
    assert client.cached_user_name("ou_alice") is None and client.cached_group_name("oc_fixture") is None
    restarted = FeishuClient("cli_fixture", "secret")
    assert restarted.cached_user_name("ou_bob") is None
