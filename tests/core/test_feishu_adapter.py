"""Tests for FeishuAdapter."""

import base64
import hashlib
import json
import time
from unittest.mock import MagicMock, patch

from lockbot.core.platforms.feishu import _TOKEN_CACHE, FeishuAdapter


def _make_adapter(
    app_secret: str = "test-secret",
    app_id: str = "test-app-id",
    encrypt_key: str = "",
) -> FeishuAdapter:
    """Return a FeishuAdapter with given credentials via config mock.

    Feishu credential mapping (new):
        app_id      → App ID
        app_secret  → App Secret
        encrypt_key → Encrypt Key (optional; empty = skip signature verification)
    """
    config = MagicMock()

    def get_val(key, default=None):
        return {"app_secret": app_secret, "app_id": app_id, "encrypt_key": encrypt_key}.get(key, default)

    config.get_val.side_effect = get_val
    return FeishuAdapter(config=config)


def _make_signature(encrypt_key: str, timestamp: str, nonce: str = "", body: bytes | str = b"") -> str:
    """Compute correct Feishu SHA-256 signature.

    Per feishu2.md:
        content = (timestamp + nonce + encrypt_key).encode('utf-8') + raw_body_bytes
        signature = sha256(content).hexdigest()
    """
    string_part = (timestamp + nonce + encrypt_key).encode("utf-8")
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(string_part + body).hexdigest()


class TestFeishuVerifyRequest:
    """Signature verification — only active when Encrypt Key (webhook_url) is configured."""

    def test_no_encrypt_key_always_accepts(self):
        """Without Encrypt Key, all requests are accepted (no signature to check)."""
        adapter = _make_adapter(encrypt_key="")
        assert adapter.verify_request(signature=None, timestamp=None) is True
        assert adapter.verify_request(signature="garbage", timestamp="0") is True

    def test_valid_signature(self):
        key = "my-encrypt-key"
        adapter = _make_adapter(encrypt_key=key)
        ts = str(int(time.time()))
        body = b'{"event_type": "im.message.receive_v1"}'
        sig = _make_signature(key, ts, body=body)
        assert adapter.verify_request(signature=sig, timestamp=ts, body=body) is True

    def test_valid_signature_with_nonce(self):
        key = "my-encrypt-key"
        adapter = _make_adapter(encrypt_key=key)
        ts = str(int(time.time()))
        nonce = "abc123"
        body = b'{"event": {}}'
        sig = _make_signature(key, ts, nonce=nonce, body=body)
        assert adapter.verify_request(signature=sig, timestamp=ts, body=body, nonce=nonce) is True

    def test_string_body_also_works(self):
        """body may arrive as str; should be encoded the same way as bytes."""
        key = "my-encrypt-key"
        adapter = _make_adapter(encrypt_key=key)
        ts = str(int(time.time()))
        body = '{"event": {}}'
        sig = _make_signature(key, ts, body=body)
        assert adapter.verify_request(signature=sig, timestamp=ts, body=body) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(encrypt_key="my-key")
        ts = str(int(time.time()))
        assert adapter.verify_request(signature="badsig", timestamp=ts, body=b"{}") is False

    def test_old_timestamp_rejected(self):
        key = "my-encrypt-key"
        adapter = _make_adapter(encrypt_key=key)
        old_ts = str(int(time.time()) - 400)
        body = b"{}"
        sig = _make_signature(key, old_ts, body=body)
        assert adapter.verify_request(signature=sig, timestamp=old_ts, body=body) is False

    def test_missing_signature_returns_false(self):
        adapter = _make_adapter(encrypt_key="some-key")
        assert adapter.verify_request(signature=None, timestamp=str(int(time.time()))) is False

    def test_missing_timestamp_returns_false(self):
        adapter = _make_adapter(encrypt_key="some-key")
        assert adapter.verify_request(signature="xxx", timestamp=None) is False

    def test_invalid_timestamp_format(self):
        adapter = _make_adapter(encrypt_key="some-key")
        assert adapter.verify_request(signature="xxx", timestamp="not-a-number") is False


