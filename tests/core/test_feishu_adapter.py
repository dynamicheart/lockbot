"""Tests for FeishuAdapter."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from lockbot.core.platforms.feishu import _TOKEN_CACHE, FeishuAdapter


def _make_adapter(app_secret: str = "test-secret", app_id: str = "test-app-id") -> FeishuAdapter:
    """Return a FeishuAdapter with given credentials via config mock."""
    config = MagicMock()

    def get_val(key, default=None):
        return {"TOKEN": app_secret, "AES_KEY": app_id}.get(key, default)

    config.get_val.side_effect = get_val
    return FeishuAdapter(config=config)


def _make_signature(secret: str, timestamp: str, nonce: str = "", body: str = "") -> str:
    string_to_sign = f"{timestamp}{nonce}{body}"
    mac = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


class TestFeishuVerifyRequest:
    def test_valid_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        ts = str(int(time.time()))
        body = '{"event_type": "im.message.receive_v1"}'
        sig = _make_signature("my-secret", ts, body=body)
        assert adapter.verify_request(signature=sig, timestamp=ts, body=body) is True

    def test_valid_signature_with_nonce(self):
        adapter = _make_adapter(app_secret="my-secret")
        ts = str(int(time.time()))
        nonce = "abc123"
        body = '{"event": {}}'
        sig = _make_signature("my-secret", ts, nonce=nonce, body=body)
        assert adapter.verify_request(signature=sig, timestamp=ts, body=body, nonce=nonce) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        ts = str(int(time.time()))
        assert adapter.verify_request(signature="badsig", timestamp=ts, body="{}") is False

    def test_old_timestamp_rejected(self):
        adapter = _make_adapter(app_secret="my-secret")
        old_ts = str(int(time.time()) - 400)
        body = "{}"
        sig = _make_signature("my-secret", old_ts, body=body)
        assert adapter.verify_request(signature=sig, timestamp=old_ts, body=body) is False

    def test_missing_signature_returns_false(self):
        adapter = _make_adapter()
        assert adapter.verify_request(signature=None, timestamp=str(int(time.time()))) is False

    def test_missing_timestamp_returns_false(self):
        adapter = _make_adapter()
        assert adapter.verify_request(signature="xxx", timestamp=None) is False

    def test_invalid_timestamp_format(self):
        adapter = _make_adapter()
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

    def test_passthrough_dict(self):
        adapter = _make_adapter()
        d = {"already": "parsed"}
        assert adapter.decrypt_payload(d) == d

    def test_empty_returns_none(self):
        adapter = _make_adapter()
        assert adapter.decrypt_payload(None) is None
        assert adapter.decrypt_payload(b"") is None

    def test_invalid_json_returns_none(self):
        adapter = _make_adapter()
        assert adapter.decrypt_payload("not json {{{") is None


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
        # Remove from cache to force a real fetch (which we'll mock to fail)
        _TOKEN_CACHE.pop(("bad-id", "bad-secret"), None)
        msg = {
            "_feishu_receive_id_type": "chat_id",
            "receive_id": "oc_chat",
            "msg_type": "text",
            "content": '{"text": "hello"}',
        }
        with patch("lockbot.core.platforms.feishu.requests.post") as mock_post:
            # Token fetch fails
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
