"""Command parsing and routing handler."""

import logging
import re

from lockbot.core.i18n import t
from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.platforms.registry import create_adapter

logger = logging.getLogger(__name__)

#: Identity-binding commands. These are platform/transport concerns rather than
#: bot-type operations, so they are handled before `supported_commands()` and are
#: delegated to an injected hook. They are **never** reachable through the HTTP API:
#: enrollment and revocation are chat-only by design.
BINDING_COMMANDS = ("bind", "unbind", "bindings")

#: Spellings that mean the same operation. The allowlist passed to
#: :func:`execute_text_command` is evaluated on the canonical word, so an alias or
#: shorthand cannot be used to slip past a gate that denies the canonical form.
#: Note this is deliberately NOT applied to ``bot.supported_commands()``, which has
#: always been matched on the word as typed.
_CANONICAL_COMMANDS = {
    "free": "unlock",
    "h": "help",
}


def canonical_command(cmd: str) -> str:
    """Return the canonical command word for a spelling of a command."""
    return _CANONICAL_COMMANDS.get(cmd, cmd)


def redact_command_text(text):
    """Mask the secret in a command string before it is written to any log.

    ``bind <code>`` is the only command carrying a secret: the code is the one-time proof
    that activates a credential, so it must never reach a log file or DEBUG output where
    someone who should not hold it could replay it. ``unbind <id>`` and ``bindings`` carry
    only a public short id, and every other command carries nothing sensitive, so they are
    returned unchanged.
    """
    if not isinstance(text, str):
        return text
    m = re.match(r"^(\s*)(\w+)(\s+)(\S.*)$", text, re.S)
    if m and m.group(2).lower() == "bind":
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]"
    return text


class CommandRejected(Exception):
    """Base class for a command refused before any bot method was invoked."""

    def __init__(self, command, message):
        self.command = command
        super().__init__(message)


class CommandNotAllowed(CommandRejected):
    """The caller's allowlist does not include this command."""

    def __init__(self, command):
        super().__init__(command, f"Command {command!r} is not permitted for this caller")


class CommandNotSupported(CommandRejected):
    """The target bot does not support this command."""

    def __init__(self, command):
        super().__init__(command, f"Command {command!r} is not supported by this bot")


def _resolve_adapter(bot) -> MessageAdapter:
    """Return the bot's adapter, or a default-platform adapter if it has none."""
    adapter = getattr(bot, "adapter", None)
    if isinstance(adapter, MessageAdapter):
        return adapter
    return create_adapter()


def _dispatch_binding_command(user_id, text, bot, cmd, config):
    """Hand an identity-binding command to the injected hook.

    ``core/`` recognizes these commands but owns no database: the backend injects
    ``bot.binding_hook``. With no hook (for example an isolated unit test) the commands report
    themselves as unavailable rather than failing obscurely.
    """
    parts = text.split(None, 1)
    argument = parts[1].strip() if len(parts) > 1 else ""
    adapter = _resolve_adapter(bot)

    hook = getattr(bot, "binding_hook", None)
    if not callable(hook):
        return adapter.build_reply(t("binding.unavailable", config=config) + "\n\n", [user_id])

    try:
        reply_text = hook(
            platform=getattr(adapter, "platform_name", ""),
            external_user_id=user_id,
            bot_id=config.get_val("BOT_ID") if config else None,
            action=cmd,
            argument=argument,
        )
    except Exception:
        logger.exception("Binding hook failed for action %s", cmd)
        return adapter.build_reply(t("binding.failed", config=config) + "\n\n", [user_id])

    return adapter.build_reply(reply_text.rstrip() + "\n\n", [user_id])


def execute_text_command(user_id, text, bot, allowed_commands=None, *, acquisition_limit=None, source="chat"):
    """Set a server-controlled origin while the shared dispatcher executes."""
    from lockbot.core.command_context import RESOURCE_OPERATIONS, command_action, command_source, observe_operation

    if source not in {"chat", "api", "unknown"}:
        raise ValueError("Unsupported command source")
    token = command_source.set(source)
    match = re.match(r"^(\w+)", text, re.I)
    operation = canonical_command(match.group(1).lower()) if match else ""
    action_token = command_action.set(operation)
    outcome = "dispatched"
    try:
        return _execute_text_command(user_id, text, bot, allowed_commands, acquisition_limit=acquisition_limit)
    except CommandRejected:
        outcome = "rejected"
        raise
    except Exception:
        outcome = "uncertain"
        raise
    finally:
        command_source.reset(token)
        command_action.reset(action_token)
        if operation in RESOURCE_OPERATIONS and source in {"chat", "api"}:
            try:
                config = getattr(bot, "config", None)
                observe_operation(
                    config.get_val("BOT_ID") if config else None,
                    "agent" if source == "api" else "chat",
                    operation,
                    outcome,
                )
            except Exception:
                logger.exception("Operation telemetry metadata unavailable")


