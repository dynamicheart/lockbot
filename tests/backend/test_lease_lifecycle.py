"""Real allocator mutations, not HTTP-success guesses, prove grant time."""

import importlib.util
import json
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import lease_history as lifecycle
from lockbot.backend.app.resource_api.event_log import read_history
from lockbot.core.bot_instance import BotInstance
from lockbot.core.command_context import operation_attempt
from lockbot.core.handler import execute_text_command

spec = importlib.util.spec_from_file_location(
    "lease_report", Path(__file__).parents[2] / "scripts/lease_history/report.py"
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(lifecycle.time, "time", lambda: value[0])
    return value


def make_bot(tmp_path, clock, kind="NODE"):
    cards = kind in {"DEVICE", "QUEUE_DEVICE"}
    cluster = {"n1": ["gpu", "gpu"]} if cards else {"n1": "gpu", "n2": "gpu"}
    bot = BotInstance(
        kind,
        {
            "BOT_ID": 1,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": cluster,
            "TIME_ALERT": 5,
            "EARLY_NOTIFY": True,
            "DEFAULT_DURATION": 60,
        },
    ).bot
    bot.adapter.send = lambda message: [(200, "ok")]
    journal = lifecycle.LeaseJournal(1, bot)
    bot.config._lifecycle_observer = journal
    return bot, journal, "n1 dev0" if cards else "n1"


def frames():
    return read_history(1, limit=100, stream="lifecycle")["items"]


@pytest.mark.parametrize("kind", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"])
def test_real_acquire_renew_and_free_account_once(tmp_path, clock, kind):
    bot, journal, target = make_bot(tmp_path, clock, kind)
    clock[0] = 1001
    execute_text_command("alice", f"lock {target} 10m", bot, source="api")
    clock[0] = 1011
    execute_text_command("alice", f"lock {target} 5m", bot, source="chat")
    clock[0] = 1031
    execute_text_command("alice", f"free {target}", bot, source="api")
    data = frames()
    events = [event for frame in data for event in frame["events"]]
    assert [event["kind"] for event in events].count("acquired") == 1
    assert [event["kind"] for event in events].count("renewed") == 1
    assert [event["kind"] for event in events].count("released") == 1
    assert {frame["source"] for frame in data} >= {"chat", "api"}
    result = report.analyze(data + data, 1000, 1031, journal.checkpoint(bot.state.bot_state))
    assert result["coverage_complete"] is True
    unit = "device" if kind in {"DEVICE", "QUEUE_DEVICE"} else "node"
    assert result["verified_allocated_seconds"] == {f"{unit}_exclusive_subject_grant_seconds": 30}
    assert "holding_id" not in json.dumps(data) and "purpose" not in json.dumps(data)


@pytest.mark.parametrize("kind", ["QUEUE", "QUEUE_DEVICE"])
def test_booking_claim_expiry_and_claim_timeout_are_actual_state_events(tmp_path, clock, kind):
    bot, journal, target = make_bot(tmp_path, clock, kind)
    clock[0] += 1
    execute_text_command("alice", f"book {target} 1m", bot, source="chat")
    execute_text_command("bob", f"book {target} 1m", bot, source="api")
    clock[0] += 1
    bot._check_and_notify()
    clock[0] += 1
    execute_text_command("alice", f"lock {target}", bot, source="api")
    clock[0] += 65
    bot._check_and_notify()
    clock[0] += 301
    bot._check_and_notify()
    data = frames()
    kinds = [event["kind"] for frame in data for event in frame["events"]]
    assert kinds.count("booked") == 2
    assert "claimed" in kinds and "claim_ready" in kinds and "expired" in kinds and "claim_timeout" in kinds
    result = report.analyze(data, 1000, clock[0], journal.checkpoint(bot.state.bot_state))
    assert result["coverage_complete"]
    assert sum(result["verified_allocated_seconds"].values()) == 60  # Never count waiting/claim windows or sweep delay.
    assert all(segment["actor"]["external_user_id"] == "alice" for segment in result["verified_segments"])


def test_late_renewal_does_not_backfill_expired_gap(tmp_path, clock):
    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    clock[0] = 1070  # No sweep yet; core still has the old holding incarnation.
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    clock[0] = 1080
    execute_text_command("alice", f"free {target}", bot, source="api")
    data = frames()
    assert any(e.get("late") for f in data for e in f["events"] if e["kind"] == "renewed")
    result = report.analyze(data, 1000, 1080, journal.checkpoint(bot.state.bot_state))
    assert result["verified_allocated_seconds"] == {"node_exclusive_subject_grant_seconds": 70}
    assert not any(s["start"] < 1070 and s["end"] > 1060 for s in result["verified_segments"])


def test_shared_grants_and_operator_release_are_separate_from_physical_time(tmp_path, clock):
    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"slock {target} 1m", bot, source="chat")
    execute_text_command("bob", f"slock {target} 1m", bot, source="api")
    clock[0] = 1010
    with operation_attempt(1, "operator", "opkick"):
        bot.do_opkick("alice", node_key="n1", reason="private-operator-reason")
    clock[0] = 1020
    execute_text_command("bob", f"free {target}", bot, source="chat")
    data = frames()
    assert any(f["source"] == "operator" and f["action"] == "opkick" for f in data)
    assert "private-operator-reason" not in json.dumps(data)
    result = report.analyze(data, 1000, 1020, journal.checkpoint(bot.state.bot_state))
    assert result["verified_allocated_seconds"] == {"node_shared_subject_grant_seconds": 30}
    assert "utilization" not in result["verified_allocated_seconds"]


def test_append_failure_preserves_operation_and_breaks_coverage_until_baseline(tmp_path, clock, monkeypatch):
    bot, journal, target = make_bot(tmp_path, clock)
    append = journal._append
    monkeypatch.setattr(journal, "_append", lambda frame: (_ for _ in ()).throw(OSError("disk failure")))
    clock[0] = 1001
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    assert bot.state.bot_state["n1"]["current_users"]
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    monkeypatch.setattr(journal, "_append", append)
    clock[0] = 1005
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    clock[0] = 1010
    execute_text_command("alice", f"free {target}", bot, source="api")
    data = frames()
    assert any(f["boundary"] == "journal_write_failed" for f in data)
    result = report.analyze(data, 1000, 1010, journal.checkpoint(bot.state.bot_state))
    assert not result["coverage_complete"] and result["verified_allocated_seconds"] is None
    assert sum(s["seconds"] for s in result["verified_segments"]) == 5
    recovered = report.analyze(data, 1005, 1010, journal.checkpoint(bot.state.bot_state))
    assert recovered["coverage_complete"] and sum(recovered["verified_allocated_seconds"].values()) == 5


def test_state_save_failure_and_restart_never_fabricate_past_intervals(tmp_path, clock, monkeypatch):
    from lockbot.core import io

    bot, journal, target = make_bot(tmp_path, clock)
    original = io.os.replace
    monkeypatch.setattr(io.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("state failure")))
    clock[0] = 1001
    with pytest.raises(OSError):
        execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    monkeypatch.setattr(io.os, "replace", original)
    clock[0] = 1005
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    clock[0] = 1008
    restarted = lifecycle.LeaseJournal(1, bot)
    bot.config._lifecycle_observer = restarted
    clock[0] = 1010
    execute_text_command("alice", f"free {target}", bot, source="api")
    result = report.analyze(frames(), 1000, 1010, restarted.checkpoint(bot.state.bot_state))
    assert result["verified_allocated_seconds"] is None
    assert any(g["reason"] == "session_boundary" for g in result["unknown_gaps"])
    recovered = report.analyze(frames(), 1008, 1010, restarted.checkpoint(bot.state.bot_state))
    assert recovered["coverage_complete"] and sum(recovered["verified_allocated_seconds"].values()) == 2


