"""
In-process webhook handler.

Handles IM callbacks for bots running in shared-port (inprocess) mode.
Uses per-bot adapter (bot.adapter) so multiple bots and multiple IM platforms
can coexist in a single process.
"""

import json
import logging

from lockbot.core.handler import execute_command

logger = logging.getLogger(__name__)


def handle_webhook(
    bot,
    raw_form: dict,
    raw_args: dict,
    raw_body: bytes | None,
    raw_headers: dict | None = None,
) -> tuple[str, int, dict]:
    """
    Process an incoming IM webhook callback for a specific bot instance.

    Args:
        bot: Bot instance with a .adapter attribute.
        raw_form: Parsed form data (application/x-www-form-urlencoded).
        raw_args: Query string parameters.
        raw_body: Raw request body bytes.
        raw_headers: HTTP request headers (needed for Slack signature verification).

    Returns:
        Tuple of (response_text, status_code, meta).
        meta may contain: user_id, command, group_id, event.
    """
    adapter = bot.adapter
    headers = raw_headers or {}
    body_str = raw_body.decode("utf-8") if raw_body else ""

    # ── URL / handshake verification ─────────────────────────────────────────

    # Slack: challenge request arrives as JSON with type == "url_verification"
    if raw_body:
        try:
            maybe_json = json.loads(body_str)
            if maybe_json.get("type") == "url_verification":
                ts = headers.get("x-slack-request-timestamp", "")
                sig = headers.get("x-slack-signature", "")
                if adapter.verify_request(sig, timestamp=ts, body=body_str):
                    return maybe_json["challenge"], 200, {"event": "url_verification"}
                return "check signature fail", 401, {"event": "url_verification_failed"}
        except (ValueError, KeyError):
            pass  # not JSON or no 'type' key — continue with normal flow

    # Infoflow: echostr handshake arrives as form field
    echostr = raw_form.get("echostr")
    if echostr:
        signature = raw_form.get("signature")
        rn = raw_form.get("rn")
        timestamp = raw_form.get("timestamp")
        if adapter.verify_request(signature, rn=rn, timestamp=timestamp):
            return echostr, 200, {"event": "url_verification"}
        return "check signature fail", 401, {"event": "url_verification_failed"}

    # ── Signature verification ────────────────────────────────────────────────

    # Slack uses X-Slack-Signature + X-Slack-Request-Timestamp headers + raw body
    # Infoflow uses signature/rn/timestamp query params
    slack_sig = headers.get("x-slack-signature")
    if slack_sig:
        ts = headers.get("x-slack-request-timestamp", "")
        if not adapter.verify_request(slack_sig, timestamp=ts, body=body_str):
            return "check signature fail", 401, {"event": "signature_failed"}
    else:
        signature = raw_args.get("signature")
        rn = raw_args.get("rn")
        timestamp = raw_args.get("timestamp")
        if not adapter.verify_request(signature, rn=rn, timestamp=timestamp):
            return "check signature fail", 401, {"event": "signature_failed"}

    # ── Decrypt / parse payload ───────────────────────────────────────────────

    msg_data = adapter.decrypt_payload(raw_body)
    if msg_data is None:
        return "decrypt failed", 400, {"event": "decrypt_failed"}

    # ── Extract command metadata ──────────────────────────────────────────────

    meta = {}
    try:
        user_id, group_id, command_text = adapter.extract_command(msg_data)
        meta = {"user_id": user_id, "group_id": group_id, "command": command_text}
    except Exception:
        logger.exception("Failed to extract command from message")
        return "parse error", 400, {"event": "parse_error"}

    logger.debug("Webhook message for bot %s: cmd=%s", bot.config.get_val("BOT_NAME"), meta.get("command"))

    # ── Execute command and send reply ───────────────────────────────────────

    reply = execute_command(msg_data, bot)

    # Direct the reply to the originating group/channel (platform-specific)
    reply = adapter.set_reply_target(reply, group_id)

    adapter.send(reply)
    return "command succeed", 200, meta
