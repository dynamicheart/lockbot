"""AC-1 / AC-8: the shared text-command seam.

``tests/core/test_handler.py`` covers the pre-existing chat behaviour and is deliberately
left untouched. This file covers the seam itself: transport-independent dispatch, the
``allowed_commands`` gate (which must refuse before any bot method runs), canonical-word
matching, and the fact that ``execute_command`` is now only adapter extraction plus
delegation.
"""

from unittest.mock import MagicMock

import pytest
from fake_adapter import FakeAdapter, make_message
from lockbot.core.device_bot import DeviceBot
from lockbot.core.handler import (
    CommandNotAllowed,
    CommandNotSupported,
    canonical_command,
    execute_command,
    execute_text_command,
)
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot

BOT_CLASSES = [DeviceBot, NodeBot, QueueBot]
BOT_IDS = [cls.__name__ for cls in BOT_CLASSES]

# Every command word the dispatcher routes, mapped to the bot method it must reach.
DISPATCH_TABLE = {
    "lock": "lock",
    "slock": "slock",
    "unlock": "unlock",
    "free": "unlock",
    "book": "book",
    "take": "take",
    "kickout": "kickout",
    "query": "query",
    "help": "print_help",
    "h": "print_help",
    "alias": "alias",
}


def _mock_bot(bot_cls, cluster_configs=None):
    """A spy bot that advertises exactly what ``bot_cls`` advertises."""
    bot = MagicMock()
    # supported_commands() reads no instance state on any of the three classes.
    bot.supported_commands.return_value = bot_cls.supported_commands(None)
    config_values = {"CLUSTER_CONFIGS": cluster_configs or {}, "LANGUAGE": "zh"}
    bot.config = MagicMock()
    bot.config.get_val.side_effect = lambda key, default=None: config_values.get(key, default)
    bot.adapter = None
    return bot


@pytest.mark.parametrize("bot_cls", BOT_CLASSES, ids=BOT_IDS)
@pytest.mark.parametrize("word", sorted(DISPATCH_TABLE))
def test_seam_dispatches_every_supported_word(bot_cls, word):
    """The seam routes each word exactly as the chat dispatcher always has."""
    bot = _mock_bot(bot_cls)
    if word not in bot.supported_commands.return_value:
        pytest.skip(f"{bot_cls.__name__} does not advertise {word!r}")

    text = f"{word} node1 2h"
    execute_text_command("user123", text, bot)

    method = getattr(bot, DISPATCH_TABLE[word])
    assert method.called, f"{word!r} did not reach {DISPATCH_TABLE[word]}()"
    if DISPATCH_TABLE[word] in ("print_help", "query"):
        # help and an unqualified query take the acting identity only
        method.assert_called_once_with("user123")
    else:
        method.assert_called_once_with("user123", text)


@pytest.mark.parametrize("bot_cls", BOT_CLASSES, ids=BOT_IDS)
def test_empty_text_is_treated_as_query(bot_cls):
    bot = _mock_bot(bot_cls)
    execute_text_command("user123", "", bot)
    bot.query.assert_called_once_with("user123")


@pytest.mark.parametrize("bot_cls", BOT_CLASSES, ids=BOT_IDS)
def test_known_node_key_is_treated_as_query(bot_cls):
    bot = _mock_bot(bot_cls, cluster_configs={"node1": {}})
    execute_text_command("user123", "node1", bot)
    bot.query.assert_called_once_with("user123", "node1")


@pytest.mark.parametrize("bot_cls", BOT_CLASSES, ids=BOT_IDS)
def test_unsupported_word_returns_help_on_the_chat_path(bot_cls):
    bot = _mock_bot(bot_cls)
    bot.supported_commands.return_value = ["help", "query"]
    execute_text_command("user123", "lock node1", bot)
    bot.lock.assert_not_called()
    assert bot.print_help.call_count == 1
    assert bot.print_help.call_args[0][0] == "user123"
    assert len(bot.print_help.call_args[0]) == 2  # carries the unrecognized-command notice