def test_rotation_oversize_baseline_and_privacy_are_explicit(tmp_path, clock, monkeypatch):
    bot, journal, target = make_bot(tmp_path, clock)
    monkeypatch.setattr(lifecycle, "MAX_FILE_BYTES", 1200)
    monkeypatch.setattr(lifecycle, "BACKUPS", 1)
    for _i in range(8):
        clock[0] += 1
        execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    result = report.analyze(frames(), 1000, clock[0], journal.checkpoint(bot.state.bot_state))
    assert result["verified_allocated_seconds"] is None
    monkeypatch.setattr(lifecycle, "MAX_LINE_BYTES", 10)
    clock[0] += 1
    execute_text_command("alice", f"free {target}", bot, source="api")
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]


def test_clock_reversal_conflicting_duplicates_and_tail_mismatch_block_verification(tmp_path, clock):
    import copy

    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"lock {target} 1m", bot, source="chat")
    clock[0] = 1020
    execute_text_command("alice", f"lock {target} 1m", bot, source="chat")
    clock[0] = 1010
    execute_text_command("alice", f"lock {target} 1m", bot, source="chat")
    clock[0] = 1030
    execute_text_command("alice", f"free {target}", bot, source="chat")
    data = frames()
    result = report.analyze(data, 1000, 1030, journal.checkpoint(bot.state.bot_state))
    assert result["verified_allocated_seconds"] is None
    assert any(g["reason"] == "clock_reversal" for g in result["unknown_gaps"])
    assert not any(s["start"] < 1020 and s["end"] > 1010 for s in result["verified_segments"])
    copied = copy.deepcopy(data[0])
    copied["timestamp"] += 1
    assert report.analyze(data + [copied], 1000, 1030)["malformed_records"] == 1
    checkpoint = journal.checkpoint(bot.state.bot_state)
    checkpoint["after_digest"] = "wrong-tail"
    assert report.analyze(data[:1], 1000, 1040, checkpoint)["verified_allocated_seconds"] is None


