"""
DingTalk (钉钉) platform adapter for LockBot.

Implements MessageAdapter for DingTalk enterprise bots, handling:
- HMAC-SHA256 signature verification (header: timestamp + sign)
- Plain JSON message parsing (no encryption)
- DingTalk text message format
- Reply via per-message sessionWebhook (no static webhook URL needed)

Credential mapping (reuses existing DB fields):
    token       → App Secret  — for HMAC-SHA256 signature verification
    aes_key     → (not used)
    webhook_url → (not used; DingTalk provides sessionWebhook per message)
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time

import requests

from lockbot.core.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)

# Reject requests older than this many seconds
_MAX_TIMESTAMP_AGE = 3600  # DingTalk spec: 1 hour


class DingTalkAdapter(MessageAdapter):
    """MessageAdapter implementation for DingTalk enterprise bots."""

    def __init__(self, config=None):
        self.config = config
        # Per-message sessionWebhook stored during extract_command for use in send()
        self._session_webhook: str = ""

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

    def verify_request(self, signature: str = None, timestamp: str = None, body: str = None, **kwargs) -> bool:
        """Verify DingTalk request signature.

        DingTalk sends timestamp and sign in HTTP headers.
        sign = Base64(HMAC-SHA256(timestamp + "\\n" + app_secret))
        """
        if not signature or not timestamp:
            return False

        try:
            ts_ms = int(timestamp)
            # timestamp is in milliseconds
            if abs(time.time() * 1000 - ts_ms) > _MAX_TIMESTAMP_AGE * 1000:
                logger.warning("DingTalk request timestamp too old: %s", timestamp)
                return False
        except (ValueError, TypeError):
            return False

        app_secret = self._get_config("TOKEN", "")
        string_to_sign = f"{timestamp}\n{app_secret}"
        mac = hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        )
        expected = base64.b64encode(mac.digest()).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def decrypt_payload(self, encrypted_data, **kwargs):
        """Parse DingTalk JSON payload (no encryption)."""
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
        """Extract (user_id, conversation_id, command_text) from a DingTalk message.

        Also saves sessionWebhook for use in send().
        Strips leading @bot mention from the text content.
        """
        user_id = msg_data.get("senderId", "") or msg_data.get("senderStaffId", "")
        conversation_id = msg_data.get("conversationId", "")

        # Save per-message reply webhook
        self._session_webhook = msg_data.get("sessionWebhook", "")

        text = ""
        if msg_data.get("msgtype") == "text":
            text = msg_data.get("text", {}).get("content", "") or ""

        # Strip leading @mentions (DingTalk includes them in text)
        text = re.sub(r"@\S+\s*", "", text).strip()
        return user_id, conversation_id, text

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build a DingTalk text message payload.

        DingTalk reply format: {"msgtype": "text", "text": {"content": "..."}}
        """
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, tuple) and len(item) == 2:
                    label, url = item
                    parts.append(f"{label} {url}")
                else:
                    parts.append(str(item))
            text = "".join(parts)
        else:
            text = str(content) if content is not None else ""

        return {
            "_dingtalk_session_webhook": self._session_webhook,
            "msgtype": "text",
            "text": {"content": text},
        }

    def set_reply_target(self, reply: dict, group_id: str) -> dict:
        """DingTalk uses sessionWebhook (set during extract_command), not group_id."""
        return reply

    def send(self, msg: dict) -> list:
        """Send a message via DingTalk sessionWebhook.

        The sessionWebhook URL is embedded in the message dict by build_reply.
        """
        webhook = msg.pop("_dingtalk_session_webhook", "") or self._session_webhook
        if not webhook:
            logger.error("DingTalk sessionWebhook is not set — cannot send reply")
            return []

        try:
            resp = requests.post(webhook, json=msg, timeout=10)
            return [(resp.status_code, resp.text)]
        except Exception:
            logger.exception("Failed to send DingTalk message")
            return []
