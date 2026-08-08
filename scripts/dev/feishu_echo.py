#!/usr/bin/env python3
"""Minimal Feishu webhook diagnostic and echo bot.

This program is intentionally independent from LockBot's Feishu adapter and
database. It is a reference implementation for validating the complete Feishu
HTTP callback flow through a local server (usually exposed with ngrok).

Examples:
    python scripts/dev/feishu_echo.py --env-file scripts/dev/feishu.env serve
    python scripts/dev/feishu_echo.py --env-file scripts/dev/feishu.env send \
        --receive-id-type open_id --receive-id ou_xxx --text "hello"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger("feishu_echo")
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
MAX_TIMESTAMP_AGE_SECONDS = 300
RECENT_EVENT_TTL_SECONDS = 600
REDACTED_FIELDS = {"app_secret", "authorization", "encrypt_key", "tenant_access_token", "token"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env_file(path: str | None) -> None:
    """Load a simple KEY=VALUE file without adding a dotenv dependency."""
    if not path:
        return

    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise FileNotFoundError(f"environment file does not exist: {env_path}")

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"invalid environment entry at {env_path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment key at {env_path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Settings:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    log_raw: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_id=os.environ.get("FEISHU_APP_ID", "").strip(),
            app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip(),
            verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", "").strip(),
            log_raw=_env_bool("FEISHU_LOG_RAW"),
        )


@dataclass
class RuntimeState:
    token: str = ""
    token_expires_at: float = 0
    recent_events: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def claim_event(self, event_id: str) -> bool:
        if not event_id:
            return True
        now = time.time()
        with self.lock:
            expired = [key for key, seen_at in self.recent_events.items() if now - seen_at > RECENT_EVENT_TTL_SECONDS]
            for key in expired:
                self.recent_events.pop(key, None)
            if event_id in self.recent_events:
                return False
            self.recent_events[event_id] = now
            return True


def compute_signature(encrypt_key: str, timestamp: str, nonce: str, body: bytes) -> str:
    content = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    return hashlib.sha256(content).hexdigest()


def verify_signature(encrypt_key: str, headers: dict[str, str], body: bytes) -> tuple[bool, str]:
    if not encrypt_key:
        return True, "disabled"

    timestamp = headers.get("x-lark-request-timestamp", "")
    nonce = headers.get("x-lark-request-nonce", "")
    supplied = headers.get("x-lark-signature", "")
    if not timestamp or not supplied:
        return False, "missing signature headers"

    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False, "invalid timestamp"
    if age > MAX_TIMESTAMP_AGE_SECONDS:
        return False, "expired timestamp"

    expected = compute_signature(encrypt_key, timestamp, nonce, body)
    if not hmac.compare_digest(expected, supplied):
        return False, "signature mismatch"
    return True, "ok"


def decrypt_event(encrypt_key: str, encrypted_b64: str) -> dict[str, Any]:
    if not encrypt_key:
        raise ValueError("encrypted event received but FEISHU_ENCRYPT_KEY is empty")

    from Crypto.Cipher import AES

    encrypted = base64.b64decode(encrypted_b64, validate=True)
    if len(encrypted) < 32 or len(encrypted) % AES.block_size != 0:
        raise ValueError("invalid encrypted event length")
    iv = encrypted[: AES.block_size]
    ciphertext = encrypted[AES.block_size :]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    pad_length = plaintext[-1]
    if pad_length < 1 or pad_length > AES.block_size:
        raise ValueError("invalid PKCS7 padding")
    if plaintext[-pad_length:] != bytes([pad_length]) * pad_length:
        raise ValueError("invalid PKCS7 padding")
    decoded = json.loads(plaintext[:-pad_length].decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("decrypted Feishu event must be a JSON object")
    return decoded


def decode_event(raw_body: bytes, encrypt_key: str) -> dict[str, Any]:
    envelope = json.loads(raw_body)
    if not isinstance(envelope, dict):
        raise ValueError("Feishu callback body must be a JSON object")
    if "encrypt" in envelope:
        return decrypt_event(encrypt_key, str(envelope["encrypt"]))
    return envelope


def verification_token(payload: dict[str, Any]) -> str:
    header = payload.get("header")
    if isinstance(header, dict) and header.get("token"):
        return str(header["token"])
    return str(payload.get("token", ""))


def redact(value: Any, key: str = "") -> Any:
    if key.lower() in REDACTED_FIELDS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def extract_text(message: dict[str, Any]) -> str:
    try:
        content = json.loads(str(message.get("content", "{}")))
    except json.JSONDecodeError:
        return ""
    text = str(content.get("text", "")) if isinstance(content, dict) else ""
    mentions = message.get("mentions", [])
    if isinstance(mentions, list):
        for mention in mentions:
            if isinstance(mention, dict) and mention.get("key"):
                text = text.replace(str(mention["key"]), "")
    text = re.sub(r"<at\b[^>]*>.*?</at>", "", text, flags=re.DOTALL)
    return " ".join(text.split())


def parse_message(payload: dict[str, Any]) -> dict[str, str] | None:
    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        return None
    if header.get("event_type") != "im.message.receive_v1":
        return None

    sender = event.get("sender")
    message = event.get("message")
    if not isinstance(sender, dict) or not isinstance(message, dict):
        return None
    if sender.get("sender_type") == "bot" or message.get("message_type") != "text":
        return None

    sender_ids = sender.get("sender_id")
    if not isinstance(sender_ids, dict):
        sender_ids = {}
    open_id = str(sender_ids.get("open_id", ""))
    chat_id = str(message.get("chat_id", ""))
    chat_type = str(message.get("chat_type", ""))
    text = extract_text(message)
    event_id = str(header.get("event_id", ""))
    message_id = str(message.get("message_id", ""))

    if chat_type == "p2p":
        receive_id_type, receive_id = "open_id", open_id
    else:
        receive_id_type, receive_id = "chat_id", chat_id

    if not text or not receive_id:
        return None
    return {
        "chat_type": chat_type or "unknown",
        "event_id": event_id,
        "message_id": message_id,
        "open_id": open_id,
        "chat_id": chat_id,
        "receive_id_type": receive_id_type,
        "receive_id": receive_id,
        "text": text,
    }


def get_tenant_access_token(settings: Settings, runtime: RuntimeState) -> str:
    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required to send messages")

    now = time.time()
    with runtime.lock:
        if runtime.token and now < runtime.token_expires_at - 60:
            return runtime.token

    response = requests.post(
        TOKEN_URL,
        json={"app_id": settings.app_id, "app_secret": settings.app_secret},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(f"tenant_access_token failed: code={data.get('code')} msg={data.get('msg', '')}")

    with runtime.lock:
        runtime.token = str(data["tenant_access_token"])
        runtime.token_expires_at = now + int(data.get("expire", 7200))
        return runtime.token


def send_text(
    settings: Settings,
    runtime: RuntimeState,
    receive_id_type: str,
    receive_id: str,
    text: str,
) -> dict[str, Any]:
    if receive_id_type not in {"open_id", "chat_id"}:
        raise ValueError("receive_id_type must be open_id or chat_id")
    token = get_tenant_access_token(settings, runtime)
    response = requests.post(
        f"{MESSAGE_URL}?receive_id_type={receive_id_type}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"send message failed: code={data.get('code')} msg={data.get('msg', '')}")
    return data


def send_echo(settings: Settings, runtime: RuntimeState, message: dict[str, str]) -> None:
    reply = f"收到{message['chat_type']}消息：{message['text']}"
    try:
        send_text(settings, runtime, message["receive_id_type"], message["receive_id"], reply)
        LOGGER.info(
            "reply sent chat_type=%s receive_id_type=%s message_id=%s",
            message["chat_type"],
            message["receive_id_type"],
            message["message_id"],
        )
    except Exception:
        LOGGER.exception(
            "reply failed chat_type=%s receive_id_type=%s message_id=%s",
            message["chat_type"],
            message["receive_id_type"],
            message["message_id"],
        )


def create_app(settings: Settings | None = None, runtime: RuntimeState | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    runtime = runtime or RuntimeState()
    application = FastAPI(title="Feishu Echo Diagnostic", version="1.0")
    application.state.settings = settings
    application.state.runtime = runtime

    @application.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "app_credentials_configured": bool(settings.app_id and settings.app_secret),
            "verification_token_configured": bool(settings.verification_token),
            "encryption_enabled": bool(settings.encrypt_key),
        }

    @application.post("/feishu/events")
    async def receive_event(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        raw_body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}

        try:
            payload = decode_event(raw_body, settings.encrypt_key)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("callback rejected: invalid payload: %s", exc)
            return JSONResponse({"error": "invalid payload"}, status_code=400)

        if settings.log_raw:
            LOGGER.debug("callback payload=%s", json.dumps(redact(payload), ensure_ascii=False, sort_keys=True))

        supplied_token = verification_token(payload)
        if settings.verification_token and not hmac.compare_digest(settings.verification_token, supplied_token):
            LOGGER.warning("callback rejected: verification token mismatch")
            return JSONResponse({"error": "verification token mismatch"}, status_code=401)

        # Feishu's request-URL verification is excluded from the normal event
        # signature flow. With Encrypt Key enabled it arrives as {"encrypt":
        # "..."} and may not include X-Lark-Signature, so decrypt and validate
        # Verification Token before considering normal-event signatures.
        if payload.get("type") == "url_verification" or payload.get("challenge"):
            challenge = str(payload.get("challenge", ""))
            if not challenge:
                return JSONResponse({"error": "missing challenge"}, status_code=400)
            LOGGER.info("URL verification challenge accepted")
            return JSONResponse({"challenge": challenge})

        signature_ok, signature_reason = verify_signature(settings.encrypt_key, headers, raw_body)
        if not signature_ok:
            LOGGER.warning("callback rejected: %s", signature_reason)
            return JSONResponse({"error": signature_reason}, status_code=401)

        message = parse_message(payload)
        if message is None:
            event_type = payload.get("header", {}).get("event_type", "unknown")
            LOGGER.info("event acknowledged without reply event_type=%s", event_type)
            return JSONResponse({"code": 0})

        dedupe_key = message["event_id"] or message["message_id"]
        if not runtime.claim_event(dedupe_key):
            LOGGER.info(
                "duplicate event acknowledged event_id=%s message_id=%s",
                message["event_id"],
                message["message_id"],
            )
            return JSONResponse({"code": 0, "duplicate": True})

        LOGGER.info(
            "message received chat_type=%s receive_id_type=%s event_id=%s message_id=%s",
            message["chat_type"],
            message["receive_id_type"],
            message["event_id"],
            message["message_id"],
        )
        background_tasks.add_task(send_echo, settings, runtime, message)
        return JSONResponse({"code": 0})

    return application


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="optional local KEY=VALUE file (for example scripts/dev/feishu.env)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the webhook diagnostic server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", default=8001, type=int)
    serve_parser.add_argument("--reload", action="store_true")

    send_parser = subparsers.add_parser("send", help="send one diagnostic text message")
    send_parser.add_argument("--receive-id-type", choices=("open_id", "chat_id"), required=True)
    send_parser.add_argument("--receive-id", required=True)
    send_parser.add_argument("--text", required=True)

    subparsers.add_parser("check-token", help="validate App ID/Secret without printing the access token")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_env_file(args.env_file)
    settings = Settings.from_env()
    runtime = RuntimeState()

    if args.command == "send":
        result = send_text(settings, runtime, args.receive_id_type, args.receive_id, args.text)
        print(json.dumps({"code": result.get("code"), "msg": result.get("msg", "")}, ensure_ascii=False))
        return 0

    if args.command == "check-token":
        get_tenant_access_token(settings, runtime)
        print(json.dumps({"ok": True, "tenant_access_token": "[REDACTED]"}))
        return 0

    if not settings.verification_token:
        LOGGER.warning("FEISHU_VERIFICATION_TOKEN is empty; callback token validation is disabled")
    if not settings.encrypt_key:
        LOGGER.warning("FEISHU_ENCRYPT_KEY is empty; callback signature verification and decryption are disabled")

    import uvicorn

    uvicorn.run(create_app(settings, runtime), host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("FEISHU_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(main())
