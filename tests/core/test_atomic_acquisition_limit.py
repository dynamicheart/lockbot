"""Concurrency checks on real bots, using distinct requests for one identity."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from lockbot.core.device_bot import DeviceBot
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot


@pytest.mark.parametrize(
    "kind,cls,verb",
    [
        ("NODE", NodeBot, "lock"),
        ("NODE", NodeBot, "slock"),
        ("DEVICE", DeviceBot, "lock"),
        ("DEVICE", DeviceBot, "slock"),
        ("QUEUE", QueueBot, "book"),
    ],
)
def test_competing_requests_cannot_exceed_cap(tmp_path, kind, cls, verb):
    bot = cls(
        config_dict={
            "BOT_ID": "cap",
            "BOT_TYPE": kind,
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu"], "b": ["gpu"]} if kind == "DEVICE" else ["a", "b"],
            "WEBHOOK_URL": "",
            "MAX_RESOURCES_PER_USER": 1,
        }
    )
    barrier = Barrier(2)
    original = bot.parse_command

    def synchronized_parse(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=3)
        return result

    bot.parse_command = synchronized_parse

    def acquire(key):
        reply = execute_text_command("alice", f"{verb} {key} 10m", bot)
        return "capped" if "❌" in bot.adapter.extract_reply_text(reply) else "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ["a", "b"]))
    assert sorted(results) == ["accepted", "capped"]
    _, state = bot.state_snapshot()
    held = []
    for key, node in state.items():
        entries = [node] if kind != "DEVICE" else node
        if any(
            any(u["user_id"] == "alice" for u in e.get("current_users", []) + e.get("booking_list", []))
            for e in entries
        ):
            held.append(key)
    assert len(held) == 1


@pytest.mark.parametrize("kind,cls", [("NODE", NodeBot), ("DEVICE", DeviceBot), ("QUEUE", QueueBot)])
def test_multitarget_rejected_before_mutation_and_renewal_allowed(tmp_path, kind, cls):
    bot = cls(
        config_dict={
            "BOT_ID": "cap",
            "BOT_TYPE": kind,
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"a": ["gpu"], "b": ["gpu"]} if kind == "DEVICE" else ["a", "b"],
            "WEBHOOK_URL": "",
            "MAX_RESOURCES_PER_USER": 1,
        }
    )
    before = bot.state_snapshot()[1]
    assert "❌" in bot.adapter.extract_reply_text(bot.lock("alice", "lock a,b 10m"))
    assert bot.state_snapshot()[1] == before
    if kind == "QUEUE":
        bot.book("alice", "book a 10m")
    bot.lock("alice", "lock a 10m")
    bot.lock("alice", "lock a 10m")
    assert "❌" in bot.adapter.extract_reply_text(bot.lock("alice", "lock b 10m"))