def test_public_frame_never_copies_private_future_values(tmp_path, clock):
    import copy

    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    raw = copy.deepcopy(frames()[0])
    raw.update(purpose="private-task", agent_label="private-agent", credential_id="private-credential")
    raw["events"][0]["purpose"] = "private-event"
    projected = lifecycle.public_frame(raw)
    assert "private-" not in json.dumps(projected)
    malformed = copy.deepcopy(raw)
    malformed["state"]["allocations"][0]["unit"] = {"private": "hidden"}
    assert lifecycle.public_frame(malformed) is None
    missing = copy.deepcopy(raw)
    missing["state"]["allocations"][0]["deadline"] = None
    assert lifecycle.public_frame(missing) is None
    duplicate = copy.deepcopy(raw)
    duplicate["state"]["allocations"] *= 2
    duplicate["after_digest"] = lifecycle.digest(duplicate["state"])
    assert lifecycle.public_frame(duplicate) is None
    assert not report.valid_state(duplicate["state"])
    for lease in raw["state"]["allocations"]:
        assert lease["ref"] not in str(bot.state.bot_state)  # Correlation reference is not the raw holding ID.


@pytest.mark.parametrize("stage", ["makedirs", "mkstemp"])
def test_prewrite_failure_always_invalidates_coverage(tmp_path, clock, monkeypatch, stage):
    from lockbot.core import io

    bot, journal, target = make_bot(tmp_path, clock)
    module = io.os if stage == "makedirs" else io.tempfile
    with monkeypatch.context() as patch:
        patch.setattr(module, stage, lambda *args, **kwargs: (_ for _ in ()).throw(OSError("prewrite failure")))
        with pytest.raises(OSError):
            execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    clock[0] += 1
    execute_text_command("alice", f"free {target}", bot, source="api")
    assert any(f["boundary"] in {"state_persist_failed", "unconfirmed_mutation"} for f in frames())


def test_concurrent_commands_and_cursor_after_append_remain_a_single_chain(tmp_path, clock):
    from concurrent.futures import ThreadPoolExecutor

    bot, journal, target = make_bot(tmp_path, clock)
    clock[0] = 1001
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: execute_text_command("alice", f"lock {target} 1m", bot, source="api"), range(8)))
    first = read_history(1, limit=1, stream="lifecycle")
    latest = first["items"][0]["sequence"]
    clock[0] = 1010
    execute_text_command("alice", f"free {target}", bot, source="api")
    older = read_history(1, limit=100, cursor=first["next_cursor"], stream="lifecycle")
    assert all(item["sequence"] < latest for item in older["items"])
    data = frames()
    assert sorted(item["sequence"] for item in data) == list(range(1, len(data) + 1))
    result = report.analyze(data, 1000, 1010, journal.checkpoint(bot.state.bot_state))
    assert result["coverage_complete"]
    assert sum(result["verified_allocated_seconds"].values()) == 9


def test_lifecycle_reader_budget_progress_and_retained_cursor_gap(tmp_path, clock, monkeypatch):
    from fastapi import HTTPException
    from lockbot.backend.app.managed_paths import get_managed_paths
    from lockbot.backend.app.resource_api import event_log

    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    path = get_managed_paths().bot_file(1, lifecycle.LOG_NAME)
    good = path.read_bytes()
    path.write_bytes(good + b"x" * 9000 + b"\n" + good)
    monkeypatch.setattr(event_log, "READ_BUDGET", 4096)
    cursor, seen, cursors = None, [], set()
    for _ in range(15):
        page = read_history(1, limit=1, cursor=cursor, stream="lifecycle")
        assert page["scanned_bytes"] <= 4096
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    assert not cursor and len(seen) == 4  # Both baseline/acquire copies survive the invalid long line.
    first = read_history(1, limit=1, stream="lifecycle")
    path.unlink()
    with pytest.raises(HTTPException) as exc:
        read_history(1, cursor=first["next_cursor"], stream="lifecycle")
    assert exc.value.status_code == 409


def test_entry_bound_and_malformed_report_cannot_claim_complete_coverage(tmp_path, clock, monkeypatch):
    bot, journal, target = make_bot(tmp_path, clock)
    execute_text_command("alice", f"lock {target} 1m", bot, source="api")
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "MAX_ENTRIES", 1)
        assert not journal.checkpoint(bot.state.bot_state)["consistent"]
    result = report.analyze(frames() + [None, {"state": {"purpose": "private-value"}}], 1000, 1001)
    assert result["verified_allocated_seconds"] is None and result["malformed_records"] == 2
    assert "private-value" not in json.dumps(result)
