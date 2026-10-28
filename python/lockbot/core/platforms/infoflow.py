"""
Infoflow (如流) platform adapter for LockBot.

Implements MessageAdapter for the Infoflow IM platform, handling:
- MD5 signature verification
- AES-ECB message decryption
- Infoflow message format (TEXT/AT/LINK body types)
- Webhook-based message sending with automatic text splitting
"""

import json
import logging

import requests

from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.msg_utils import AESCipher, base64_urlsafe_decode, check_signature, extract_msg_body
from lockbot.core.request import post_webhook

logger = logging.getLogger(__name__)


class InfoflowAdapter(MessageAdapter):
    """MessageAdapter implementation for the Infoflow (如流) platform."""

    platform_name = "infoflow"

    def __init__(self, config=None):
        """
        Args:
            config: Config instance for platform credentials (TOKEN, AESKEY, WEBHOOK_URL).
                    If None, the global Config singleton is used for sending.
        """
        self.config = config

    def verify_request(self, signature: str, rn: str = None, timestamp: str = None) -> bool:
        """Verify request signature using MD5(rn + timestamp + TOKEN)."""
        token = self._get_config("TOKEN", "")
        return check_signature(signature, rn, timestamp, token)

    def decrypt_payload(self, encrypted_data, **kwargs):
        """Decrypt an AES-encrypted message payload, or refuse it.

        Fails closed. The callback signature is ``MD5(rn + timestamp + TOKEN)`` and so
        covers none of the body; successful decryption is therefore the only evidence that
        the body came from the platform. Parsing an undecryptable body as plaintext JSON
        would let anyone replaying a captured signature choose the sender identity, which
        downstream code treats as trusted.

        Args:
            encrypted_data: Base64-encoded encrypted message bytes or string.

        Returns:
            Parsed message dict, or None if the payload did not decrypt.
        """
        if not encrypted_data:
            return None

        aes_key = self._get_config("AESKEY", "")
        try:
            encrypter = AESCipher(base64_urlsafe_decode(aes_key))
            decrypted = encrypter.decrypt(encrypted_data)
            return json.loads(decrypted)
        except Exception as e:
            logger.warning("Refusing an Infoflow payload that did not decrypt: %s", e)
            return None

    def extract_command(self, msg_data: dict) -> tuple:
        """Extract user_id, group_id, and command text from an Infoflow message.

        Args:
            msg_data: Parsed Infoflow message dict.

        Returns:
            (user_id, group_id, command_text) tuple.
        """
        user_id = msg_data["message"]["header"]["fromuserid"]
        group_id = msg_data["message"]["header"].get("toid", "")
        command_text = extract_msg_body(msg_data["message"]["body"]).strip()
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
        if user_ids or not group_id:
            body.append({"type": "AT", "atuserids": list(user_ids)})
        msg = {
            "message": {
                "header": {},
                "body": body,
            }
        }
        if group_id:
            groups = group_id if isinstance(group_id, (list, tuple)) else [group_id]
            msg["message"]["header"]["toid"] = [int(group) for group in groups]
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

    def send_broadcast(self, content: str, group_id: str, mention_all: bool = False) -> tuple[str, str]:
        """Send once to an explicit group. Ambiguous delivery is never automatically retried.

        Infoflow AT uses ``atall: true`` (also represented in smallnest/infoflow msg.go).
        The broadcast queue owns pacing and tracks each group independently.
        """
        if not str(group_id).isdecimal() or int(group_id) <= 0:
            return "skipped", "invalid_group"
        payload = self.build_reply(content, [], group_id=group_id)
        if mention_all:
            payload["message"]["body"].append({"type": "AT", "atall": True})
        try:
            response = requests.post(
                self._get_config("WEBHOOK_URL"), json=payload, timeout=(3, 5), allow_redirects=False
            )
        except requests.RequestException:
            return "unknown", "delivery_uncertain"
        if not 200 <= response.status_code < 300:
            return "failed", "http_rejected"
        try:
            data = response.json()
        except ValueError:
            return "unknown", "unrecognized_response"
        if not isinstance(data, dict) or "errcode" not in data:
            return "unknown", "unrecognized_response"
        if str(data["errcode"]) != "0" or (isinstance(data.get("data"), dict) and data["data"].get("fail")):
            return "failed", "platform_rejected"
        return "accepted", "platform_accepted"

    def apply_reply_target(self, msg: dict, conversation_id) -> dict:
        """Set the Infoflow reply target (``toid``) on an already-built message."""
        if conversation_id:
            groups = conversation_id if isinstance(conversation_id, (list, tuple)) else [conversation_id]
            msg["message"]["header"]["toid"] = [int(group) for group in groups]
        return msg

    def extract_reply_text(self, msg) -> str:
        """Flatten an Infoflow reply into plain text, dropping AT mentions.

        Note this is not the inverse of :func:`extract_msg_body`: that one parses *inbound*
        messages, where a LINK carries a ``label``, while :meth:`build_reply` emits a LINK
        carrying only an ``href``. Unknown element types are skipped rather than raising —
        rendering a reply is a presentation concern, not a parsing contract.
        """
        if isinstance(msg, str):
            return msg
        if not isinstance(msg, dict):
            return ""
        parts = []
        for item in msg.get("message", {}).get("body", []) or []:
            kind = item.get("type")
            if kind == "TEXT":
                parts.append(item.get("content", ""))
            elif kind == "LINK":
                parts.append(item.get("href") or item.get("label") or "")
        return "".join(parts)

    def _get_config(self, key, default=None):
        """Get config value from instance config or fresh defaults."""
        if self.config is not None:
            return self.config.get_val(key, default)
        from lockbot.core.config import Config

        return Config().get_val(key, default)
