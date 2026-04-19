"""Tests for SlackAdapter."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from lockbot.core.platforms.slack import SlackAdapter


def _make_adapter(token="xoxb-test", signing_secret="test-secret"):
    """Return a SlackAdapter with given credentials via config mock.

    Slack credential mapping (new):
        bot_token       → Bot Token (xoxb-...)
        signing_secret  → Signing Secret
    """
    config = MagicMock()

    def get_val(key, default=None):
        return {"bot_token": token, "signing_secret": signing_secret}.get(key, default)

    config.get_val.side_effect = get_val
    return SlackAdapter(config=config)


def _make_signature(body: str, secret: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


class TestSlackVerifyRequest:
    def test_valid_signature(self):
        adapter = _make_adapter(signing_secret="my-secret")
        ts = str(int(time.time()))
        body = "test=body"
        sig = _make_signature(body, "my-secret", ts)
        assert adapter.verify_request(sig, timestamp=ts, body=body) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(signing_secret="my-secret")
        ts = str(int(time.time()))
        assert adapter.verify_request("v0=invalid", timestamp=ts, body="test=body") is False

    def test_replayed_old_timestamp(self):
        adapter = _make_adapter(signing_secret="my-secret")
        old_ts = str(int(time.time()) - 400)  # 400s ago > 300s limit
        body = "test=body"
        sig = _make_signature(body, "my-secret", old_ts)
        assert adapter.verify_request(sig, timestamp=old_ts, body=body) is False

    def test_missing_params_returns_false(self):
        adapter = _make_adapter()
        assert adapter.verify_request(None) is False
        assert adapter.verify_request("v0=x", timestamp=None, body="y") is False
        assert adapter.verify_request("v0=x", timestamp="123", body=None) is False


class TestSlackDecryptPayload:
    def test_parses_json_string(self):
        adapter = _make_adapter()
        payload = json.dumps({"type": "event_callback", "event": {"text": "hello"}})
        result = adapter.decrypt_payload(payload)
        assert result["type"] == "event_callback"

    def test_parses_json_bytes(self):
        adapter = _make_adapter()
        payload = b'{"type": "url_verification", "challenge": "abc"}'
        result = adapter.decrypt_payload(payload)
        assert result["challenge"] == "abc"

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


class TestSlackExtractCommand:
    def test_strips_mention(self):
        adapter = _make_adapter()
        msg = {"event": {"user": "U123", "channel": "C456", "text": "<@UBOT> lock node1 2h"}}
        user_id, channel, cmd = adapter.extract_command(msg)
        assert user_id == "U123"
        assert channel == "C456"
        assert cmd == "lock node1 2h"

    def test_strips_mention_with_extra_spaces(self):
        adapter = _make_adapter()
        msg = {"event": {"user": "U1", "channel": "C1", "text": "<@UBOT>   query"}}
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == "query"

    def test_no_mention(self):
        adapter = _make_adapter()
        msg = {"event": {"user": "U1", "channel": "C1", "text": "free"}}
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == "free"

    def test_empty_text(self):
        adapter = _make_adapter()
        msg = {"event": {"user": "U1", "channel": "C1", "text": ""}}
        _, _, cmd = adapter.extract_command(msg)
        assert cmd == ""

    def test_missing_event(self):
        adapter = _make_adapter()
        user_id, channel, cmd = adapter.extract_command({})
        assert user_id == ""
        assert channel == ""
        assert cmd == ""


class TestSlackBuildReply:
    def test_plain_text(self):
        adapter = _make_adapter()
        msg = adapter.build_reply("hello world", ["U1"], group_id="C123")
        assert msg["channel"] == "C123"
        assert msg["text"] == "hello world"

    def test_uses_user_id_as_channel_fallback(self):
        adapter = _make_adapter()
        msg = adapter.build_reply("hi", ["U999"])
        assert msg["channel"] == "U999"

    def test_list_with_strings(self):
        adapter = _make_adapter()
        msg = adapter.build_reply(["line1\n", "line2\n"], ["U1"], group_id="C1")
        assert msg["text"] == "line1\nline2\n"

    def test_list_with_link_tuple(self):
        adapter = _make_adapter()
        msg = adapter.build_reply([("click here", "https://example.com")], ["U1"], group_id="C1")
        assert "<https://example.com|click here>" in msg["text"]

    def test_none_content(self):
        adapter = _make_adapter()
        msg = adapter.build_reply(None, ["U1"], group_id="C1")
        assert msg["text"] == ""


class TestSlackSend:
    def test_send_success(self):
        adapter = _make_adapter(token="xoxb-real")
        mock_response = MagicMock()
        mock_response.data = {"ok": True}
        with patch("lockbot.core.platforms.slack.WebClient") as MockClient:
            MockClient.return_value.chat_postMessage.return_value = mock_response
            result = adapter.send({"channel": "C1", "text": "hello"})
        assert result == [(200, str({"ok": True}))]
        MockClient.assert_called_once_with(token="xoxb-real")

    def test_send_no_token_returns_empty(self):
        adapter = _make_adapter(token="")
        result = adapter.send({"channel": "C1", "text": "hi"})
        assert result == []

    def test_send_slack_api_error(self):
        adapter = _make_adapter(token="xoxb-real")
        with patch("lockbot.core.platforms.slack.WebClient") as MockClient:
            from slack_sdk.errors import SlackApiError

            mock_resp = MagicMock()
            mock_resp.__getitem__ = lambda self, key: "channel_not_found" if key == "error" else None
            mock_resp.status_code = 404
            MockClient.return_value.chat_postMessage.side_effect = SlackApiError(
                message="channel_not_found", response=mock_resp
            )
            result = adapter.send({"channel": "C_BAD", "text": "hi"})
        assert result == [(404, "channel_not_found")]


class TestPlatformRegistry:
    def test_registry_contains_infoflow_and_slack(self):
        from lockbot.core.platforms import PLATFORM_REGISTRY
        from lockbot.core.platforms.infoflow import InfoflowAdapter

        assert "Infoflow" in PLATFORM_REGISTRY
        assert "Slack" in PLATFORM_REGISTRY
        assert PLATFORM_REGISTRY["Infoflow"] is InfoflowAdapter
        assert PLATFORM_REGISTRY["Slack"] is SlackAdapter