def _execute_text_command(user_id, text, bot, allowed_commands=None, *, acquisition_limit=None):
    """Dispatch a raw command string to a bot, independent of any transport.

    This is the single seam shared by the chat path (via :func:`execute_command`) and by
    the HTTP API, so command parsing and routing cannot drift between transports.

    Args:
        user_id: The acting user's platform-native id. Callers are responsible for
            having established it from a trusted source.
        text: The raw command text, already stripped of transport framing.
        bot: The bot instance to dispatch to.
        allowed_commands: When None (the chat path), every command the bot supports is
            dispatchable and a refusal is rendered as a help reply, exactly as before.
            When a collection is supplied (the API path), the command is matched against
            it by canonical word and a refusal raises :class:`CommandRejected` instead,
            so the caller can return a precise error rather than a help dump.

    Returns:
        Whatever the dispatched bot method returns.

    Raises:
        CommandNotAllowed: allowlist supplied and the command is not in it.
        CommandNotSupported: allowlist supplied and the bot does not support the command.
    """
    gated = allowed_commands is not None
    config = getattr(bot, "config", None)
    cluster_configs = config.get_val("CLUSTER_CONFIGS", {}) if config else {}

    supported_commands = set(bot.supported_commands())

    m = re.match(r"^(\w+)", text, re.I)
    cmd = m.group(1).lower() if m else ""

    # Only an empty mention is a query. Malformed nonempty input must reach
    # the existing rejection path rather than silently displaying resource state.
    if not text.strip():
        cmd = "query"

    # Treat known node_key as query
    if cmd not in supported_commands and text in cluster_configs:
        cmd = "query"

    if gated and (
        cmd in BINDING_COMMANDS or canonical_command(cmd) not in {canonical_command(c) for c in allowed_commands}
    ):
        # Binding commands are refused unconditionally, not merely by allowlist absence,
        # so adding one to an allowlist by mistake cannot expose enrollment over HTTP.
        raise CommandNotAllowed(cmd)

    if cmd in BINDING_COMMANDS:
        return _dispatch_binding_command(user_id, text, bot, cmd, config)

    if cmd not in supported_commands:
        if gated:
            raise CommandNotSupported(cmd)
        return bot.print_help(user_id, t("error.unrecognized_command", config=config, command=text))

    acquisition_kwargs = {"acquisition_limit": acquisition_limit} if acquisition_limit is not None else {}
    if cmd == "lock":
        return bot.lock(user_id, text, **acquisition_kwargs)
    elif cmd == "slock":
        return bot.slock(user_id, text, **acquisition_kwargs)
    elif cmd in ("unlock", "free"):
        return bot.unlock(user_id, text)
    elif cmd == "kickout":
        return bot.kickout(user_id, text)
    elif cmd == "book":
        return bot.book(user_id, text, **acquisition_kwargs)
    elif cmd == "take":
        return bot.take(user_id, text)
    elif cmd == "query":
        if text in cluster_configs:
            return bot.query(user_id, text)
        else:
            return bot.query(user_id)
    elif cmd in ("help", "h"):
        return bot.print_help(user_id)
    elif cmd == "alias":
        return bot.alias(user_id, text)
    else:
        # Reachable for a command the bot advertises but the dispatcher has no branch
        # for (e.g. QueueBot's "kicklock").
        if gated:
            raise CommandNotSupported(cmd)
        return bot.print_help(user_id, t("error.unknown_error", config=config, command=text))


def execute_command(msg_data, bot):
    """Extract the acting identity and command text from a platform message, then dispatch."""
    adapter = _resolve_adapter(bot)
    user_id, _group_id, rcv_info = adapter.extract_command(msg_data)
    return execute_text_command(user_id, rcv_info, bot)
