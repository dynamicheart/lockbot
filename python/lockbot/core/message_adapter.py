"""
Abstract base class for platform-specific message handling.

Each IM platform (Infoflow, Slack, Discord, etc.) should implement
MessageAdapter to provide platform-specific message construction,
signature verification, payload decryption, and sending.
"""

from abc import ABC, abstractmethod


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
