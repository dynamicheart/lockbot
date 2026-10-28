import base64
import hashlib
import json
import logging
import time
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES
from fastapi.testclient import TestClient

from scripts.dev.feishu_echo import (
    RuntimeState,
    Settings,
    compute_signature,
    create_app,
    decrypt_event,
    extract_text,
    get_user_name,
    send_text,
    trace,
)


def _settings(**overrides):
    values = {
        "app_id": "cli_test",
        "app_secret": "secret",
        "verification_token": "verification-token",
        "encrypt_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def _message_event(*, chat_type="p2p", text="hello", event_id="event-1", message_id="message-1"):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": "verification-token",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user", "user_id": "user-1"},
                "sender_type": "user",
            },
            "message": {
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _encrypt(encrypt_key, payload):
    plaintext = json.dumps(payload).encode()
    pad_length = AES.block_size - len(plaintext) % AES.block_size
    padded = plaintext + bytes([pad_length]) * pad_length
    iv = b"0123456789abcdef"
    key = hashlib.sha256(encrypt_key.encode()).digest()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    return base64.b64encode(iv + ciphertext).decode()


def test_health_does_not_expose_credentials():
    client = TestClient(create_app(_settings()))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "app_credentials_configured": True,
        "verification_token_configured": True,
        "encryption_enabled": False,
    }
    assert "secret" not in response.text


