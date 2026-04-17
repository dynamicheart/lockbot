"""Command parsing and routing handler."""

import re

from lockbot.core.config import Config
from lockbot.core.i18n import t
from lockbot.core.message_adapter import MessageAdapter


def execute_command(msg_data, bot):
    adapter = getattr(bot, "adapter", None)
    if not isinstance(adapter, MessageAdapter):
        from lockbot.core.platforms.infoflow import InfoflowAdapter

        adapter = InfoflowAdapter()
    user_id, _, rcv_info = adapter.extract_command(msg_data)
    config = getattr(bot, "config", None)
    cluster_configs = config.get_val("CLUSTER_CONFIGS", {}) if config else Config.get("CLUSTER_CONFIGS", {})

    supported_commands = set(bot.supported_commands())

    m = re.match(r"^(\w+)", rcv_info, re.I)
    cmd = m.group(1).lower() if m else ""

    # Empty input defaults to query
    if cmd == "":
        cmd = "query"

    # Treat known node_key as query
    if cmd not in supported_commands and rcv_info in cluster_configs:
        cmd = "query"

    if cmd not in supported_commands:
        return bot.print_help(
            user_id, t("error.unrecognized_command", config=getattr(bot, "config", None), command=rcv_info)
        )

    if cmd == "lock":
        return bot.lock(user_id, rcv_info)
    elif cmd == "slock":
        return bot.slock(user_id, rcv_info)
    elif cmd in ("unlock", "free"):
        return bot.unlock(user_id, rcv_info)
    elif cmd == "kickout":
        return bot.kickout(user_id, rcv_info)
    elif cmd == "book":
        return bot.book(user_id, rcv_info)
    elif cmd == "take":
        return bot.take(user_id, rcv_info)
    elif cmd == "query":
        if rcv_info in cluster_configs:
            return bot.query(user_id, rcv_info)
        else:
            return bot.query(user_id)
    elif cmd in ("help", "h"):
        return bot.print_help(user_id)
    else:
        return bot.print_help(user_id, t("error.unknown_error", config=getattr(bot, "config", None), command=rcv_info))


# ── Legacy Flask entry point (deprecated) ────────────────────────────────────
# The functions below are used by lockbot.core.entry (legacy Flask mode).
# New deployments should use the FastAPI platform mode (lockbot.backend.app.main).


def decrypt_message(msg_base64):
    """Decrypt a base64-encoded message, returning parsed JSON or aborting on failure.

    .. deprecated:: Use the FastAPI webhook handler instead.
    """
    from flask import abort

    from lockbot.core.platforms.infoflow import InfoflowAdapter

    result = InfoflowAdapter().decrypt_payload(msg_base64)
    if result is None:
        abort(404)
    return result


def handle_request(echostr, signature, rn, timestamp, msg_base64, bot):
    """Handle an incoming Flask request for the legacy Infoflow standalone mode.

    .. deprecated:: Use the FastAPI webhook handler instead.
    """
    from lockbot.core.platforms.infoflow import InfoflowAdapter

    adapter = InfoflowAdapter()
    if echostr:
        if adapter.verify_request(signature, rn=rn, timestamp=timestamp):
            return echostr
        return "check signature fail", 401

    if not adapter.verify_request(signature, rn=rn, timestamp=timestamp):
        return "check signature fail", 401

    msg_data = decrypt_message(msg_base64)
    reply = execute_command(msg_data, bot)
    reply = adapter.set_reply_target(reply, msg_data["message"]["header"].get("toid", ""))
    adapter.send(reply)
    return "command succeed"


def page_not_found(_):
    """Return a 404 response.

    .. deprecated:: Use platform mode instead.
    """
    return "404 - Page not found", 404
