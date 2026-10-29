"""Verify Feishu HTTP envelopes before accepting any actor or conversation identity."""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


class InvalidFeishuEvent(ValueError):
    pass


@dataclass(frozen=True)
class FeishuIdentity:
    app_id: str
    tenant_key: str
    bot_open_id: str
    verification_token: str
    encrypt_key: str = ""


MEMBERSHIP_EVENTS = {
    "im.chat.member.bot.added_v1": "present",
    "im.chat.member.bot.deleted_v1": "removed",
}


def normalize_membership(data, identity: FeishuIdentity):
    """Only consume a verified app-recipient event, never an arbitrary bot/user join.

    Official bot-membership events describe the receiving application's bot and
    contain no separate added-bot ID. Trust comes from HTTP verification or the
    private official-SDK channel plus the exact app/tenant header match.
    """
    try:
        header = data["header"]
        if header.get("event_type") not in MEMBERSHIP_EVENTS:
            return None
        if (
            not identity.tenant_key
            or header.get("app_id") != identity.app_id
            or header.get("tenant_key") != identity.tenant_key
        ):
            raise ValueError()
        event = data["event"]
        if event.get("external") is not False:
            return None  # External groups are outside this internal-tenant feature.
        operator_tenant = event.get("operator_tenant_key")
        if operator_tenant and operator_tenant != identity.tenant_key:
            raise ValueError()
        chat_id, event_id = event["chat_id"], header["event_id"]
        if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]{1,125}", chat_id):
            raise ValueError()
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
            raise ValueError()
        timestamp = int(header["create_time"])
        if abs(time.time() - timestamp / 1000) > 600:
            raise ValueError()
        return {
            "group_id": chat_id,
            "event_id": event_id,
            "event_time_ms": timestamp,
            "status": MEMBERSHIP_EVENTS[header["event_type"]],
        }
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise InvalidFeishuEvent("Invalid bot membership event") from None


def verify_http(body: bytes, headers, identity: FeishuIdentity, *, now=None):
    """Fail closed; an encrypted application never accepts a plaintext downgrade.

    URL verification uses the same configured token and encrypted-envelope policy.
    Regular encrypted events additionally require a fresh raw-body signature.
    """
    if not identity.verification_token or len(body) > 256 * 1024:
        raise InvalidFeishuEvent("Invalid callback configuration or size")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        if identity.encrypt_key:
            encoded = data["encrypt"]
            decoded = base64.b64decode(encoded, validate=True)
            key = hashlib.sha256(identity.encrypt_key.encode()).digest()
            plain = unpad(AES.new(key, AES.MODE_CBC, decoded[:16]).decrypt(decoded[16:]), 16)
            data = json.loads(plain)
        elif "encrypt" in data:
            raise ValueError()
        if not isinstance(data, dict):
            raise ValueError()
        header = data.get("header", {})
        token = data.get("token") if data.get("type") == "url_verification" else header.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, identity.verification_token):
            raise ValueError()
        if data.get("type") == "url_verification":
            challenge = data.get("challenge")
            if not isinstance(challenge, str) or not challenge or len(challenge) > 4096:
                raise ValueError()
            return {"challenge": challenge}
        if identity.encrypt_key:
            timestamp = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            signature = headers.get("x-lark-signature", "")
            if not timestamp or not nonce or abs((time.time() if now is None else now) - int(timestamp)) > 300:
                raise ValueError()
            expected = hashlib.sha256((timestamp + nonce + identity.encrypt_key).encode() + body).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                raise ValueError()
        if header.get("app_id") != identity.app_id or header.get("tenant_key") != identity.tenant_key:
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise InvalidFeishuEvent("Callback verification failed") from None


def normalize_message(data, identity: FeishuIdentity):
    """Ignore DMs, non-user events and messages that do not mention this exact bot.

    Mentions of other people are preserved as text rather than silently erased.
    Normalized user IDs are Open IDs scoped to the configured app and tenant.
    """
    try:
        header, event = data["header"], data["event"]
        if header.get("event_type") != "im.message.receive_v1":
            return None
        if header.get("app_id") != identity.app_id or header.get("tenant_key") != identity.tenant_key:
            raise InvalidFeishuEvent("Event identity mismatch")
        sender, message = event["sender"], event["message"]
        if sender.get("sender_type") != "user" or message.get("chat_type") != "group":
            return None
        if sender.get("tenant_key") != identity.tenant_key:
            raise InvalidFeishuEvent("Sender tenant mismatch")
        if message.get("message_type") != "text":
            return None
        user_id, chat_id = sender["sender_id"]["open_id"], message["chat_id"]
        if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", user_id) or user_id == identity.bot_open_id:
            return None
        if not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat_id):
            raise ValueError()
        text = json.loads(message["content"])["text"]
        if not isinstance(text, str) or len(text) > 8192:
            raise ValueError()
        addressed = False
        for mention in message.get("mentions", []):
            key = mention.get("key", "")
            if mention.get("id", {}).get("open_id") == identity.bot_open_id and key and key in text:
                text = text.replace(key, "")
                addressed = True
        if not addressed:
            return None
        message_id = message["message_id"]
        # Once durable claims expire, old signed/verified events must still be refused.
        if abs(time.time() - int(message["create_time"]) / 1000) > 600:
            raise ValueError()
        if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
            raise ValueError()
        return {"user_id": user_id, "group_id": chat_id, "command": text.strip(), "message_id": message_id}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise InvalidFeishuEvent("Invalid message event") from None
