"""A non-Infoflow MessageAdapter test double.

Exists to prove the platform boundary (AC-8): the dispatcher, the adapter factory, and
everything layered above them must work without any Infoflow payload shape and without
any Infoflow field name. Nothing here uses ``message``/``header``/``body``/``fromuserid``/
``toid``/``atuserids``; the envelope is deliberately shaped differently so that an
accidental dependency on the Infoflow layout fails loudly.
"""

import json

from lockbot.core.message_adapter import MessageAdapter

VALID_SIGNATURE = "fake-valid-signature"


class FakeAdapter(MessageAdapter):
    """Minimal in-memory adapter for a fictional ``fakeim`` platform."""

    platform_name = "fakeim"

    def __init__(self, config=None):
        self.config = config
        self.sent: list[dict] = []

    def verify_request(self, signature: str, **kwargs) -> bool:
        return signature == VALID_SIGNATURE

    def decrypt_payload(self, encrypted_data, **kwargs):
        if not encrypted_data:
            return None
        if isinstance(encrypted_data, bytes):
            encrypted_data = encrypted_data.decode("utf-8")
        try:
            return json.loads(encrypted_data)
        except ValueError:
            return None

    def extract_command(self, msg_data: dict) -> tuple:
        envelope = msg_data["envelope"]
        return envelope["actor"], envelope.get("conversation", ""), envelope["text"].strip()

    def build_reply(self, content, user_ids, group_id=None) -> dict:
        items = [content] if isinstance(content, str) else list(content)
        return {
            "envelope": {
                "segments": items,
                "notify": list(user_ids),
                "conversation": group_id,
            }
        }

    def send(self, msg: dict) -> list:
        self.sent.append(msg)
        return [(200, "accepted")]

    def apply_reply_target(self, msg: dict, conversation_id) -> dict:
        if conversation_id:
            msg["envelope"]["conversation"] = conversation_id
        return msg

    def extract_reply_text(self, msg) -> str:
        if isinstance(msg, str):
            return msg
        if not isinstance(msg, dict):
            return ""
        parts = []
        for segment in msg.get("envelope", {}).get("segments", []) or []:
            if isinstance(segment, str):
                parts.append(segment)
            elif isinstance(segment, tuple):
                parts.append(segment[0])
        return "".join(parts)


def make_message(text, actor="fake-actor-1", conversation="fake-conversation-1") -> dict:
    """Build a ``fakeim`` inbound message carrying ``text``."""
    return {"envelope": {"actor": actor, "conversation": conversation, "text": text}}


def encode_message(text, **kwargs) -> bytes:
    """Serialize a ``fakeim`` inbound message the way the transport would deliver it."""
    return json.dumps(make_message(text, **kwargs)).encode("utf-8")
