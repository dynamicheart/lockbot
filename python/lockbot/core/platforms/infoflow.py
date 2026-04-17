"""
Infoflow (如流) platform adapter for LockBot.

Implements MessageAdapter for the Infoflow IM platform, handling:
- MD5 signature verification
- AES-ECB message decryption
- Infoflow message format (TEXT/AT/LINK body types)
- Webhook-based message sending with automatic text splitting
"""

import base64
import binascii
import hashlib
import json
import logging

import six
from Crypto.Cipher import AES

from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.request import post_webhook

logger = logging.getLogger(__name__)


# ── Infoflow crypto / format helpers ─────────────────────────────────────────


def _base64_urlsafe_decode(s):
    """Base64 decode with URL-safe character replacement and padding."""
    s = s.replace("-", "+").replace("_", "/") + "=" * (len(s) % 4)
    return base64.b64decode(s)


def _check_signature(signature, rn, timestamp, token):
    """Verify Infoflow request signature using MD5(rn + timestamp + token)."""
    md5 = hashlib.md5()
    md5.update(f"{rn}{timestamp}{token}".encode())
    return md5.hexdigest() == signature


def _extract_msg_body(body):
    """Extract text/link content from Infoflow message body, ignoring AT mentions."""
    rcv_info = ""
    for info in body:
        if info["type"] == "AT":
            pass
        elif info["type"] == "TEXT":
            rcv_info += info["content"]
        elif info["type"] == "LINK":
            rcv_info += info["label"]
        else:
            raise Exception("unknown message format" + str(info))
    return rcv_info


class _AESCipher:
    """AES encryption/decryption helper (Infoflow-specific)."""

    def __init__(self, key, mode=AES.MODE_ECB, padding="PKCS7", encode="base64", **kwargs):
        self.key = key
        self.mode = mode
        self.padding = padding
        self.encode = encode
        self.kwargs = kwargs
        self.bs = AES.block_size
        self.IV = self.kwargs.get("IV", None)
        if self.IV and self.mode in (AES.MODE_ECB, AES.MODE_CTR):
            raise TypeError("ECB and CTR mode does not use IV")

    def _aes(self):
        return AES.new(self.key, self.mode, **self.kwargs)

    def decrypt(self, ciphertext):
        """Decrypt ciphertext and return unpadded plaintext."""
        if not ciphertext:
            return None
        if self.padding == "PKCS7":
            if six.PY3:

                def unpad(s):
                    return s[0 : -s[-1]]
            else:

                def unpad(s):
                    return s[0 : -ord(s[-1])]
        else:

            def unpad(s):
                return s.rstrip("\x00")

        if isinstance(ciphertext, six.binary_type) and self.encode != "raw":
            ciphertext = ciphertext.decode("utf-8")
        if self.encode == "hex":
            ciphertext = binascii.unhexlify(ciphertext)
        if self.encode == "base64":
            ciphertext = _base64_urlsafe_decode(ciphertext)
        return unpad(self._aes().decrypt(ciphertext))


# ── Adapter ───────────────────────────────────────────────────────────────────


class InfoflowAdapter(MessageAdapter):
    """MessageAdapter implementation for the Infoflow (如流) platform."""

    def __init__(self, config=None):
        """
        Args:
            config: Config instance for platform credentials (TOKEN, AESKEY, WEBHOOK_URL).
                    If None, the global Config singleton is used for sending.
        """
        self.config = config

    def verify_request(self, signature: str, rn: str = None, timestamp: str = None, **kwargs) -> bool:
        """Verify request signature using MD5(rn + timestamp + TOKEN)."""
        token = self._get_config("TOKEN", "")
        return _check_signature(signature, rn, timestamp, token)

    def decrypt_payload(self, encrypted_data, **kwargs):
        """Decrypt an AES-encrypted message payload.

        Args:
            encrypted_data: Base64-encoded encrypted message bytes or string.

        Returns:
            Parsed message dict, or None on failure.
        """
        if not encrypted_data:
            return None

        aes_key = self._get_config("AESKEY", "")
        try:
            encrypter = _AESCipher(_base64_urlsafe_decode(aes_key))
            decrypted = encrypter.decrypt(encrypted_data)
            return json.loads(decrypted)
        except Exception as e:
            logger.warning("AES decrypt failed: %s", e)
            try:
                return json.loads(encrypted_data)
            except Exception:
                logger.exception("Failed to decrypt/parse message")
                return None

    def extract_command(self, msg_data: dict) -> tuple:
        """Extract user_id, group_id, and command text from an Infoflow message.

        Returns:
            (user_id, group_id, command_text) tuple.
        """
        user_id = msg_data["message"]["header"]["fromuserid"]
        group_id = msg_data["message"]["header"].get("toid", "")
        command_text = _extract_msg_body(msg_data["message"]["body"]).strip()
        return user_id, group_id, command_text

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build an Infoflow reply message.

        Args:
            content: str or list. If str, wrapped as a single TEXT body.
                If list, each item is:
                - str → TEXT body element
                - tuple(label, href) → TEXT(label) + LINK(href) body elements
            user_ids: List of user IDs to @mention.
            group_id: Optional group chat ID (toid) for the reply target.

        Returns:
            Infoflow message dict with TEXT, optional LINK, and AT body elements.
        """
        body = []
        items = [content] if isinstance(content, str) else content
        for item in items:
            if isinstance(item, str):
                body.append({"type": "TEXT", "content": item})
            elif isinstance(item, tuple):
                label, url = item
                body.append({"type": "TEXT", "content": label})
                body.append({"type": "LINK", "href": url})
                body.append({"type": "TEXT", "content": "\n"})
        body.append({"type": "AT", "atuserids": list(user_ids)})
        msg = {
            "message": {
                "header": {},
                "body": body,
            }
        }
        if group_id:
            msg["message"]["header"]["toid"] = group_id
        return msg

    def send(self, msg: dict) -> list:
        """Send a message via Infoflow webhook.

        Handles automatic text splitting for messages exceeding 2000 characters.

        Args:
            msg: Infoflow message dict (as returned by build_reply).

        Returns:
            List of (status_code, response_text) tuples.
        """
        return post_webhook(msg, config=self.config)

    def set_reply_target(self, reply: dict, group_id: str) -> dict:
        """Set the Infoflow reply target to the given group chat (toid)."""
        if group_id:
            reply["message"]["header"]["toid"] = group_id
        return reply

    def _get_config(self, key, default=None):
        """Get config value from instance config or global Config."""
        if self.config:
            return self.config.get_val(key, default)
        from lockbot.core.config import Config

        return Config.get(key, default)


# Public helpers — used by bots/router.py for Infoflow URL verification
def check_signature(signature, rn, timestamp, token):
    """Verify Infoflow request signature (public alias for backwards compat)."""
    return _check_signature(signature, rn, timestamp, token)
