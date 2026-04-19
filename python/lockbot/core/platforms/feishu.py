"""
Feishu (飞书) platform adapter for LockBot.

Implements MessageAdapter for Feishu enterprise bots (HTTP webhook mode).
Feishu sends HTTP POST events to our /api/bots/webhook/{bot_id} endpoint.

Reference docs:
  - feishu1.md: Callback configuration, challenge handshake, Encrypt Key
  - feishu2.md: Signature verification and AES-256-CBC decryption
  - feishu.md:  Message content formats

Credential mapping (reuses existing DB fields):
    webhook_url → App ID       — identifies the application; used to fetch tenant_access_token
    token       → App Secret   — authenticates the application; used to fetch tenant_access_token
    aes_key     → Encrypt Key  — optional; if set, enables AES-256-CBC decryption
                                 and SHA-256 signature verification of incoming events

If aes_key (Encrypt Key) is empty:
  - Incoming requests are accepted without signature verification
  - Event bodies are expected as plaintext JSON

If aes_key (Encrypt Key) is set:
  - Incoming bodies arrive as {"encrypt": "<base64-ciphertext>"}
  - Signature: sha256(timestamp + nonce + encrypt_key + raw_body_bytes) == X-Lark-Signature
  - Payload is AES-256-CBC decrypted (key=sha256(encrypt_key), iv=first 16 bytes of decoded data)
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
        code = data.get("code")
        if code != 0:
            logger.error("[Feishu] tenant_access_token failed: app_id=%s, resp=%s", app_id, data)
            return ""
        token = data["tenant_access_token"]
        expire = int(data.get("expire", 7200))
        _TOKEN_CACHE[key] = (token, time.time() + expire)
        logger.debug("[Feishu] tenant_access_token obtained: app_id=%s, expire_in=%ds", app_id, expire)
        return token
    except Exception:
        logger.exception("[Feishu] Exception fetching tenant_access_token: app_id=%s", app_id)
        return ""


def _decrypt_aes_cbc(encrypt_key: str, encrypted_b64: str) -> dict | None:
    """AES-256-CBC decrypt a Feishu encrypted payload.

    Per feishu2.md:
      key = SHA256(encrypt_key)          # 32 bytes
      buf = base64_decode(encrypted_b64)
      iv  = buf[:16]
      ciphertext = buf[16:]
      plaintext = AES-CBC-decrypt(ciphertext, key, iv)  with PKCS7 unpadding
    """
    try:
        from Crypto.Cipher import AES  # pycryptodome

        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        buf = base64.b64decode(encrypted_b64)
        iv = buf[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(buf[16:])
        # PKCS7 unpad
        pad_len = decrypted[-1]
        decrypted = decrypted[:-pad_len]
        return json.loads(decrypted.decode("utf-8"))
    except Exception:
        logger.exception("Failed to AES-decrypt Feishu payload")
        return None


class FeishuAdapter(MessageAdapter):
    """MessageAdapter implementation for Feishu enterprise bots (HTTP webhook mode)."""

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

    def verify_request(self, signature: str = None, timestamp: str = None, body: bytes | str = None, **kwargs) -> bool:
        """Verify Feishu request signature.

        Per feishu2.md (签名校验), when Encrypt Key is configured:
            content = (timestamp + nonce + encrypt_key).encode('utf-8') + raw_body_bytes
            expected = sha256(content).hexdigest()
            compare with X-Lark-Signature header

        If no Encrypt Key is configured (webhook_url empty), skip verification and
        return True — Feishu sends plaintext events without a signature in that case.
        """
        encrypt_key = self._get_config("AESKEY", "")
        if not encrypt_key:
            # No Encrypt Key configured → accept all requests (no signature to check)
            logger.debug("[Feishu] verify_request: no encrypt_key configured, skipping verification")
            return True

        if not signature or not timestamp:
            logger.warning(
                "[Feishu] verify_request: missing signature or timestamp (sig=%s, ts=%s)", signature, timestamp
            )
            return False

        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > _MAX_TIMESTAMP_AGE:
                logger.warning(
                    "[Feishu] verify_request: timestamp too old: %s (age=%ds)", timestamp, abs(time.time() - ts)
                )
                return False
        except (ValueError, TypeError):
            return False

        nonce = kwargs.get("nonce", "")
        # Build content: string part + raw body bytes
        string_part = (timestamp + nonce + encrypt_key).encode("utf-8")
        if isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = (body or "").encode("utf-8")
        content = string_part + body_bytes
        expected = hashlib.sha256(content).hexdigest()

        match = hmac.compare_digest(expected, signature)
        if not match:
            logger.warning(
                "[Feishu] verify_request: signature mismatch: expected=%s, got=%s, ts=%s, nonce=%s, body_len=%d",
                expected[:16],
                signature[:16] if signature else "None",
                timestamp,
                nonce,
                len(body_bytes),
            )
        else:
            logger.debug("[Feishu] verify_request: signature OK")
        return match

    def decrypt_payload(self, encrypted_data: bytes | str, **kwargs) -> dict | None:
        """Parse or decrypt a Feishu event body.

        Without Encrypt Key: body is plaintext JSON → just parse.
        With Encrypt Key: body is {"encrypt": "<base64>"} → AES-256-CBC decrypt.
        """
        if not encrypted_data:
            logger.warning("[Feishu] decrypt_payload: empty payload")
            return None

        if isinstance(encrypted_data, bytes):
            body_str = encrypted_data.decode("utf-8")
        else:
            body_str = encrypted_data

        try:
            data = json.loads(body_str)
        except Exception:
            logger.warning(
                "[Feishu] decrypt_payload: invalid JSON, body_len=%d, body_preview=%s", len(body_str), body_str[:200]
            )
            return None

        if "encrypt" in data:
            encrypt_key = self._get_config("AESKEY", "")
            if not encrypt_key:
                logger.warning("[Feishu] decrypt_payload: encrypted payload received but AES_KEY is not configured")
                return None
            logger.debug("[Feishu] decrypt_payload: decrypting AES-CBC encrypted payload")
            result = _decrypt_aes_cbc(encrypt_key, data["encrypt"])
            if result is None:
                logger.error("[Feishu] decrypt_payload: AES-CBC decryption returned None")
            return result

        logger.debug(
            "[Feishu] decrypt_payload: plaintext JSON, event_type=%s", data.get("header", {}).get("event_type", "N/A")
        )
        return data

    def extract_command(self, msg_data: dict) -> tuple:
        """Extract (user_id, conversation_id, command_text) from a Feishu event.

        Feishu v2 event structure (im.message.receive_v1):
        {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1", ...},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_...", "user_id": "..."}},
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
        text = re.sub(r"<at[^>]*>[^<]*</at>", "", text).strip()

        return user_id, chat_id, text

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build a Feishu text message payload.

        Feishu send message format (feishu.md):
            POST /open-apis/im/v1/messages?receive_id_type=chat_id
            body: {"receive_id": "<chat_id>", "msg_type": "text", "content": "{...}"}

        Hyperlinks use Markdown syntax: [label](url)
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

        app_id = self._get_config("WEBHOOK_URL", "")
        app_secret = self._get_config("TOKEN", "")

        token = _get_tenant_access_token(app_id, app_secret)
        if not token:
            logger.error("[Feishu] send: tenant_access_token unavailable — cannot send reply")
            return []

        url = f"{_FEISHU_MSG_URL}?receive_id_type={receive_id_type}"
        logger.debug(
            "[Feishu] send: receive_id_type=%s, receive_id=%s, msg_type=%s",
            receive_id_type,
            msg.get("receive_id", ""),
            msg.get("msg_type", ""),
        )
        try:
            resp = requests.post(
                url,
                json=msg,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=10,
            )
            status = resp.status_code
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = resp.text
            logger.debug("[Feishu] send response: status=%d, body=%s", status, resp_json)
            if status != 200:
                logger.warning("[Feishu] send failed: status=%d, body=%s", status, resp_json)
            return [(status, resp.text)]
        except Exception:
            logger.exception("[Feishu] send: exception while sending message")
            return []

    def handle_webhook(self, bot, raw_form: dict, raw_args: dict, raw_body: bytes, headers: dict) -> tuple:
        """Handle a Feishu HTTP callback event.

        Per feishu1.md / feishu2.md:
        1. Challenge handshake: body has type=="url_verification" → respond immediately
           with JSON {"challenge": "..."} before any signature check (must be <1 s).
        2. Signature verification (only when Encrypt Key / aes_key is configured):
           sha256(timestamp + nonce + encrypt_key + raw_body_bytes) == X-Lark-Signature
        3. Decrypt payload: plaintext JSON or {"encrypt": "..."} AES-256-CBC.
        4. Extract command and execute.
        """
        from lockbot.core.message_adapter import _BAD_SIG, _DECRYPT_FAIL

        body_str = raw_body.decode("utf-8") if raw_body else ""
        bot_name = self._get_config("BOT_NAME", "?")

        logger.debug(
            "[Feishu] handle_webhook: bot=%s, body_len=%d, headers={%s}",
            bot_name,
            len(body_str),
            ", ".join(f"{k}={v}" for k, v in sorted(headers.items()) if k.startswith("x-lark-")),
        )

        # Step 1 — challenge handshake (must respond before signature check)
        if raw_body:
            try:
                maybe_json = json.loads(body_str)
                if maybe_json.get("type") == "url_verification":
                    challenge = maybe_json.get("challenge", "")
                    logger.debug("[Feishu] handle_webhook: challenge handshake OK, bot=%s", bot_name)
                    return json.dumps({"challenge": challenge}), 200, {"event": "url_verification"}
            except (ValueError, KeyError):
                pass

        # Step 2 — signature verification
        ts = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        sig = headers.get("x-lark-signature", "")
        encrypt_key = self._get_config("AESKEY", "")
        logger.debug(
            "[Feishu] handle_webhook: verifying sig, encrypt_key=%s, ts=%s, nonce=%s, sig=%s",
            "configured" if encrypt_key else "empty",
            ts,
            nonce,
            (sig[:16] + "...") if sig else "None",
        )
        if not self.verify_request(sig, timestamp=ts, nonce=nonce, body=raw_body):
            logger.warning("[Feishu] handle_webhook: signature verification FAILED, bot=%s", bot_name)
            return *_BAD_SIG, {"event": "signature_failed"}

        # Step 3 — decrypt / parse payload
        msg_data = self.decrypt_payload(raw_body)
        if msg_data is None:
            logger.warning("[Feishu] handle_webhook: decrypt/parse FAILED, bot=%s", bot_name)
            return *_DECRYPT_FAIL, {"event": "decrypt_failed"}

        # Log the event type and key fields for debugging
        event_type = msg_data.get("header", {}).get("event_type", "N/A")
        logger.debug("[Feishu] handle_webhook: payload OK, event_type=%s, bot=%s", event_type, bot_name)

        # Step 4 — execute
        result = self._run_command(bot, msg_data, "Feishu")
        logger.debug(
            "[Feishu] handle_webhook: command done, bot=%s, meta=%s", bot_name, result[2] if len(result) > 2 else {}
        )
        return result