class TestFeishuDecryptPayload:
    def test_parses_json_string(self):
        adapter = _make_adapter()
        data = json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}})
        result = adapter.decrypt_payload(data)
        assert result["schema"] == "2.0"

    def test_parses_json_bytes(self):
        adapter = _make_adapter()
        data = b'{"type": "url_verification", "challenge": "test"}'
        result = adapter.decrypt_payload(data)
        assert result["challenge"] == "test"

    def test_empty_returns_none(self):
        adapter = _make_adapter()
        assert adapter.decrypt_payload(None) is None
        assert adapter.decrypt_payload(b"") is None

    def test_invalid_json_returns_none(self):
        adapter = _make_adapter()
        assert adapter.decrypt_payload("not json {{{") is None

    def test_encrypted_body_without_key_returns_none(self):
        """If body has 'encrypt' field but no Encrypt Key configured, return None."""
        adapter = _make_adapter(encrypt_key="")
        data = json.dumps({"encrypt": "somebase64=="})
        assert adapter.decrypt_payload(data) is None

    def test_encrypted_body_decrypted_with_key(self):
        """AES-256-CBC decrypt: key=sha256(encrypt_key), iv=first 16 bytes."""
        import os

        from Crypto.Cipher import AES

        encrypt_key = "test-encrypt-key"
        plaintext = json.dumps({"type": "im.message.receive_v1", "schema": "2.0"}).encode()

        # PKCS7 pad
        pad_len = 16 - len(plaintext) % 16
        padded = plaintext + bytes([pad_len] * pad_len)

        key = hashlib.sha256(encrypt_key.encode()).digest()
        iv = os.urandom(16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        adapter = _make_adapter(encrypt_key=encrypt_key)
        result = adapter.decrypt_payload(json.dumps({"encrypt": encrypted}))
        assert result is not None
        assert result["schema"] == "2.0"


class TestFeishuChallengeHandshake:
    """Feishu URL verification (challenge) handshake via webhook_handler."""

    def _make_bot(self, encrypt_key: str = ""):
        """Return a minimal bot mock with a FeishuAdapter."""
        bot = MagicMock()
        bot.adapter = _make_adapter(encrypt_key=encrypt_key)
        bot.config.get_val.return_value = "test-bot"
        return bot

    def test_challenge_returns_json(self):
        """Feishu saves callback URL by sending url_verification; must respond with JSON."""
        from lockbot.backend.app.bots.webhook_handler import handle_webhook

        challenge_value = "1b6aef1a-401f-406a-be41-f48911eabcef"
        body = json.dumps(
            {
                "challenge": challenge_value,
                "token": "some-verification-token",
                "type": "url_verification",
            }
        ).encode()

        text, code, meta = handle_webhook(
            bot=self._make_bot(),
            raw_form={},
            raw_args={},
            raw_body=body,
            raw_headers={"x-lark-request-timestamp": str(int(time.time()))},
        )

        assert code == 200
        assert meta["event"] == "url_verification"
        # Response must be JSON {"challenge": "..."}
        parsed = json.loads(text)
        assert parsed["challenge"] == challenge_value

    def test_challenge_with_encrypt_key(self):
        """Challenge handshake should work even when Encrypt Key is configured."""
        from lockbot.backend.app.bots.webhook_handler import handle_webhook

        challenge_value = "abc-xyz-123"
        body = json.dumps(
            {
                "challenge": challenge_value,
                "token": "vtoken",
                "type": "url_verification",
            }
        ).encode()

        key = "my-encrypt-key"
        ts = str(int(time.time()))
        sig = _make_signature(key, ts, body=body)

        text, code, meta = handle_webhook(
            bot=self._make_bot(encrypt_key=key),
            raw_form={},
            raw_args={},
            raw_body=body,
            raw_headers={
                "x-lark-request-timestamp": ts,
                "x-lark-request-nonce": "",
                "x-lark-signature": sig,
            },
        )

        assert code == 200
        assert json.loads(text)["challenge"] == challenge_value

    def test_challenge_wrong_signature_still_succeeds(self):
        """Challenge is returned before signature check (Feishu requires fast response)."""
        from lockbot.backend.app.bots.webhook_handler import handle_webhook

        body = json.dumps(
            {
                "challenge": "xyz",
                "type": "url_verification",
            }
        ).encode()

        text, code, meta = handle_webhook(
            bot=self._make_bot(encrypt_key="some-key"),
            raw_form={},
            raw_args={},
            raw_body=body,
            raw_headers={"x-lark-request-timestamp": str(int(time.time())), "x-lark-signature": "wrong"},
        )

        # Challenge always returns immediately, before signature check
        assert code == 200
        assert json.loads(text)["challenge"] == "xyz"

    def test_normal_event_with_bad_signature_rejected(self):
        """Non-challenge events with bad signature should be rejected when Encrypt Key is set."""
        from lockbot.backend.app.bots.webhook_handler import handle_webhook

        body = json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_xxx"}},
                    "message": {"chat_id": "oc_xxx", "message_type": "text", "content": '{"text":"hello"}'},
                },
            }
        ).encode()

        text, code, meta = handle_webhook(
            bot=self._make_bot(encrypt_key="my-key"),
            raw_form={},
            raw_args={},
            raw_body=body,
            raw_headers={
                "x-lark-request-timestamp": str(int(time.time())),
                "x-lark-signature": "bad-signature",
            },
        )

        assert code == 401
        assert meta["event"] == "signature_failed"

    def test_normal_event_no_encrypt_key_accepted(self):
        """Without Encrypt Key, normal events pass through signature check."""
        from lockbot.backend.app.bots.webhook_handler import handle_webhook

        body = json.dumps(
            {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_xxx"}},
                    "message": {"chat_id": "oc_xxx", "message_type": "text", "content": '{"text":"lock node1"}'},
                },
            }
        ).encode()

        bot = self._make_bot(encrypt_key="")

        with (
            patch.object(bot.adapter, "send", return_value=[]),
            patch("lockbot.core.handler.execute_command", return_value={}),
        ):
            text, code, meta = handle_webhook(
                bot=bot,
                raw_form={},
                raw_args={},
                raw_body=body,
                raw_headers={"x-lark-request-timestamp": str(int(time.time()))},
            )

        assert code == 200
        assert meta.get("command") == "lock node1"


