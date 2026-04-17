"""
Abstract base class for platform-specific message handling.

Each IM platform (Infoflow, Slack, Discord, etc.) should implement
MessageAdapter to provide platform-specific message construction,
signature verification, payload decryption, and sending.
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

_OK = ("command succeed", 200)
_BAD_SIG = ("check signature fail", 401)
_DECRYPT_FAIL = ("decrypt failed", 400)
_PARSE_ERR = ("parse error", 400)


class MessageAdapter(ABC):
    """Platform-agnostic interface for bot message operations."""

    @abstractmethod
    def verify_request(self, signature: str, **kwargs) -> bool:
        """Verify the authenticity of an incoming request."""

    @abstractmethod
    def decrypt_payload(self, encrypted_data, **kwargs):
        """Decrypt an incoming message payload. Returns parsed message dict or None."""

    @abstractmethod
    def extract_command(self, msg_data: dict) -> tuple:
        """Extract (user_id, group_id, command_text) from a parsed message."""

    @abstractmethod
    def build_reply(self, content, user_ids, group_id=None) -> dict:
        """Build a platform-specific reply message.

        Args:
            content: Text content (str) or list of mixed items:
                - str → TEXT body element
                - tuple(str, str) → TEXT(label) + LINK(href) body elements
            user_ids: List of user IDs to mention/notify.
            group_id: Optional group/channel ID to send the reply to.

        Returns:
            Platform-specific message dict ready to be sent.
        """

    @abstractmethod
    def send(self, msg: dict) -> list:
        """Send a message via the platform's API.

        Args:
            msg: Platform-specific message dict (as returned by build_reply).

        Returns:
            List of (status_code, response_text) tuples.
        """

    @abstractmethod
    def handle_webhook(
        self,
        bot,
        raw_form: dict,
        raw_args: dict,
        raw_body: bytes,
        headers: dict,
    ) -> tuple:
        """Handle a full incoming HTTP webhook request for this platform.

        Each platform adapter owns its complete request lifecycle:
        challenge handshake → signature verification → decryption → command execution.

        Args:
            bot: Bot instance (has .adapter, .config attributes).
            raw_form: Parsed form data (application/x-www-form-urlencoded).
            raw_args: Query string parameters.
            raw_body: Raw request body bytes.
            headers: HTTP request headers (lower-cased keys).

        Returns:
            Tuple of (response_text, status_code, meta_dict).
        """

    def set_reply_target(self, reply: dict, group_id: str) -> dict:
        """Patch a reply dict so it is directed to the given group/channel.

        Called after build_reply when the group/channel ID is known.
        The default implementation is a no-op — override in platform adapters.

        Args:
            reply: Message dict as returned by build_reply.
            group_id: Group or channel ID to direct the reply to.

        Returns:
            The (possibly mutated) reply dict.
        """
        return reply

    def _run_command(self, bot, msg_data: dict, platform: str) -> tuple:
        """Extract command from msg_data, execute it, send reply.

        Returns:
            (response_text, status_code, meta_dict)
        """
        from lockbot.core.handler import execute_command

        try:
            user_id, group_id, command_text = bot.adapter.extract_command(msg_data)
        except Exception:
            logger.exception("Failed to extract command (%s)", platform)
            return *_PARSE_ERR, {"event": "parse_error"}

        meta = {"user_id": user_id, "group_id": group_id, "command": command_text}
        logger.debug("%s webhook bot %s: cmd=%s", platform, bot.config.get_val("BOT_NAME"), command_text)

        reply = execute_command(msg_data, bot)
        reply = bot.adapter.set_reply_target(reply, group_id)
        bot.adapter.send(reply)
        return *_OK, meta
