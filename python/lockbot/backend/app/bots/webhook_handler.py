"""
In-process webhook handler.

Handles IM callbacks for bots running in shared-port (inprocess) mode.
Uses per-bot adapter (bot.adapter) instead of direct platform-specific calls,
so multiple bots can coexist in a single process.
"""

import logging

from lockbot.core.handler import execute_command, redact_command_text
from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.platforms.registry import create_adapter

logger = logging.getLogger(__name__)


def handle_webhook(bot, raw_form: dict, raw_args: dict, raw_body: bytes | None) -> tuple[str, int, dict]:
    """
    Process an incoming IM webhook callback for a specific bot instance.

    Returns:
        Tuple of (response_text, status_code, meta).
        meta contains: user_id, command, group_id (if available).
    """
    adapter = getattr(bot, "adapter", None)
    if not isinstance(adapter, MessageAdapter):
        adapter = create_adapter(bot.config.get_val("PLATFORM", None), config=bot.config)
    meta = {}

    # URL verification request (echostr present in form data)
    echostr = raw_form.get("echostr")
    if echostr:
        signature = raw_form.get("signature")
        rn = raw_form.get("rn")
        timestamp = raw_form.get("timestamp")
        if adapter.verify_request(signature, rn=rn, timestamp=timestamp):
            return echostr, 200, {"event": "url_verification"}
        return "check signature fail", 401, {"event": "url_verification_failed"}

    # Normal message callback
    signature = raw_args.get("signature")
    rn = raw_args.get("rn")
    timestamp = raw_args.get("timestamp")

    if not adapter.verify_request(signature, rn=rn, timestamp=timestamp):
        return "check signature fail", 401, {"event": "signature_failed"}

    # Decrypt message
    msg_data = adapter.decrypt_payload(raw_body)
    if msg_data is None:
        return "decrypt failed", 400, {"event": "decrypt_failed"}

    # Extract command info for logging
    group_id = None
    try:
        user_id, group_id, command_text = adapter.extract_command(msg_data)
        meta = {"user_id": user_id, "group_id": group_id, "command": command_text}
    except Exception:
        meta = {"event": "parse_error"}

    # Log only the (redacted) command, never the decrypted payload: the body can carry a
    # bind code and other sensitive content that must not reach DEBUG output.
    logger.debug(
        "Webhook command for bot %s: %s",
        bot.config.get_val("BOT_NAME"),
        redact_command_text(meta.get("command", "<unparsed>")),
    )

    allowed_groups = bot.config.get_val("_AUTHORIZED_GROUP_IDS", [])
    if allowed_groups and str(group_id) not in allowed_groups:
        return "conversation not authorized", 403, {"event": "scope_refused"}

    # Execute command
    reply = execute_command(msg_data, bot)

    # Send the reply back to the conversation the message came from. Retargeting goes
    # through the adapter so this path carries no platform-specific message layout.
    adapter.apply_reply_target(reply, group_id)
    adapter.send(reply)
    return "command succeed", 200, meta