class TestFeishuExtractCommand:
    def _msg(
        self,
        text: str = "lock node1",
        open_id: str = "ou_xxx",
        chat_id: str = "oc_xxx",
    ) -> dict:
        return {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": open_id, "user_id": "uid_xxx"}},
                "message": {
                    "chat_id": chat_id,
                    "message_type": "text",
                    "content": json.dumps({"text": text}),
                },
            },
        }

    def test_basic_extract(self):
        adapter = _make_adapter()
        user_id, chat_id, cmd = adapter.extract_command(self._msg())
        assert user_id == "ou_xxx"
        assert chat_id == "oc_xxx"
        assert cmd == "lock node1"

    def test_strips_at_mention_tags(self):
        adapter = _make_adapter()
        msg = self._msg('<at user_id="ou_bot">Bot</at> query')
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == "query"

    def test_strips_multiple_at_tags(self):
        adapter = _make_adapter()
        msg = self._msg('<at user_id="ou_bot">Bot</at><at user_id="ou_bot2">Bot2</at> free')
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == "free"

    def test_empty_text(self):
        adapter = _make_adapter()
        msg = self._msg("")
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == ""

    def test_missing_event(self):
        adapter = _make_adapter()
        user_id, chat_id, cmd = adapter.extract_command({})
        assert user_id == ""
        assert chat_id == ""
        assert cmd == ""

    def test_non_text_message_type(self):
        adapter = _make_adapter()
        msg = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "chat_id": "oc_1",
                    "message_type": "image",
                    "content": "{}",
                },
            }
        }
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == ""


