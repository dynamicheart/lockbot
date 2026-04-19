"""
Slack platform adapter for LockBot.

Implements MessageAdapter for Slack, handling:
- HMAC-SHA256 signature verification (X-Slack-Signature)
- No encryption (plain JSON events)
- Slack Events API message format
- chat.postMessage via slack_sdk WebClient

Credential mapping (reuses existing DB fields, no schema change needed):
    token    → Bot Token (xoxb-...)  — for calling Slack API
    aes_key  → Signing Secret        — for verifying incoming webhooks
    webhook_url → Event Subscription URL (displayed to user, configured in Slack App)
"""

import hashlib
import hmac
import json
import logging
import re
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from lockbot.core.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)

# Reject requests older than this many seconds (replay protection)
_MAX_TIMESTAMP_AGE = 300


class SlackAdapter(MessageAdapter):
    """MessageAdapter implementation for Slack."""

    def __init__(self, config=None):
        """
        Args:
            config: Config instance for platform credentials (bot_token, signing_secret, event_url).
                    If None, falls back to global Config singleton.
        """
        self.config = config

    def set_reply_target(self, reply: dict, group_id: str) -> dict:
        """Override the Slack channel with the given channel/group ID."""
        if group_id:
            reply["channel"] = group_id
        return reply

    def _get_config(self, key: str, default="") -> str:
        if self.config is not None:
            try:
                val = self.config.get_val(key)
                if val is not None:
                    return str(val)
            except Exception:
                pass
        from lockbot.core.config import Config

        return str(Config.get(key) or default)

    def verify_request(self, signature: str, timestamp: str = None, body: str = None, **kwargs) -> bool:
        """Verify Slack request signature (HMAC-SHA256).

        Args:
            signature: Value of X-Slack-Signature header (e.g. "v0=abc123...").
            timestamp: Value of X-Slack-Request-Timestamp header.
            body: Raw request body string.
        """
        if not signature or not timestamp or body is None:
            return False

        # Replay attack protection
        try:
            if abs(time.time() - int(timestamp)) > _MAX_TIMESTAMP_AGE:
                logger.warning("Slack request timestamp too old: %s", timestamp)
                return False
        except (ValueError, TypeError):
            return False

        signing_secret = self._get_config("signing_secret", "")
        base = f"v0:{timestamp}:{body}"
        expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def decrypt_payload(self, encrypted_data, **kwargs):
        """Parse Slack JSON payload (no encryption).

        Args:
            encrypted_data: Raw request body (bytes or str).

        Returns:
            Parsed dict, or None on failure.
        """
        if not encrypted_data:
            return None
        if isinstance(encrypted_data, bytes):
            encrypted_data = encrypted_data.decode("utf-8")
        if isinstance(encrypted_data, str):
            try:
                return json.loads(encrypted_data)
            except Exception:
                return None
        return encrypted_data

    def extract_command(self, msg_data: dict) -> tuple:
        """Extract (user_id, channel_id, command_text) from a Slack event payload.

        Strips the leading <@BOT_ID> mention so commands like
        "@lockbot lock node1 2h" become "lock node1 2h".

        Returns:
            (user_id, channel_id, command_text)
        """
        event = msg_data.get("event", {})
        user_id = event.get("user", "")
        channel = event.get("channel", "")
        text = event.get("text", "") or ""
        # Remove <@UXXXXXXXX> mention prefix (any Slack user/bot ID)
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        return user_id, channel, text

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build a Slack chat.postMessage payload.

        Args:
            content: str, or list of str / (label, url) tuples.
            user_ids: List of user IDs (used as fallback channel if group_id absent).
            group_id: Channel ID to post to.

        Returns:
            dict with keys 'channel' and 'text'.
        """
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, tuple) and len(item) == 2:
                    label, url = item
                    parts.append(f"<{url}|{label}>")
                else:
                    parts.append(str(item))
            text = "".join(parts)
        else:
            text = str(content) if content is not None else ""

        channel = group_id or (user_ids[0] if user_ids else "")
        return {"channel": channel, "text": text}

    def send(self, msg: dict) -> list:
        """Send a message via Slack chat.postMessage.

        Args:
            msg: Dict as returned by build_reply.

        Returns:
            List of (status_code, response_text) tuples.
        """
        token = self._get_config("bot_token", "")
        if not token:
            logger.error("Slack Bot Token (bot_token) is not configured")
            return []

        client = WebClient(token=token)
        try:
            response = client.chat_postMessage(**msg)
            return [(200, str(response.data))]
        except SlackApiError as e:
            logger.error("Slack API error: %s", e.response["error"])
            return [(e.response.status_code, e.response["error"])]
        except Exception:
            logger.exception("Failed to send Slack message")
            return []

    def handle_webhook(self, bot, raw_form: dict, raw_args: dict, raw_body: bytes, headers: dict) -> tuple:
        """Handle a Slack HTTP callback event.

        1. Challenge handshake: body JSON has type=="url_verification" → verify sig + respond.
        2. Signature verification: X-Slack-Signature + X-Slack-Request-Timestamp + raw body.
        3. Parse payload (Slack uses plaintext JSON, no encryption).
        4. Extract command and execute.
        """
        from lockbot.core.message_adapter import _BAD_SIG, _DECRYPT_FAIL

        body_str = raw_body.decode("utf-8") if raw_body else ""
        ts = headers.get("x-slack-request-timestamp", "")
        sig = headers.get("x-slack-signature", "")

        # Step 1 — challenge handshake
        if raw_body:
            try:
                maybe_json = json.loads(body_str)
                if maybe_json.get("type") == "url_verification":
                    if self.verify_request(sig, timestamp=ts, body=body_str):
                        return maybe_json["challenge"], 200, {"event": "url_verification"}
                    return *_BAD_SIG, {"event": "url_verification_failed"}
            except (ValueError, KeyError):
                pass

        # Step 2 — signature verification
        if not self.verify_request(sig, timestamp=ts, body=body_str):
            return *_BAD_SIG, {"event": "signature_failed"}

        # Step 3 — parse payload
        msg_data = self.decrypt_payload(raw_body)
        if msg_data is None:
            return *_DECRYPT_FAIL, {"event": "decrypt_failed"}

        # Step 4 — execute
        return self._run_command(bot, msg_data, "Slack")