def test_plaintext_challenge():
    client = TestClient(create_app(_settings()))
    response = client.post(
        "/feishu/events",
        json={"type": "url_verification", "challenge": "challenge-value", "token": "verification-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


def test_invalid_verification_token_is_rejected():
    client = TestClient(create_app(_settings()))
    response = client.post(
        "/feishu/events",
        json={"type": "url_verification", "challenge": "challenge-value", "token": "wrong"},
    )
    assert response.status_code == 401


def test_p2p_message_replies_to_open_id():
    client = TestClient(create_app(_settings()))
    with patch("scripts.dev.feishu_echo.send_echo") as send_echo:
        response = client.post("/feishu/events", json=_message_event(chat_type="p2p", text="query"))
    assert response.status_code == 200
    message = send_echo.call_args.args[2]
    assert message["receive_id_type"] == "open_id"
    assert message["receive_id"] == "ou_user"
    assert message["text"] == "query"


def test_group_message_strips_mention_and_replies_to_chat():
    payload = _message_event(chat_type="group", text="@_user_1 query", event_id="event-group")
    payload["event"]["message"]["mentions"] = [{"key": "@_user_1", "name": "Echo Bot"}]
    client = TestClient(create_app(_settings()))
    with patch("scripts.dev.feishu_echo.send_echo") as send_echo:
        response = client.post("/feishu/events", json=payload)
    assert response.status_code == 200
    message = send_echo.call_args.args[2]
    assert message["chat_type"] == "group"
    assert message["receive_id_type"] == "chat_id"
    assert message["receive_id"] == "oc_chat"
    assert message["text"] == "query"


def test_duplicate_event_is_acknowledged_without_second_reply():
    client = TestClient(create_app(_settings()))
    first_payload = _message_event(event_id="event-first", message_id="message-same")
    retry_payload = _message_event(event_id="event-retry", message_id="message-same")
    with patch("scripts.dev.feishu_echo.send_echo") as send_echo:
        first = client.post("/feishu/events", json=first_payload)
        second = client.post("/feishu/events", json=retry_payload)
    assert first.json() == {"code": 0}
    assert second.json() == {"code": 0, "duplicate": True}
    send_echo.assert_called_once()


def test_trace_keeps_message_shape_and_redacts_credentials(caplog):
    settings = _settings(log_raw=True)
    with caplog.at_level(logging.INFO, logger="feishu_echo"):
        trace(
            settings,
            "callback_request",
            payload={
                "token": "verification-token",
                "event": {
                    "message": {
                        "content": json.dumps({"text": "@_user_1 hello"}),
                        "mentions": [{"key": "@_user_1", "name": "lockbot"}],
                    }
                },
            },
            authorization="Bearer secret-token",
        )

    output = caplog.text
    assert '"name": "lockbot"' in output
    assert "@_user_1 hello" in output
    assert "verification-token" not in output
    assert "secret-token" not in output
    assert output.count("[REDACTED]") == 2


def test_get_user_name_uses_open_id_and_cache():
    settings = _settings()
    runtime = RuntimeState(token="tenant-token", token_expires_at=time.time() + 7200)
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "code": 0,
        "msg": "success",
        "data": {"user": {"open_id": "ou_user", "name": "杨健邦"}},
    }
    response.headers = {"X-Tt-Logid": "log-id"}

    with patch("scripts.dev.feishu_echo.requests.get", return_value=response) as get:
        assert get_user_name(settings, runtime, "ou_user") == "杨健邦"
        assert get_user_name(settings, runtime, "ou_user") == "杨健邦"

    get.assert_called_once_with(
        "https://open.feishu.cn/open-apis/contact/v3/users/ou_user",
        params={"user_id_type": "open_id"},
        headers={
            "Authorization": "Bearer tenant-token",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=10,
    )


def test_non_text_event_is_acknowledged_without_reply():
    payload = _message_event()
    payload["event"]["message"]["message_type"] = "image"
    client = TestClient(create_app(_settings()))
    with patch("scripts.dev.feishu_echo.send_echo") as send_echo:
        response = client.post("/feishu/events", json=payload)
    assert response.json() == {"code": 0}
    send_echo.assert_not_called()


def test_encrypted_event_with_valid_signature():
    encrypt_key = "encrypt-key"
    settings = _settings(encrypt_key=encrypt_key)
    payload = _message_event(event_id="encrypted-event")
    body = json.dumps({"encrypt": _encrypt(encrypt_key, payload)}).encode()
    timestamp = str(int(time.time()))
    nonce = "nonce-value"
    signature = compute_signature(encrypt_key, timestamp, nonce, body)
    client = TestClient(create_app(settings))

    with patch("scripts.dev.feishu_echo.send_echo") as send_echo:
        response = client.post(
            "/feishu/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": signature,
            },
        )
    assert response.status_code == 200
    send_echo.assert_called_once()


def test_encrypted_challenge_without_signature_headers():
    encrypt_key = "encrypt-key"
    payload = {
        "type": "url_verification",
        "challenge": "encrypted-challenge",
        "token": "verification-token",
    }
    body = json.dumps({"encrypt": _encrypt(encrypt_key, payload)}).encode()
    client = TestClient(create_app(_settings(encrypt_key=encrypt_key)))

    response = client.post("/feishu/events", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"challenge": "encrypted-challenge"}


def test_encrypted_challenge_still_validates_verification_token():
    encrypt_key = "encrypt-key"
    payload = {"type": "url_verification", "challenge": "challenge", "token": "wrong-token"}
    body = json.dumps({"encrypt": _encrypt(encrypt_key, payload)}).encode()
    client = TestClient(create_app(_settings(encrypt_key=encrypt_key)))

    response = client.post("/feishu/events", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 401


def test_encrypted_event_with_bad_signature_is_rejected():
    encrypt_key = "encrypt-key"
    body = json.dumps({"encrypt": _encrypt(encrypt_key, _message_event())}).encode()
    client = TestClient(create_app(_settings(encrypt_key=encrypt_key)))
    response = client.post(
        "/feishu/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": str(int(time.time())),
            "X-Lark-Request-Nonce": "nonce-value",
            "X-Lark-Signature": "bad-signature",
        },
    )
    assert response.status_code == 401


def test_decrypt_rejects_bad_padding():
    invalid = base64.b64encode(b"0" * 32).decode()
    try:
        decrypt_event("encrypt-key", invalid)
    except ValueError as exc:
        assert "padding" in str(exc)
    else:
        raise AssertionError("invalid padding should fail")


def test_extract_text_supports_legacy_at_tag():
    message = {"content": json.dumps({"text": '<at user_id="ou_bot">Bot</at> query'})}
    assert extract_text(message) == "query"


def test_send_text_uses_expected_receive_id_type_and_token_cache():
    runtime = RuntimeState(token="tenant-token", token_expires_at=time.time() + 3600)
    response = MagicMock()
    response.json.return_value = {"code": 0, "msg": "ok"}
    with patch("scripts.dev.feishu_echo.requests.post", return_value=response) as post:
        result = send_text(_settings(), runtime, "chat_id", "oc_chat", "hello")
    assert result["code"] == 0
    assert "receive_id_type=chat_id" in post.call_args.args[0]
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tenant-token"
    assert post.call_args.kwargs["json"]["receive_id"] == "oc_chat"
