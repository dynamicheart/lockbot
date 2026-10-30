# ruff: noqa: F811
"""The stdlib consumer must accept real frozen feed projections, not just mock JSON."""

import importlib.util
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import lifecycle_feed
from lockbot.core.handler import execute_text_command

from backend.test_lease_lifecycle import clock, make_bot  # noqa: F401
from backend.test_lifecycle_contract import identity

_spec = importlib.util.spec_from_file_location(
    "v2_consumer", Path(__file__).parents[2] / "scripts/lease_history/report_v2.py"
)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)


@pytest.mark.parametrize("resource", ["", "n1"])
def test_real_server_anchor_projection_and_frozen_pages_feed_v2_report(tmp_path, clock, resource):
    bot, journal, _ = make_bot(tmp_path, clock)
    for timestamp, command in [(1001, "lock n1 10m"), (1010, "lock n1 20m"), (1020, "free n1")]:
        clock[0] = timestamp
        execute_text_command("alice", command, bot, source="api")
    clock[0] = 1030
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint.pop("state")
    selected = consumer.scope("https://lockbot.example.invalid", 1, 1000, 1030, resource, page_size=1)
    parameters = []

    def fetch(scope, key, cursor):
        parameters.append((scope.copy(), cursor))
        assert key == "synthetic-op-key"
        return lifecycle_feed.read_feed(
            identity(),
            start=scope["start"],
            end=scope["end"],
            resource=scope["resource"],
            limit=scope["page_size"],
            cursor=cursor,
            checkpoint=checkpoint,
        )

    exported = consumer.collect(selected, "synthetic-op-key", fetch=fetch, pause=lambda _: None)
    assert len(parameters) >= 3
    result = consumer.analyze(exported)
    assert result["coverage_complete"]
    assert result["subject_grant_seconds"] == {"node_exclusive_subject_grant_seconds": 19}
    assert result["committed_events"] == {"acquired": 1, "renewed": 1, "released": 1}
    assert result["event_count"] == 3


def test_real_restart_boundary_stays_unknown_for_consumer(tmp_path, clock):
    from lockbot.backend.app.resource_api.lease_history import LeaseJournal

    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1001
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    clock[0] = 1010
    journal = LeaseJournal(1, bot)
    bot.config._lifecycle_observer = journal
    clock[0] = 1030
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint.pop("state")
    page = lifecycle_feed.read_feed(identity(), start=1000, end=1030, checkpoint=checkpoint)
    exported = {
        "export_schema_version": 2,
        "scope": consumer.scope("https://lockbot.example.invalid", 1, 1000, 1030),
        "collection_status": "exhausted",
        "pages": [page],
    }
    result = consumer.analyze(exported)
    assert not result["coverage_complete"] and result["subject_grant_seconds"] is None


def test_real_filtered_sequence_gaps_and_omitted_tip_are_not_false_missing_history(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    for stamp, user, command in [
        (1001, "alice", "lock n2 1m"),
        (1002, "bob", "lock n1 1m"),
        (1003, "alice", "free n2"),
        (1004, "bob", "free n1"),
        (1005, "alice", "lock n2 1m"),
    ]:
        clock[0] = stamp
        execute_text_command(user, command, bot, source="chat")
    clock[0] = 1010
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint.pop("state")
    page = lifecycle_feed.read_feed(identity(), resource="n1", start=1000, end=1010, checkpoint=checkpoint)
    assert page["coverage"]["complete_window"]
    assert page["checkpoint"]["contract"]["commit_sequence"] > page["items"][0]["contract"]["commit_sequence"]
    result = consumer.analyze(
        {
            "export_schema_version": 2,
            "scope": consumer.scope("https://lockbot.example.invalid", 1, 1000, 1010, "n1"),
            "collection_status": "exhausted",
            "pages": [page],
        }
    )
    assert result["coverage_complete"]
    assert result["subject_grant_seconds"] == {"node_exclusive_subject_grant_seconds": 2}
