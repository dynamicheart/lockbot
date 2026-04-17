"""Tests for DingTalkAdapter."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from lockbot.core.platforms.dingtalk import DingTalkAdapter


def _make_adapter(app_secret: str = "test-secret") -> DingTalkAdapter:
    """Return a DingTalkAdapter with given credentials via config mock."""
    config = MagicMock()

    def get_val(key, default=None):
        return {"TOKEN": app_secret}.get(key, default)

    config.get_val.side_effect = get_val
    return DingTalkAdapter(config=config)


def _make_sign(timestamp_ms: int, secret: str) -> str:
    """Compute the expected DingTalk signature."""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    mac = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


class TestDingTalkVerifyRequest:
    def test_valid_signature(self):
        adapter = _make_adapter("my-secret")
        ts_ms = int(time.time() * 1000)
        sign = _make_sign(ts_ms, "my-secret")
        assert adapter.verify_request(signature=sign, timestamp=str(ts_ms)) is True

    def test_invalid_signature(self):
        adapter = _make_adapter("my-secret")
        ts_ms = int(time.time() * 1000)
        assert adapter.verify_request(signature="badsign==", timestamp=str(ts_ms)) is False

    def test_replayed_old_timestamp(self):
        """Timestamps older than 1 hour should be rejected."""
        adapter = _make_adapter("my-secret")
        old_ts_ms = int((time.time() - 3700) * 1000)  # 3700s ago > 3600s limit
        sign = _make_sign(old_ts_ms, "my-secret")
        assert adapter.verify_request(signature=sign, timestamp=str(old_ts_ms)) is False

    def test_missing_signature_returns_false(self):
        adapter = _make_adapter()
        ts_ms = str(int(time.time() * 1000))
        assert adapter.verify_request(signature=None, timestamp=ts_ms) is False

    def test_missing_timestamp_returns_false(self):
        adapter = _make_adapter()
        assert adapter.verify_request(signature="xxx", timestamp=None) is False

    def test_invalid_timestamp_format(self):
        adapter = _make_adapter()
        assert adapter.verify_request(signature="xxx", timestamp="not-a-number") is False


class TestDingTalkDecryptPayload:
    def test_parses_json_string(self):
        adapter = _make_adapter()
        data = json.dumps({"msgtype": "text", "text": {"content": "hello"}})
        result = adapter.decrypt_payload(data)
        assert result["msgtype"] == "text"

    def test_parses_json_bytes(self):
        adapter = _make_adapter()
        data = b'{"msgtype": "text", "text": {"content": "hello"}}'
        result = adapter.decrypt_payload(data)
        assert result["text"]["content"] == "hello"

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


class TestDingTalkExtractCommand:
    def _msg(self, text: str = "lock node1", sender_id: str = "U1", conv_id: str = "C1") -> dict:
        return {
            "senderId": sender_id,
            "conversationId": conv_id,
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
            "msgtype": "text",
            "text": {"content": text},
        }

    def test_basic_extract(self):
        adapter = _make_adapter()
        user_id, conv_id, cmd = adapter.extract_command(self._msg())
        assert user_id == "U1"
        assert conv_id == "C1"
        assert cmd == "lock node1"

    def test_strips_at_mention(self):
        adapter = _make_adapter()
        user_id, conv_id, cmd = adapter.extract_command(self._msg("@bot123 query"))
        assert cmd == "query"

    def test_strips_multiple_mentions(self):
        adapter = _make_adapter()
        user_id, conv_id, cmd = adapter.extract_command(self._msg("@bot1 @bot2 free"))
        assert cmd == "free"

    def test_saves_session_webhook(self):
        adapter = _make_adapter()
        msg = self._msg("query")
        adapter.extract_command(msg)
        assert adapter._session_webhook == "https://oapi.dingtalk.com/robot/sendBySession?session=abc"

    def test_uses_sender_staff_id_fallback(self):
        adapter = _make_adapter()
        msg = {
            "senderStaffId": "staff-001",
            "conversationId": "C2",
            "sessionWebhook": "https://webhook",
            "msgtype": "text",
            "text": {"content": "query"},
        }
        user_id, _, _ = adapter.extract_command(msg)
        assert user_id == "staff-001"

    def test_non_text_msgtype_empty_command(self):
        adapter = _make_adapter()
        msg = {
            "senderId": "U1",
            "conversationId": "C1",
            "sessionWebhook": "https://webhook",
            "msgtype": "picture",
        }
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == ""


class TestDingTalkBuildReply:
    def test_plain_text(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://webhook"
        reply = adapter.build_reply("hello", ["U1"])
        assert reply["msgtype"] == "text"
        assert reply["text"]["content"] == "hello"
        assert reply["_dingtalk_session_webhook"] == "https://webhook"

    def test_list_of_strings(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://webhook"
        reply = adapter.build_reply(["line1\n", "line2\n"], ["U1"])
        assert reply["text"]["content"] == "line1\nline2\n"

    def test_list_with_link_tuple(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://webhook"
        reply = adapter.build_reply([("see here", "https://example.com")], ["U1"])
        assert "see here" in reply["text"]["content"]
        assert "https://example.com" in reply["text"]["content"]

    def test_none_content(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://webhook"
        reply = adapter.build_reply(None, ["U1"])
        assert reply["text"]["content"] == ""

    def test_set_reply_target_noop(self):
        """set_reply_target should be a no-op for DingTalk (uses sessionWebhook)."""
        adapter = _make_adapter()
        reply = {"msgtype": "text"}
        result = adapter.set_reply_target(reply, "some-group-id")
        assert result == reply


class TestDingTalkSend:
    def test_send_success(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=abc"
        msg = {
            "_dingtalk_session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
            "msgtype": "text",
            "text": {"content": "hello"},
        }
        with patch("lockbot.core.platforms.dingtalk.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"errcode":0}'
            mock_post.return_value = mock_resp
            result = adapter.send(msg)
        assert result == [(200, '{"errcode":0}')]
        # Verify the _dingtalk_session_webhook key was popped from the message
        assert "_dingtalk_session_webhook" not in msg

    def test_send_no_webhook_returns_empty(self):
        adapter = _make_adapter()
        adapter._session_webhook = ""
        msg = {"msgtype": "text", "text": {"content": "hello"}}
        result = adapter.send(msg)
        assert result == []

    def test_send_uses_instance_webhook_as_fallback(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://fallback-webhook"
        msg = {"msgtype": "text", "text": {"content": "hello"}}
        with patch("lockbot.core.platforms.dingtalk.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"errcode":0}'
            mock_post.return_value = mock_resp
            result = adapter.send(msg)
        assert result == [(200, '{"errcode":0}')]
        mock_post.assert_called_once_with("https://fallback-webhook", json=msg, timeout=10)

    def test_send_exception_returns_empty(self):
        adapter = _make_adapter()
        adapter._session_webhook = "https://webhook"
        msg = {"msgtype": "text", "text": {"content": "hello"}}
        with patch(
            "lockbot.core.platforms.dingtalk.requests.post",
            side_effect=Exception("connection error"),
        ):
            result = adapter.send(msg)
        assert result == []
