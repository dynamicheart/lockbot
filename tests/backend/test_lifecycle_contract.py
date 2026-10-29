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


@pytest.mark.parametrize("persisted_uuid", [None, "foreign-bot-uuid"])
def test_outbox_recovery_requires_matching_bot_uuid(tmp_path, clock, monkeypatch, persisted_uuid):
    bot, journal, _ = make_bot(tmp_path, clock)
    monkeypatch.setattr(journal, "_append", lambda frame: (_ for _ in ()).throw(OSError("offline")))
    clock[0] += 1
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    path = get_managed_paths().state_file(1)
    raw = json.loads(path.read_text())
    envelope = raw["lifecycle_outbox"]
    assert envelope["bot_uuid"] == "bot-uuid"
    old_journal = envelope["pending"][-1]["contract"]["journal_id"]
    if persisted_uuid is None:
        envelope.pop("bot_uuid")
    else:
        envelope["bot_uuid"] = persisted_uuid
    path.write_text(json.dumps(raw))
    original_bytes = path.read_bytes()
    restarted = lease_history.LeaseJournal(1, bot)
    assert not restarted.outbox.recovered
    assert restarted.outbox.journal_id != old_journal
    assert path.read_bytes() == original_bytes
    assert records()[-1]["boundary"] == "session_start"
    assert records()[-1]["contract"]["atomic_state"] is False


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


def test_interrupted_append_retries_without_duplicate_frame(tmp_path, clock, monkeypatch):
    bot, journal, _ = make_bot(tmp_path, clock)
    original = journal._append
    written = []

    def append_then_fail(frame):
        original(frame)
        written.append(frame["contract"]["frame_id"])
        raise OSError("acknowledgement interrupted")

    monkeypatch.setattr(journal, "_append", append_then_fail)
    clock[0] = 1001
    execute_text_command("alice", "lock n1 10m", bot, source="api")
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    monkeypatch.setattr(journal, "_append", original)
    clock[0] = 1002
    execute_text_command("alice", "lock n1 20m", bot, source="api")
    assert sum(row["contract"]["frame_id"] == written[0] for row in records()) == 1


def test_expiry_and_reacquisition_keep_system_actor_and_new_holding_identity(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1001
    execute_text_command("alice", "lock n1 1m", bot, source="api")
    acquired = lease_contract.contract_frame(records()[-1])["events"][0]
    clock[0] = 1100
    bot.state.bot_state["n1"]["current_users"] = []
    bot.state.bot_state["n1"]["status"] = "idle"
    from lockbot.core.io import save_bot_state_to_file

    save_bot_state_to_file(bot.state.bot_state, bot.config)
    expired = lease_contract.contract_frame(records()[-1])
    assert expired["contract"]["initiator"] == {"kind": "system"}
    assert expired["events"][0]["kind"] == "expired"
    assert expired["events"][0]["occurred_at"] == 1061
    assert expired["events"][0]["recorded_at"] == 1100
    clock[0] = 1101
    execute_text_command("bob", "lock n1 1m", bot, source="chat")
    reacquired = lease_contract.contract_frame(records()[-1])["events"][0]
    assert reacquired["lease_ref"] != acquired["lease_ref"] and reacquired["version"] == 1


def test_resource_filter_validates_unfiltered_chain_and_projects_anchor(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1001
    execute_text_command("alice", "lock n2 1m", bot, source="chat")
    clock[0] = 1002
    execute_text_command("bob", "lock n1 1m", bot, source="api")
    clock[0] = 1003
    execute_text_command("alice", "free n2", bot, source="chat")
    clock[0] = 1004
    execute_text_command("bob", "free n1", bot, source="api")
    clock[0] = 1010
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint.pop("state")
    result = lifecycle_feed.read_feed(identity(), resource="n1", start=1001, end=1010, checkpoint=checkpoint)
    assert result["coverage"]["complete_window"]
    assert result["coverage"]["start_anchor"]["state"]["allocations"] == []
    assert result["coverage"]["start_anchor"]["state_digest_scope"] == "whole_bot_before_resource_projection"
    assert all(event["resource"] == "n1" for row in result["items"] for event in row["events"])
    assert all(lease["resource"] == "n1" for row in result["items"] for lease in row["state"]["allocations"])


def test_recovery_reconciles_exact_state_file_before_replaying_journal(tmp_path, clock, monkeypatch):
    import os
    import stat

    bot, journal, _ = make_bot(tmp_path, clock)
    monkeypatch.setattr(journal, "_append", lambda frame: (_ for _ in ()).throw(OSError("unavailable")))
    clock[0] = 1001
    execute_text_command("alice", "lock n1 1m", bot, source="api")
    state_info = get_managed_paths().state_file(1).stat()
    calls, synced = [], os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        calls.append(
            "state" if info.st_ino == state_info.st_ino else ("directory" if stat.S_ISDIR(info.st_mode) else "journal")
        )
        return synced(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    lease_history.LeaseJournal(1, bot)
    assert calls[:2] == ["state", "directory"]
    assert "journal" in calls[2:]


def test_shared_holder_and_operator_actor_are_separate_references(tmp_path, clock):
    from lockbot.core.command_context import operation_attempt

    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1001
    execute_text_command("alice", "slock n1 1m", bot, source="chat")
    clock[0] = 1002
    execute_text_command("bob", "slock n1 1m", bot, source="api")
    acquired = lease_contract.contract_frame(records()[-1])
    assert len(acquired["state"]["allocations"]) == 2
    refs = {lease["actor"]["external_user_id"]: lease["ref"] for lease in acquired["state"]["allocations"]}
    assert refs["alice"] != refs["bob"]
    clock[0] = 1010
    with operation_attempt(1, "operator", "opkick"):
        bot.do_opkick("alice", node_key="n1", reason="never-public-operator-reason")
    released = lease_contract.contract_frame(records()[-1])
    assert released["contract"]["initiator"] == {"kind": "operator", "bot_id": 1}
    assert released["events"][0]["lease_ref"] == refs["alice"]
    assert released["events"][0]["version"] == 2
    assert released["state"]["allocations"][0]["ref"] == refs["bob"]
    assert "never-public" not in json.dumps(released)


def test_acknowledged_frame_identity_memory_is_bounded(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    for minute in range(10):
        clock[0] += 1
        execute_text_command("alice", f"lock n1 {minute + 1}m", bot, source="api")
    assert journal.outbox.pending == []
    assert journal.outbox.known == set()
    assert len(journal.outbox.versions) == 1


def test_checkpoint_clock_reversal_cannot_verify_a_window_without_another_write(tmp_path, clock):
    bot, journal, _ = make_bot(tmp_path, clock)
    clock[0] = 1010
    execute_text_command("alice", "lock n1 1m", bot, source="api")
    clock[0] = 1005
    checkpoint = journal.checkpoint(bot.state.bot_state)
    assert checkpoint["consistent"] is False
    assert checkpoint["integrity_boundary"] == "clock_reversal"
    assert not lifecycle_feed.read_feed(identity(), start=1000, end=1005, checkpoint=checkpoint)["coverage"][
        "complete_window"
    ]
