"""
Feishu (飞书) platform adapter for LockBot.

Implements MessageAdapter for Feishu enterprise bots, handling:
- Request verification via X-Lark-Signature HMAC-SHA256 header
- Challenge handshake (url_verification event type)
- JSON event payload (no encryption in v2 event framework)
- Send text messages via POST /open-apis/im/v1/messages with tenant_access_token
- Token caching (2-hour TTL, refreshed on expiry)

Credential mapping (reuses existing DB fields):
    token       → App Secret  — for HMAC-SHA256 request verification
    aes_key     → App ID      — for fetching tenant_access_token
    webhook_url → (not used;  displayed to user as Event Subscription callback URL)
"""

import hashlib
import hmac
import json
import logging
import time

import requests

from lockbot.core.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)

# Reject requests older than this many seconds (Feishu uses 5-minute window)
_MAX_TIMESTAMP_AGE = 300

# tenant_access_token cache: keyed by (app_id, app_secret) → (token, expire_at)
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


def _get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """Fetch (or return cached) tenant_access_token for the given app credentials."""
    key = (app_id, app_secret)
    cached = _TOKEN_CACHE.get(key)
    if cached:
        token, expire_at = cached
        # Refresh 60 s before expiry
        if time.time() < expire_at - 60:
            return token

    try:
        resp = requests.post(
            _FEISHU_TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Failed to get Feishu tenant_access_token: %s", data)
            return ""
        token = data["tenant_access_token"]
        expire = int(data.get("expire", 7200))
        _TOKEN_CACHE[key] = (token, time.time() + expire)
        return token
    except Exception:
        logger.exception("Exception fetching Feishu tenant_access_token")
        return ""


class FeishuAdapter(MessageAdapter):
    """MessageAdapter implementation for Feishu enterprise bots."""

    def __init__(self, config=None):
        self.config = config

    def _get_config(self, key: str, default: str = "") -> str:
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
        """Verify Feishu request via X-Lark-Signature.

        Feishu signs requests as:
            HMAC-SHA256(app_secret, timestamp + nonce + body_string)
        where nonce comes from the request header X-Lark-Nonce.
        The hex digest is compared against X-Lark-Signature.
        """
        if not signature or not timestamp:
            return False

        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > _MAX_TIMESTAMP_AGE:
                logger.warning("Feishu request timestamp too old: %s", timestamp)
                return False
        except (ValueError, TypeError):
            return False

        app_secret = self._get_config("TOKEN", "")
        nonce = kwargs.get("nonce", "")
        string_to_sign = f"{timestamp}{nonce}{body or ''}"
        mac = hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        )
        expected = mac.hexdigest()
        return hmac.compare_digest(expected, signature)

    def decrypt_payload(self, encrypted_data, **kwargs):
        """Parse Feishu JSON payload (no encryption in HTTP callback v2)."""
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
        """Extract (user_id, conversation_id, command_text) from a Feishu event.

        Feishu v2 event structure (im.message.receive_v1):
        {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1", ...},
            "event": {
                "sender": {"sender_id": {"open_id": "...", "user_id": "..."}},
                "message": {
                    "chat_id": "oc_...",
                    "message_type": "text",
                    "content": "{\"text\": \"@bot hello\"}"
                }
            }
        }
        """
        event = msg_data.get("event", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        user_id = sender_id.get("open_id", "") or sender_id.get("user_id", "")

        message = event.get("message", {})
        chat_id = message.get("chat_id", "")

        text = ""
        msg_type = message.get("message_type", "")
        if msg_type == "text":
            raw_content = message.get("content", "{}")
            try:
                content_obj = json.loads(raw_content)
                text = content_obj.get("text", "")
            except Exception:
                text = ""

        # Strip @mention tags: <at user_id="xxx">name</at>
        import re

        text = re.sub(r"<at[^>]*>[^<]*</at>", "", text).strip()

        return user_id, chat_id, text

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build a Feishu text message payload.

        Feishu send message format:
            POST /open-apis/im/v1/messages?receive_id_type=chat_id
            body: {"receive_id": "<chat_id>", "msg_type": "text", "content": "{...}"}
        """
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, tuple) and len(item) == 2:
                    label, url = item
                    parts.append(f"[{label}]({url})")
                else:
                    parts.append(str(item))
            text = "".join(parts)
        else:
            text = str(content) if content is not None else ""

        # receive_id will be set by set_reply_target; default to first user_id
        receive_id = group_id or (user_ids[0] if user_ids else "")
        receive_id_type = "chat_id" if group_id else "open_id"

        return {
            "_feishu_receive_id_type": receive_id_type,
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }

    def set_reply_target(self, reply: dict, group_id: str) -> dict:
        """Set the reply target to the group chat."""
        if group_id:
            reply["receive_id"] = group_id
            reply["_feishu_receive_id_type"] = "chat_id"
        return reply

    def send(self, msg: dict) -> list:
        """Send a message via Feishu REST API."""
        receive_id_type = msg.pop("_feishu_receive_id_type", "chat_id")

        app_id = self._get_config("AES_KEY", "")
        app_secret = self._get_config("TOKEN", "")

        token = _get_tenant_access_token(app_id, app_secret)
        if not token:
            logger.error("Feishu tenant_access_token unavailable — cannot send reply")
            return []

        url = f"{_FEISHU_MSG_URL}?receive_id_type={receive_id_type}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            resp = requests.post(url, json=msg, headers=headers, timeout=10)
            return [(resp.status_code, resp.text)]
        except Exception:
            logger.exception("Failed to send Feishu message")
            return []
