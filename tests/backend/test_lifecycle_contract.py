# ruff: noqa: F811
"""Crash evidence and frozen exports must never manufacture complete coverage."""

import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.resource_api import lease_contract, lease_history, lifecycle_feed
from lockbot.core.handler import execute_text_command

from backend.test_lease_lifecycle import clock, make_bot  # noqa: F401


def records():
    path = get_managed_paths().bot_file(1, lease_history.LOG_NAME)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def identity():
    return SimpleNamespace(id=1, uuid="bot-uuid", identity_revision=0, api_key="operator-key")


def test_atomic_outbox_replays_crash_after_state_replace_exactly_once(tmp_path, clock, monkeypatch):
    bot, journal, _ = make_bot(tmp_path, clock)
    original = journal._append
    monkeypatch.setattr(journal, "_append", lambda frame: (_ for _ in ()).throw(OSError("unavailable")))
    clock[0] += 1
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    persisted = json.loads(get_managed_paths().state_file(1).read_text())
    pending = persisted["lifecycle_outbox"]["pending"][-1]
    assert pending["state"]["allocations"] and pending["contract"]["atomic_state"]
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    monkeypatch.setattr(journal, "_append", original)
    restarted = lease_history.LeaseJournal(1, bot)
    frame_id = pending["contract"]["frame_id"]
    assert sum(row["contract"]["frame_id"] == frame_id for row in records()) == 1
    assert restarted.outbox.journal_id == journal.outbox.journal_id
    assert restarted.outbox.sequence > pending["contract"]["commit_sequence"]
    bot.config._lifecycle_observer = restarted
    clock[0] += 1
    execute_text_command("alice", "lock n1 20m", bot, source="chat")
    renewed = lease_contract.contract_frame(records()[-1])
    assert renewed["events"][0]["kind"] == "renewed"
    assert renewed["events"][0]["version"] == 2
    assert renewed["contract"]["initiator"] == {
        "kind": "platform_user",
        "platform": "infoflow",
        "external_user_id": "alice",
    }
    lease_history.LeaseJournal(1, bot)
    assert sum(row["contract"]["frame_id"] == frame_id for row in records()) == 1


def test_failed_replace_does_not_publish_uncommitted_acquisition(tmp_path, clock, monkeypatch):
    from lockbot.core import io

    bot, journal, _ = make_bot(tmp_path, clock)
    baseline = len(records())
    original = io.os.replace
    monkeypatch.setattr(io.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("full")))
    clock[0] += 1
    with pytest.raises(OSError):
        execute_text_command("alice", "lock n1 10m", bot, source="api")
    assert len(records()) == baseline and journal.outbox.pending == []
    assert journal.checkpoint(bot.state.bot_state)["integrity_boundary"] == "state_persist_failed"
    monkeypatch.setattr(io.os, "replace", original)
    # The allocator's in-memory error recovery is outside the journal; resync is a boundary.
    clock[0] += 1
    from lockbot.core.io import save_bot_state_to_file

    save_bot_state_to_file(bot.state.bot_state, bot.config)
    assert records()[-1]["boundary"] == "state_persist_failed"
    assert records()[-1]["events"] == []


def test_outbox_overflow_is_persisted_gap_not_silent_eviction(tmp_path, clock, monkeypatch):
    bot, journal, _ = make_bot(tmp_path, clock)
    monkeypatch.setattr(lease_contract, "MAX_PENDING_FRAMES", 2)
    monkeypatch.setattr(journal, "_append", lambda frame: (_ for _ in ()).throw(OSError("offline")))
    for command in ("lock n1 10m", "lock n1 11m", "lock n1 12m"):
        clock[0] += 1
        execute_text_command("alice", command, bot, source="api")
    envelope = json.loads(get_managed_paths().state_file(1).read_text())["lifecycle_outbox"]
    assert len(envelope["pending"]) == 1
    assert envelope["pending"][0]["boundary"] == "outbox_overflow"
    assert envelope["pending"][0]["events"] == []


