"""B14: the deprecated legacy handler must not print or log a decrypted bind code.

`handle_request` used to `print(msg_data)` after decryption, sending the decrypted
`bind <code>` (code included) to stdout. That bypassed every redaction added for the FastAPI
path. This pins that no bind code reaches stdout or the logger from the legacy path.
"""

import logging

from fake_adapter import VALID_SIGNATURE, FakeAdapter, encode_message
from lockbot.core import handler
from lockbot.core.bot_instance import BotInstance
from lockbot.core.platforms.registry import register_adapter, unregister_adapter

SECRET = "LEAK5678"


def test_legacy_handler_does_not_emit_the_bind_code(monkeypatch, capsys, caplog, tmp_path):
    # The legacy entry point verifies/decrypts through a module-level global adapter.
    register_adapter(FakeAdapter)
    monkeypatch.setattr(handler, "_global_adapter", FakeAdapter())
    try:
        bot = BotInstance(
            "NODE",
            {"BOT_NAME": "legacy", "BOT_ID": 1, "DATA_DIR": str(tmp_path), "PLATFORM": FakeAdapter.platform_name},
            auto_start=False,
        ).bot

        with caplog.at_level(logging.DEBUG):
            body = handler.handle_request(
                echostr=None,
                signature=VALID_SIGNATURE,
                rn="1",
                timestamp="2",
                msg_base64=encode_message(f"bind {SECRET}"),
                bot=bot,
            )
    finally:
        unregister_adapter(FakeAdapter.platform_name)

    assert body == "command succeed"
    out = capsys.readouterr()
    assert SECRET not in out.out
    assert SECRET not in out.err
    assert SECRET not in caplog.text
