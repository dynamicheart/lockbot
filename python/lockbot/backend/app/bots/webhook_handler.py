"""
In-process webhook handler.

Handles IM callbacks for bots running in shared-port (inprocess) mode.
Uses per-bot adapter (bot.adapter) instead of direct platform-specific calls,
so multiple bots can coexist in a single process.
"""

import logging

from lockbot.core.handler import execute_command
from lockbot.core.platforms.infoflow import InfoflowAdapter

logger = logging.getLogger(__name__)


def handle_webhook(bot, raw_form: dict, raw_args: dict, raw_body: bytes | None) -> tuple[str, int, dict]:
    """
    Process an incoming IM webhook callback for a specific bot instance.

    Returns:
        Tuple of (response_text, status_code, meta).
        meta contains: user_id, command, group_id (if available).
    """
    adapter = getattr(bot, "adapter", InfoflowAdapter(config=bot.config))
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
    try:
        user_id, group_id, command_text = adapter.extract_command(msg_data)
        meta = {"user_id": user_id, "group_id": group_id, "command": command_text}
    except Exception:
        meta = {"event": "parse_error"}

    logger.debug("Webhook message for bot %s: %s", bot.config.get_val("BOT_NAME"), msg_data)

    # Execute command
    reply = execute_command(msg_data, bot)

    # Set reply target (group id)
    toid = msg_data["message"]["header"].get("toid")
    if toid:
        reply["message"]["header"]["toid"] = toid

    # Send reply via per-bot adapter
    adapter.send(reply)
    return "command succeed", 200, meta