def test_frozen_cursor_binds_scope_and_excludes_new_equal_timestamp_frames(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    clock[0] = 1001
    execute_text_command("alice", "lock n1 11m", bot, source="api")
    first = lifecycle_feed.read_feed(identity(), limit=1, start=1000, end=2000)
    first_id = first["items"][0]["contract"]["frame_id"]
    cursor = first["next_cursor"]
    execute_text_command("alice", "lock n1 20m", bot, source="api")
    later_id = records()[-1]["contract"]["frame_id"]
    exported = [first_id]
    while cursor:
        page = lifecycle_feed.read_feed(identity(), cursor=cursor, limit=1, start=1000, end=2000)
        exported.extend(row["contract"]["frame_id"] for row in page["items"])
        cursor = page["next_cursor"]
    assert first_id != later_id and later_id not in exported
    assert len(exported) == len(set(exported))
    for changes in ({"resource": "n1"}, {"start": 999}, {"end": 1999}, {"limit": 2}):
        kwargs = {"cursor": first["next_cursor"], "limit": 1, "start": 1000, "end": 2000, **changes}
        with pytest.raises(HTTPException) as caught:
            lifecycle_feed.read_feed(identity(), **kwargs)
        assert caught.value.status_code == 400
    foreign = identity()
    foreign.id = 2
    with pytest.raises(HTTPException):
        lifecycle_feed.read_feed(foreign, cursor=first["next_cursor"], limit=1, start=1000, end=2000)
    assert page["coverage"]["complete_history"] is False


def test_rotation_retains_frozen_files_until_eviction_and_cursor_expires(tmp_path, clock, monkeypatch):
    bot, journal, _ = make_bot(tmp_path, clock)
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    first = lifecycle_feed.read_feed(identity(), limit=1)
    path = get_managed_paths().bot_file(1, lease_history.LOG_NAME)
    path.rename(path.with_name(path.name + ".1"))
    path.write_text("")
    assert lifecycle_feed.read_feed(identity(), cursor=first["next_cursor"], limit=1)["items"]
    path.with_name(path.name + ".1").unlink()
    with pytest.raises(HTTPException) as caught:
        lifecycle_feed.read_feed(identity(), cursor=first["next_cursor"], limit=1)
    assert caught.value.status_code == 409
    clock[0] += lifecycle_feed.CURSOR_SECONDS + 1
    with pytest.raises(HTTPException) as caught:
        lifecycle_feed.read_feed(identity(), cursor=first["next_cursor"], limit=1)
    assert caught.value.status_code == 409


def test_closed_contract_projection_ignores_private_imported_fields(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    row = copy.deepcopy(records()[-1])
    row["purpose"] = "secret-purpose"
    row["contract"]["initiator"]["purpose"] = "secret-purpose"
    row["contract"]["private"] = "secret-purpose"
    row["events"][0]["raw_command"] = "secret-purpose"
    safe = lease_contract.contract_frame(row)
    assert "secret-purpose" not in json.dumps(safe)
    assert len(safe["events"][0]["event_id"]) == 64
    assert lease_contract.contract_frame([]) is None
    assert lease_contract.contract_frame({}) is None


def test_complete_window_requires_atomic_anchor_chain_and_live_checkpoint(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1001
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    clock[0] = 1010
    execute_text_command("alice", "lock n1 20m", bot, source="api")
    clock[0] = 1020
    execute_text_command("alice", "free n1", bot, source="chat")
    clock[0] = 1030
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint.pop("state")
    result = lifecycle_feed.read_feed(identity(), start=1000, end=1030, checkpoint=checkpoint)
    assert result["coverage"]["complete_window"]
    assert result["coverage"]["start_anchor"]["state"]["allocations"] == []
    events = [event for row in reversed(result["items"]) for event in row["events"]]
    assert [event["kind"] for event in events] == ["acquired", "renewed", "released"]
    assert len({event["lease_ref"] for event in events}) == 1
    assert [event["version"] for event in events] == [1, 2, 3]
    result = lifecycle_feed.read_feed(identity(), start=1000, end=1030)
    assert not result["coverage"]["complete_window"]
    broken = copy.deepcopy(checkpoint)
    broken["after_digest"] = "0" * 64
    assert not lifecycle_feed.read_feed(identity(), start=1000, end=1030, checkpoint=broken)["coverage"][
        "complete_window"
    ]


def test_durable_order_fsyncs_file_before_replace_and_directory_after(tmp_path, clock, monkeypatch):
    import stat

    from lockbot.core import io

    bot, journal, _ = make_bot(tmp_path, clock)
    calls = []
    fsync, replace = io.os.fsync, io.os.replace

    def synced(fd):
        calls.append("directory" if stat.S_ISDIR(io.os.fstat(fd).st_mode) else "file")
        return fsync(fd)

    def replaced(*args):
        calls.append("replace")
        return replace(*args)

    monkeypatch.setattr(io.os, "fsync", synced)
    monkeypatch.setattr(io.os, "replace", replaced)
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    assert calls[:3] == ["file", "replace", "directory"]
    assert calls[3:] == ["file", "directory"]
