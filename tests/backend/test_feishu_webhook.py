"""Route-level tests for Feishu webhook authentication and deferred processing."""

import base64
import hashlib
import json
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES
from lockbot.core.config import Config
from lockbot.core.platforms.feishu import FeishuAdapter


def _bot_payload(**credential_overrides):
    credentials = {
        "app_id": "cli_test",
        "app_secret": "secret",
        "verification_token": "verification-token",
        "encrypt_key": "",
    }
    credentials.update(credential_overrides)
    return {
        "name": "feishu_route_bot",
        "bot_type": "NODE",
        "platform": "Feishu",
        "credentials": credentials,
        "cluster_configs": {"n1": "n1"},
    }


def _create_bot(client, auth_header, **credential_overrides):
    response = client.post("/api/bots", json=_bot_payload(**credential_overrides), headers=auth_header)
    assert response.status_code == 201
    return response.json()["id"]


def _encrypt(encrypt_key, payload):
    plaintext = json.dumps(payload).encode()
    pad_length = AES.block_size - len(plaintext) % AES.block_size
    padded = plaintext + bytes([pad_length]) * pad_length
    iv = b"0123456789abcdef"
    key = hashlib.sha256(encrypt_key.encode()).digest()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    return base64.b64encode(iv + ciphertext).decode()


def _message_event(event_id="route-event-1", message_id="om_message"):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": "verification-token",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": "user",
            },
            "message": {
                "chat_id": "oc_group",
                "chat_type": "group",
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 query"}),
                "mentions": [{"key": "@_user_1", "name": "LockBot"}],
            },
        },
    }


class TestStoppedFeishuChallenge:
    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_plaintext_challenge_works_while_stopped(self, manager, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        manager.get_instance.return_value = None

        response = client.post(
            f"/api/bots/webhook/{bot_id}",
            json={
                "type": "url_verification",
                "challenge": "plain-challenge",
                "token": "verification-token",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"challenge": "plain-challenge"}

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_encrypted_challenge_without_signature_works_while_stopped(self, manager, client, auth_header):
        encrypt_key = "encrypt-key"
        bot_id = _create_bot(client, auth_header, encrypt_key=encrypt_key)
        manager.get_instance.return_value = None
        payload = {
            "type": "url_verification",
            "challenge": "encrypted-challenge",
            "token": "verification-token",
        }

        response = client.post(
            f"/api/bots/webhook/{bot_id}",
            json={"encrypt": _encrypt(encrypt_key, payload)},
        )

        assert response.status_code == 200
        assert response.json() == {"challenge": "encrypted-challenge"}


class TestDeferredFeishuEvent:
    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_event_is_processed_once_and_metadata_is_recorded(self, manager, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        config = Config(
            {
                "BOT_NAME": "feishu_route_bot",
                "app_id": "cli_test",
                "app_secret": "secret",
                "verification_token": "verification-token",
                "encrypt_key": "",
            }
        )
        adapter = FeishuAdapter(config=config)
        adapter.process_prepared_webhook = MagicMock(return_value=("command succeed", 200, {}))
        bot = MagicMock(adapter=adapter, config=config)
        manager.get_instance.return_value = MagicMock(bot=bot)
        first_payload = _message_event(event_id="route-event-first", message_id="om_same_message")
        retry_payload = _message_event(event_id="route-event-retry", message_id="om_same_message")

        first = client.post(f"/api/bots/webhook/{bot_id}", json=first_payload)
        second = client.post(f"/api/bots/webhook/{bot_id}", json=retry_payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert adapter.process_prepared_webhook.call_count == 1
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "oc_group"
        assert detail["last_user_id"] == "ou_user"
        command_logs = [entry for entry in client.get(f"/api/bots/{bot_id}/logs", headers=auth_header).json()]
        assert any(entry["category"] == "command" and "query" in entry["message"] for entry in command_logs)