class TestAllowlistGate:
    def test_denied_command_never_reaches_the_bot(self):
        bot = _mock_bot(NodeBot)
        with pytest.raises(CommandNotAllowed) as exc:
            execute_text_command("user123", "kickout node1", bot, allowed_commands={"lock", "query"})
        assert exc.value.command == "kickout"
        bot.kickout.assert_not_called()
        bot.print_help.assert_not_called()

    def test_preemptive_and_administrative_commands_are_refusable(self):
        bot = _mock_bot(QueueBot)
        personal = {"lock", "slock", "unlock", "query", "book", "alias"}
        for word in ("take", "kickout", "kicklock"):
            with pytest.raises(CommandNotAllowed):
                execute_text_command("user123", f"{word} node1", bot, allowed_commands=personal)
        bot.take.assert_not_called()
        bot.kickout.assert_not_called()

    def test_unknown_enrollment_words_are_refused_not_helped(self):
        """``bind``/``unbind``/``bindings`` must never be reachable through a gated caller."""
        bot = _mock_bot(NodeBot)
        for word in ("bind", "unbind", "bindings"):
            with pytest.raises(CommandNotAllowed):
                execute_text_command("user123", f"{word} abc123", bot, allowed_commands={"lock", "query"})
        bot.print_help.assert_not_called()

    def test_allowed_command_still_dispatches(self):
        bot = _mock_bot(NodeBot)
        execute_text_command("user123", "lock node1 2h", bot, allowed_commands={"lock", "query"})
        bot.lock.assert_called_once_with("user123", "lock node1 2h")

    def test_supported_but_unroutable_word_raises_when_gated(self):
        """QueueBot advertises ``kicklock`` but the dispatcher has no branch for it."""
        bot = _mock_bot(QueueBot)
        with pytest.raises(CommandNotSupported):
            execute_text_command("user123", "kicklock node1", bot, allowed_commands={"kicklock"})

    def test_unsupported_command_raises_instead_of_dumping_help_when_gated(self):
        """``book`` on a DeviceBot is an unsupported operation, not an unknown command."""
        bot = _mock_bot(DeviceBot)
        with pytest.raises(CommandNotSupported) as exc:
            execute_text_command("user123", "book node1", bot, allowed_commands={"lock", "book", "query"})
        assert exc.value.command == "book"
        bot.print_help.assert_not_called()

    @pytest.mark.parametrize(
        ("text", "allowed"),
        [
            ("free node1", {"unlock"}),
            ("unlock node1", {"free"}),
        ],
    )
    def test_alias_spellings_resolve_to_the_same_allow_decision(self, text, allowed):
        bot = _mock_bot(NodeBot)
        execute_text_command("user123", text, bot, allowed_commands=allowed)
        bot.unlock.assert_called_once_with("user123", text)

    @pytest.mark.parametrize("text", ["FREE node1", "Unlock node1"])
    def test_alias_spellings_resolve_to_the_same_deny_decision(self, text):
        bot = _mock_bot(NodeBot)
        with pytest.raises(CommandNotAllowed):
            execute_text_command("user123", text, bot, allowed_commands={"lock", "query"})
        bot.unlock.assert_not_called()

    @pytest.mark.parametrize("text", ["LOCK node1 2h", "LoCk node1 2h"])
    def test_casing_does_not_bypass_the_gate(self, text):
        bot = _mock_bot(NodeBot)
        with pytest.raises(CommandNotAllowed):
            execute_text_command("user123", text, bot, allowed_commands={"query"})
        bot.lock.assert_not_called()

    def test_empty_text_is_gated_as_query(self):
        bot = _mock_bot(NodeBot)
        with pytest.raises(CommandNotAllowed) as exc:
            execute_text_command("user123", "", bot, allowed_commands={"lock"})
        assert exc.value.command == "query"
        bot.query.assert_not_called()

    def test_node_key_shorthand_is_gated_as_query(self):
        bot = _mock_bot(NodeBot, cluster_configs={"node1": {}})
        with pytest.raises(CommandNotAllowed):
            execute_text_command("user123", "node1", bot, allowed_commands={"lock"})
        bot.query.assert_not_called()

    def test_empty_allowlist_denies_everything(self):
        bot = _mock_bot(NodeBot)
        with pytest.raises(CommandNotAllowed):
            execute_text_command("user123", "query", bot, allowed_commands=set())


@pytest.mark.parametrize(
    ("word", "expected"),
    [("free", "unlock"), ("unlock", "unlock"), ("h", "help"), ("help", "help"), ("lock", "lock")],
)
def test_canonical_command(word, expected):
    assert canonical_command(word) == expected


class TestExecuteCommandDelegates:
    """AC-1 + AC-8: ``execute_command`` only extracts identity and text, via the adapter."""

    def test_identity_and_text_come_from_the_adapter(self):
        bot = _mock_bot(NodeBot)
        bot.adapter = FakeAdapter()
        execute_command(make_message("lock node1 2h", actor="fake-actor-7"), bot)
        bot.lock.assert_called_once_with("fake-actor-7", "lock node1 2h")

    def test_no_infoflow_payload_is_required(self):
        """The dispatcher never touches message/header/body/fromuserid/toid."""
        bot = _mock_bot(NodeBot)
        bot.adapter = FakeAdapter()
        msg = make_message("  query  ")
        execute_command(msg, bot)
        bot.query.assert_called_once_with("fake-actor-1")
        assert set(msg) == {"envelope"}

    def test_bot_without_an_adapter_falls_back_to_the_default_platform(self):
        """Pre-existing behaviour: a bot exposing no real adapter still dispatches."""
        bot = _mock_bot(NodeBot)
        infoflow_msg = {
            "message": {
                "header": {"fromuserid": "user123"},
                "body": [{"type": "TEXT", "content": "lock node1"}],
            }
        }
        execute_command(infoflow_msg, bot)
        bot.lock.assert_called_once_with("user123", "lock node1")
