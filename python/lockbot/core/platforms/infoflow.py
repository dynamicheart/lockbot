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

from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.msg_utils import AESCipher, base64_urlsafe_decode, check_signature, extract_msg_body
from lockbot.core.request import post_webhook

logger = logging.getLogger(__name__)


class InfoflowAdapter(MessageAdapter):
    """MessageAdapter implementation for the Infoflow (如流) platform."""

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
            encrypter = AESCipher(base64_urlsafe_decode(aes_key))
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

        Args:
            msg_data: Parsed Infoflow message dict.

        Returns:
            (user_id, group_id, command_text) tuple.
        """
        user_id = msg_data["message"]["header"]["fromuserid"]
        group_id = msg_data["message"]["header"].get("toid", "")
        command_text = extract_msg_body(msg_data["message"]["body"]).strip()
        return user_id, group_id, command_text

    def build_reply(self, content: str, user_ids: list, group_id: str = None) -> dict:
        """Build an Infoflow reply message.

        Args:
            content: Text content of the reply.
            user_ids: List of user IDs to @mention.
            group_id: Optional group chat ID (toid) for the reply target.

        Returns:
            Infoflow message dict with TEXT and AT body elements.
        """
        msg = {
            "message": {
                "header": {},
                "body": [
                    {"type": "TEXT", "content": content},
                    {"type": "AT", "atuserids": list(user_ids)},
                ],
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

    def _get_config(self, key, default=None):
        """Get config value from instance config or global Config."""
        if self.config:
            return self.config.get_val(key, default)
        from lockbot.core.config import Config

        return Config.get(key, default)
