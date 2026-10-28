"""B8: a bind code must never reach a log, at any logging boundary.

The bind code is the one-time secret that activates a credential. Two boundaries used to
leak it: the webhook handler logged the whole decrypted payload at DEBUG, and the backend
wrote the raw command line to the per-bot command log. ``redact_command_text`` is the single
seam both now pass through; these tests pin it and the handler's DEBUG line.
"""

import logging

import pytest
from fake_adapter import VALID_SIGNATURE, FakeAdapter, encode_message
from lockbot.backend.app.bots.webhook_handler import handle_webhook
from lockbot.core.bot_instance import BotInstance
from lockbot.core.handler import redact_command_text
from lockbot.core.platforms.registry import register_adapter, unregister_adapter

SECRET = "AB7Q9ZK3"


class TestRedactCommandText:
    def test_bind_masks_its_argument(self):
        assert SECRET not in redact_command_text(f"bind {SECRET}")
        assert redact_command_text(f"bind {SECRET}") == "bind [redacted]"

    def test_bind_is_case_insensitive(self):
        assert SECRET not in redact_command_text(f"BIND {SECRET}")

    def test_bind_with_no_argument_is_unchanged(self):
        assert redact_command_text("bind") == "bind"

    def test_bindings_is_not_treated_as_bind(self):
        assert redact_command_text("bindings") == "bindings"

    def test_unbind_keeps_its_public_id(self):
        assert redact_command_text("unbind abcd1234") == "unbind abcd1234"

    def test_ordinary_commands_are_unchanged(self):
        assert redact_command_text("lock n1 1h") == "lock n1 1h"

    def test_non_string_is_returned_as_is(self):
        assert redact_command_text(None) is None


@pytest.fixture
def fake_bot(tmp_path):
    register_adapter(FakeAdapter)
    try:
        instance = BotInstance(
            "NODE",
            {"BOT_NAME": "redactme", "BOT_ID": 7, "DATA_DIR": str(tmp_path), "PLATFORM": FakeAdapter.platform_name},
        )
        yield instance.bot
    finally:
        unregister_adapter(FakeAdapter.platform_name)


class TestWebhookDebugLogNeverLeaksTheCode:
    def test_debug_log_omits_the_bind_code(self, fake_bot, caplog):
        with caplog.at_level(logging.DEBUG, logger="lockbot.backend.app.bots.webhook_handler"):
            handle_webhook(
                fake_bot,
                raw_form={},
                raw_args={"signature": VALID_SIGNATURE},
                raw_body=encode_message(f"bind {SECRET}"),
            )
        assert SECRET not in caplog.text
        assert "[redacted]" in caplog.text

    def test_debug_log_does_not_dump_the_whole_payload(self, fake_bot, caplog):
        with caplog.at_level(logging.DEBUG, logger="lockbot.backend.app.bots.webhook_handler"):
            handle_webhook(
                fake_bot,
                raw_form={},
                raw_args={"signature": VALID_SIGNATURE},
                raw_body=encode_message("query", conversation="super-secret-room"),
            )
        assert "super-secret-room" not in caplog.text, "the raw payload must no longer be logged"