class TestFeishuBuildReply:
    def test_plain_text(self):
        adapter = _make_adapter()
        reply = adapter.build_reply("hello", ["ou_user"], group_id="oc_chat")
        assert reply["receive_id"] == "oc_chat"
        assert reply["msg_type"] == "text"
        assert json.loads(reply["content"])["text"] == "hello"
        assert reply["_feishu_receive_id_type"] == "chat_id"

    def test_uses_user_id_fallback(self):
        adapter = _make_adapter()
        reply = adapter.build_reply("hi", ["ou_user"])
        assert reply["receive_id"] == "ou_user"
        assert reply["_feishu_receive_id_type"] == "open_id"

    def test_list_of_strings(self):
        adapter = _make_adapter()
        reply = adapter.build_reply(["line1\n", "line2\n"], ["ou_user"], group_id="oc_chat")
        assert json.loads(reply["content"])["text"] == "line1\nline2\n"

    def test_list_with_link_tuple(self):
        adapter = _make_adapter()
        reply = adapter.build_reply([("click here", "https://example.com")], ["ou_user"], group_id="oc_chat")
        content = json.loads(reply["content"])["text"]
        assert "[click here](https://example.com)" in content

    def test_none_content(self):
        adapter = _make_adapter()
        reply = adapter.build_reply(None, ["ou_user"], group_id="oc_chat")
        assert json.loads(reply["content"])["text"] == ""

    def test_set_reply_target(self):
        adapter = _make_adapter()
        reply = adapter.build_reply("hi", ["ou_user"])
        updated = adapter.set_reply_target(reply, "oc_group")
        assert updated["receive_id"] == "oc_group"
        assert updated["_feishu_receive_id_type"] == "chat_id"

    def test_set_reply_target_empty_group_id_noop(self):
        adapter = _make_adapter()
        reply = adapter.build_reply("hi", ["ou_user"])
        original_id = reply["receive_id"]
        updated = adapter.set_reply_target(reply, "")
        assert updated["receive_id"] == original_id


class TestFeishuSend:
    def _mock_token_cache(self, app_id: str, app_secret: str, token: str = "t-token"):
        """Pre-populate the token cache to avoid real HTTP calls."""
        _TOKEN_CACHE[(app_id, app_secret)] = (token, time.time() + 7200)

    def test_send_success(self):
        adapter = _make_adapter(app_secret="secret", app_id="app-id")
        self._mock_token_cache("app-id", "secret", "t-testtoken")
        msg = {
            "_feishu_receive_id_type": "chat_id",
            "receive_id": "oc_chat",
            "msg_type": "text",
            "content": '{"text": "hello"}',
        }
        with patch("lockbot.core.platforms.feishu.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"code":0}'
            mock_post.return_value = mock_resp
            result = adapter.send(msg)
        assert result == [(200, '{"code":0}')]
        assert "_feishu_receive_id_type" not in msg

    def test_send_no_token_returns_empty(self):
        adapter = _make_adapter(app_secret="bad-secret", app_id="bad-id")
        _TOKEN_CACHE.pop(("bad-id", "bad-secret"), None)
        msg = {
            "_feishu_receive_id_type": "chat_id",
            "receive_id": "oc_chat",
            "msg_type": "text",
            "content": '{"text": "hello"}',
        }
        with patch("lockbot.core.platforms.feishu.requests.post") as mock_post:
            mock_post.return_value = MagicMock(json=lambda: {"code": 99, "msg": "error"})
            result = adapter.send(msg)
        assert result == []

    def test_send_exception_returns_empty(self):
        adapter = _make_adapter(app_secret="secret", app_id="app-id")
        self._mock_token_cache("app-id", "secret", "t-testtoken")
        msg = {
            "_feishu_receive_id_type": "chat_id",
            "receive_id": "oc_chat",
            "msg_type": "text",
            "content": '{"text": "hello"}',
        }
        with patch(
            "lockbot.core.platforms.feishu.requests.post",
            side_effect=Exception("network error"),
        ):
            result = adapter.send(msg)
        assert result == []


class TestPlatformRegistryAll:
    def test_all_platforms_present(self):
        from lockbot.core.platforms import PLATFORM_REGISTRY
        from lockbot.core.platforms.dingtalk import DingTalkAdapter
        from lockbot.core.platforms.feishu import FeishuAdapter
        from lockbot.core.platforms.infoflow import InfoflowAdapter
        from lockbot.core.platforms.slack import SlackAdapter

        assert "Infoflow" in PLATFORM_REGISTRY
        assert "Slack" in PLATFORM_REGISTRY
        assert "DingTalk" in PLATFORM_REGISTRY
        assert "Feishu" in PLATFORM_REGISTRY
        assert PLATFORM_REGISTRY["Infoflow"] is InfoflowAdapter
        assert PLATFORM_REGISTRY["Slack"] is SlackAdapter
        assert PLATFORM_REGISTRY["DingTalk"] is DingTalkAdapter
        assert PLATFORM_REGISTRY["Feishu"] is FeishuAdapter
