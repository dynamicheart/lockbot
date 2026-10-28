"""Chat replies use the existing title/paragraph/mention format in either language."""

from unittest.mock import MagicMock

import pytest
from lockbot.core.config import Config
from lockbot.core.handler import execute_text_command
from lockbot.core.i18n import t
from lockbot.core.i18n.en import MESSAGES as EN
from lockbot.core.i18n.zh import MESSAGES as ZH
from lockbot.core.platforms.infoflow import InfoflowAdapter


@pytest.mark.parametrize("lang,heading", [("zh", "【无需解绑】"), ("en", "[Nothing to revoke]")])
def test_binding_reply_keeps_paragraph_break_before_real_platform_mention(lang, heading):
    config = Config({"BOT_ID": "test", "LANGUAGE": lang})
    bot = MagicMock()
    bot.config = config
    bot.adapter = InfoflowAdapter(config)
    bot.binding_hook.return_value = t("binding.nothing_to_revoke", lang=lang, bot_id=4)
    reply = execute_text_command("alice", "unbind", bot)
    body = reply["message"]["body"]
    assert body[0]["type"] == "TEXT"
    assert heading in body[0]["content"]
    assert "\n\n" in body[0]["content"]
    assert body[0]["content"].endswith("\n\n")
    assert "`" not in body[0]["content"]
    assert body[-1] == {"type": "AT", "atuserids": ["alice"]}


def test_all_agent_chat_messages_have_matching_translations_and_parameters():
    from string import Formatter

    keys = {k for k in ZH if k.startswith(("binding.", "notify.api"))}
    assert keys == {k for k in EN if k.startswith(("binding.", "notify.api"))}

    def fields(value):
        return {name for _, name, _, _ in Formatter().parse(value) if name}

    for key in keys:
        assert ZH[key] and EN[key]
        assert fields(ZH[key]) == fields(EN[key]), key
