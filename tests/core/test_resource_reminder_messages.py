"""Compare the complete adapter payloads captured before reminder extraction."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.platforms.feishu import FeishuAdapter
from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot

CASES = (
    ("node-expiry", NodeBot, "NODE"),
    ("node-warning", NodeBot, "NODE"),
    ("device-expiry", DeviceBot, "DEVICE"),
    ("device-warning", DeviceBot, "DEVICE"),
    ("queue-combined", QueueBot, "QUEUE"),
    ("queue-device-combined", QueueDeviceBot, "QUEUE_DEVICE"),
    ("queue-delay", QueueBot, "QUEUE"),
    ("queue-take", QueueBot, "QUEUE"),
    ("startup-clamp", NodeBot, "NODE"),
)
ADAPTERS = {"infoflow": InfoflowAdapter, "feishu": FeishuAdapter}
MESSAGES = Path(__file__).parent / "fixtures" / "resource-reminder-messages.json"


def capture_message(path, case, bot_class, bot_type, adapter_name, lang):
    cards = bot_type in {"DEVICE", "QUEUE_DEVICE"}
    adapter = ADAPTERS[adapter_name]()
    sent = []
    adapter.send = sent.append
    config = {
        "BOT_TYPE": bot_type,
        "BOT_ID": "fixture",
        "DATA_DIR": str(path),
        "CLUSTER_CONFIGS": {"node1": ["Example GPU"], "node2": ["Example GPU"]} if cards else ["node1", "node2"],
        "LANGUAGE": lang,
        "DEFAULT_DURATION": 600,
        "EARLY_NOTIFY": case.endswith("-warning"),
    }
    first = "node1 dev0" if cards else "node1"
    second = "node2 dev0" if cards else "node2"
    bot = bot_class(config_dict=config, adapter=adapter)

    def command(user, text, *, source="chat"):
        return execute_text_command("ou_" + user, text, bot, source=source)

    with patch("time.time", return_value=100000) as clock:
        if case.endswith("-combined"):
            command("holder", f"lock {first} 1m")
            command("waiter", f"book {first} 30m")
            command("expired", f"book {second} 30m")
            command("next", f"book {second} 30m")
            bot._check_and_notify()
            sent.clear()
            clock.return_value += 301
            bot._check_and_notify()
        elif case == "queue-delay":
            command("first", f"book {first} 30m")
            command("waiter", f"book {first} 30m")
            sent.append(command("first", f"lock {first} 60m"))
        elif case == "queue-take":
            command("holder", f"lock {first} 1h", source="api")
            command("unrelated", f"lock {second} 1h")
            command("waiter", f"book {first} 30m")
            sent.append(command("operator", f"take {first} 30m"))
        elif case == "startup-clamp":
            command("holder", f"lock {first} 2h", source="api")
            bot_class(config_dict={**config, "MAX_LOCK_DURATION": 600}, adapter=adapter)
        else:
            command("holder", f"lock {first} 1m")
            clock.return_value += 30 if case.endswith("-warning") else 61
            bot._check_and_notify()

    assert len(sent) == 1, case
    payload = sent[0]
    # Mentions come from a set and its iteration order varies with Python's hash seed.
    # Preserve all text, newline-derived rows, fragment ordering, and targets.
    if adapter_name == "feishu":
        payload.pop("uuid")
        post = json.loads(payload["content"])
        for row in post["zh_cn"]["content"]:
            mentions = sorted((item for item in row if item["tag"] == "at"), key=lambda item: item["user_id"])
            for i, item in enumerate(row):
                if item["tag"] == "at":
                    row[i] = mentions.pop(0)
        payload["content"] = post
    else:
        for part in payload["message"]["body"]:
            if part["type"] == "AT":
                part["atuserids"].sort()
    return payload


@pytest.mark.parametrize("case,bot_class,bot_type", CASES)
@pytest.mark.parametrize("adapter_name", ADAPTERS)
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_resource_reminder_messages_match_original(tmp_path, case, bot_class, bot_type, adapter_name, lang):
    expected = json.loads(MESSAGES.read_text(encoding="utf-8"))
    actual = capture_message(tmp_path, case, bot_class, bot_type, adapter_name, lang)
    assert actual == expected[f"{case}/{adapter_name}/{lang}"]


@pytest.mark.parametrize("operation", ["lock", "take"])
def test_queue_replies_render_after_the_state_is_saved(tmp_path, monkeypatch, operation):
    from lockbot.core.io import _bot_dir

    adapter = InfoflowAdapter()
    bot = QueueBot(
        config_dict={
            "BOT_TYPE": "QUEUE",
            "BOT_ID": "fixture",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["node1", "node2"],
            "LANGUAGE": "en",
        },
        adapter=adapter,
    )
    execute_text_command("prior-user", "lock node2 1h", bot)
    if operation == "take":
        execute_text_command("prior-user", "lock node1 1h", bot)
    original = adapter.build_reply
    monkeypatch.setattr(bot, "_get_site_value", lambda key: "true" if key == "agent_promotion_enabled" else "")
    monkeypatch.setattr(bot, "_agent_skill_url", lambda: "https://example.invalid/skill")

    def build_reply(content, users, group_id=None):
        assert not bot._lock.locked()
        saved = json.loads((Path(_bot_dir(bot.config)) / "bot_state.json").read_text())
        assert saved["bot_state"]["node1"]["current_users"][0]["user_id"] == "actor"
        return original(content, users, group_id)

    monkeypatch.setattr(adapter, "build_reply", build_reply)
    payload = execute_text_command("actor", f"{operation} node1 30m", bot)
    parts = payload["message"]["body"]
    if operation == "lock":
        assert [item["type"] for item in parts] == ["TEXT", "TEXT", "LINK", "TEXT", "TEXT", "AT"]
        assert parts[0]["content"].endswith("\n\n")
        assert parts[2]["href"] == "https://example.invalid/skill"
        assert parts[3]["content"] == parts[4]["content"] == "\n"
    else:
        assert "node2" not in parts[0]["content"]
        assert set(parts[1]["atuserids"]) == {"actor", "prior-user"}
