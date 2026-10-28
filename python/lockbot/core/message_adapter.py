"""
Abstract base class for platform-specific message handling.

Each IM platform (Infoflow, Slack, Discord, etc.) should implement
MessageAdapter to provide platform-specific message construction,
signature verification, payload decryption, and sending.
"""

from abc import ABC, abstractmethod
from typing import ClassVar


class MessageAdapter(ABC):
    """Platform-agnostic interface for bot message operations."""

    #: Normalized platform identifier (lowercase, no padding), e.g. ``"infoflow"``.
    #: Layers above the adapter use this instead of inspecting the adapter class, and
    #: :mod:`lockbot.core.platforms.registry` keys its registry on it.
    platform_name: ClassVar[str] = ""

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
    def apply_reply_target(self, msg: dict, conversation_id) -> dict:
        """Direct an already-built reply at a conversation, in place.

        A bot builds its reply without knowing which conversation the triggering message
        came from, so the transport layer has to retarget it afterwards. Doing that through
        the adapter keeps the caller free of any platform-specific message layout.

        Args:
            msg: Platform-specific message dict (as returned by build_reply).
            conversation_id: Target conversation, as returned by extract_command. A falsy
                value leaves the message untouched, so it goes to the default destination.

        Returns:
            The same msg, retargeted.
        """

    @abstractmethod
    def extract_reply_text(self, msg) -> str:
        """Flatten a built reply back into plain text.

        The HTTP API answers with text, never with a platform message dict, so it needs a
        platform-neutral way to read what a bot method produced. Mentions are dropped; they
        are a chat-presentation concern.

        Args:
            msg: Platform-specific message dict, or a plain string (some bot paths return
                one directly).

        Returns:
            The human-readable text of the reply.
        """
