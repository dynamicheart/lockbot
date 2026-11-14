"""Aggregate journal persistence, isolation and explicit unknown coverage."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from lockbot.backend.app.service_history import ServiceHistory, parse_range
from lockbot.backend.app.service_traffic import TrafficStats


def event(journal, bot_id=1, duration=10, **kwargs):
    journal.record(
        bot_id=bot_id, source="agent", status=200, duration_ms=duration, received=3, sent=5, failed=False, **kwargs
    )


def test_restart_histogram_merge_unknown_and_offsets(tmp_path):
    now = [120.0]
    first = ServiceHistory(tmp_path, lambda: now[0])
    for _ in range(19):
        event(first, duration=1)
    now[0] = 180
    first.flush()
    # Simulated process restart: no data/coverage is invented for the intervening minute.
    now[0] = 240
    second = ServiceHistory(tmp_path, lambda: now[0])
    event(second, duration=10000)
    now[0] = 300
    second.flush()
    result = second.query(120, 300, 60)
    assert [row["coverage"] for row in result["history"]] == ["complete", "unknown", "complete"]
    assert result["history"][1]["requests"] is None
    assert result["totals"]["requests"] == 20
    assert result["totals"]["p95_ms"] == 1  # merged histogram, never average minute P95
    assert result["totals"]["request_bytes"] == 60
    assert second.query(120, 300, 60, 1)["totals"]["requests"] == 20
    assert second.query(120, 300, 60, 2)["history"][0]["coverage"] == "unknown"
    assert parse_range("1970-01-01T08:02:00+08:00", "1970-01-01T00:05:00Z", 60, 300) == (120, 300, 60)


def test_hotpath_no_io_cardinality_and_no_private_dimensions(tmp_path):
    now = [120.0]
    journal = ServiceHistory(tmp_path / "not-created", lambda: now[0])
    stats = TrafficStats(clock=lambda: now[0], history=journal)
    for number in range(300):
        stats.record(
            bot_id=number + 1,
            source="agent",
            route="GET /secret",
            status=200,
            duration_ms=3,
            credential={"id": "secret", "user": "private-person", "kind": "agent"},
        )
    assert not journal.directory.exists()
    assert len(journal.bots) == 256
    # Pending scopes must obey the same bot bound, even before periodic flush.
    now[0] = 180
    journal.flush()
    serialized = next(journal.directory.glob("*.jsonl")).read_text()
    assert "secret" not in serialized and "private-person" not in serialized
    assert journal.query(120, 180, 60)["totals"]["requests"] == 300
    assert len(serialized.encode()) < 200000


def test_partial_shutdown_idle_and_operations(tmp_path):
    now = [125.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    stats = TrafficStats(clock=lambda: now[0], history=journal)
    stats.record_operation(bot_id=1, source="agent", command="lock", outcome="dispatched")
    event(journal)
    now[0] = 150
    journal.stop()
    result = journal.query(120, 180, 60)
    assert result["history"][0]["coverage"] == "partial"
    assert result["history"][0]["observed_seconds"] == 25
    assert result["history"][0]["operations"]["agent"] == 1
    now[0] = 180
    other = ServiceHistory(tmp_path, lambda: now[0])
    now[0] = 240
    other.flush()
    assert other.query(180, 240, 60)["history"][0]["requests"] == 0
    assert other.query(180, 240, 60)["history"][0]["coverage"] == "complete"


def test_corrupt_tail_rotation_retention_disk_and_storage_failure(tmp_path):
    now = [3540.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal)
    now[0] = 3600
    journal.flush()
    path = next(tmp_path.glob("*.jsonl"))
    with path.open("ab") as stream:
        stream.write(b'{"truncated":')
    result = journal.query(3540, 3600, 60)
    assert result["storage"]["damaged_segments"]
    assert result["totals"]["requests"] == 1
    event(journal)
    now[0] = 3660
    journal.flush()
    assert len(list(tmp_path.glob("*.jsonl"))) == 2
    journal.disk_budget = 1
    journal.flush()
    assert not list(tmp_path.glob("*.jsonl"))
    old = tmp_path / "19700101T00.jsonl"
    old.write_text("{}\n")
    journal.disk_budget = 10000
    now[0] = 32 * 86400
    journal.flush()
    assert not old.exists()
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    failed = ServiceHistory(blocked, lambda: now[0])
    event(failed)
    now[0] += 60
    failed.flush()
    assert failed.error == "storage_unavailable"
    event(failed)  # write failure cannot fail request recording


def test_parallel_record_query_and_flush(tmp_path):
    now = [120.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: event(journal), range(1000)))
    now[0] = 180
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: (journal.flush(), journal.query(120, 180, 60)), range(8)))
    assert all(result[1]["totals"]["requests"] == 1000 for result in results)


@pytest.mark.parametrize(
    "start,end,resolution",
    [
        ("1970-01-01T00:00:00", "1970-01-01T00:01:00Z", 60),
        ("1970-01-01T00:00:00Z", "1970-03-01T00:01:00Z", 60),
        ("1970-01-01T00:00:00Z", "1970-01-01T00:01:00Z", 1),
        ("bad", "bad", 60),
    ],
)
def test_invalid_ranges(start, end, resolution):
    with pytest.raises(HTTPException) as error:
        parse_range(start, end, resolution, 10000000)
    assert error.value.status_code == 422


def test_history_permissions_and_scope(client, auth_header, admin_header, db_session, tmp_path):
    from lockbot.backend.app.auth.models import User

    from backend.test_bot_authz import _create_bot, _register_user

    own = _create_bot(client, auth_header)
    outsider = _register_user(client, "history-outsider", "outsider@test.com", "pass123")
    other = _create_bot(client, outsider)
    now = [120.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    client.app.state.service_history = journal
    event(journal, own)
    for _ in range(3):
        event(journal, other)
    now[0] = 180
    journal.flush()
    params = {"start": "1970-01-01T00:02:00Z", "end": "1970-01-01T00:03:00Z", "resolution_seconds": 60}
    global_path = "/api/admin/service-traffic/history"
    assert client.get(global_path, params=params).status_code == 401
    assert client.get(global_path, params=params, headers=auth_header).status_code == 403
    assert client.get(global_path, params=params, headers=admin_header).status_code == 403
    bot_path = f"/api/bots/{own}/service-traffic/history"
    assert client.get(bot_path, params=params, headers=outsider).status_code == 404
    scoped = client.get(bot_path, params=params, headers=auth_header)
    assert scoped.status_code == 200 and scoped.json()["totals"]["requests"] == 1
    assert scoped.headers["cache-control"] == "no-store"
    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    assert client.get(global_path, params=params, headers=admin_header).json()["totals"]["requests"] == 4
    assert (
        client.get(global_path, params={**params, "bot_id": own}, headers=admin_header).json()["totals"]["requests"]
        == 1
    )


def test_tail_recovery_and_restart_within_minute(tmp_path):
    now = [120.0]
    first = ServiceHistory(tmp_path, lambda: now[0])
    event(first)
    now[0] = 140
    first.stop()
    path = next(tmp_path.glob("*.jsonl"))
    with path.open("ab") as stream:
        stream.write(b'{"interrupted":')
    # Restart immediately: disjoint partial coverage can be combined to a full minute.
    second = ServiceHistory(tmp_path, lambda: now[0])
    event(second)
    now[0] = 180
    second.flush()
    result = second.query(120, 180, 60)
    assert result["history"][0]["coverage"] == "complete"
    assert result["totals"]["requests"] == 2
    assert not result["storage"]["damaged_segments"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_read_budget_and_malformed_records_remain_unknown(tmp_path, monkeypatch):
    import lockbot.backend.app.service_history as module

    now = [120.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal)
    now[0] = 180
    journal.flush()
    monkeypatch.setattr(module, "MAX_READ_BYTES", 1)
    result = journal.query(120, 180, 60)
    assert result["storage"]["query_truncated"]
    assert result["history"][0]["requests"] is None
    monkeypatch.setattr(module, "MAX_READ_BYTES", 100000)
    path = next(tmp_path.glob("*.jsonl"))
    path.write_text('{"v":1,"at":120,"bot_id":null,"coverage":[120,180],"counter":[]}\n')
    result = journal.query(120, 180, 60)
    assert result["storage"]["damaged_segments"]
    assert result["history"][0]["coverage"] == "unknown"


def test_writer_stall_and_idle_eviction_do_not_invent_coverage(tmp_path):
    now = [120.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal)
    now[0] = 720
    event(journal)
    journal.flush()
    result = journal.query(120, 720, 60)
    assert result["history"][0]["coverage"] == "unknown"
    assert len(journal.pending) <= 514


def test_hourly_index_retains_histograms_gaps_and_survives_detail_expiry(tmp_path):
    now = [0.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    for minute in range(60):
        now[0] = minute * 60
        if minute == 0:
            event(journal, duration=1)
        elif minute == 30:
            event(journal, duration=10000)
        now[0] += 60
        journal.flush()
    assert (tmp_path / "hourly" / "19700101T00.jsonl").exists()
    hourly = journal.query(0, 3600, 3600)
    assert hourly["history"][0]["coverage"] == "complete"
    assert hourly["totals"]["requests"] == 2
    assert hourly["totals"]["p95_ms"] == 10000
    assert hourly["history"][0]["sources"]["agent"] == 2
    now[0] = 27 * 3600
    journal.flush()
    assert not (tmp_path / "19700101T00.jsonl").exists()
    assert journal.query(0, 3600, 3600)["totals"]["requests"] == 2


def test_hourly_rollup_does_not_fill_restart_gap(tmp_path):
    now = [0.0]
    first = ServiceHistory(tmp_path, lambda: now[0])
    event(first)
    now[0] = 60
    first.stop()
    now[0] = 3540
    second = ServiceHistory(tmp_path, lambda: now[0])
    event(second)
    now[0] = 3600
    second.flush()
    result = second.query(0, 3600, 3600)
    assert result["history"][0]["observed_seconds"] == 120
    assert result["history"][0]["coverage"] == "partial"
    assert result["totals"]["requests"] == 2


def test_deep_json_is_unknown_and_rollup_preserves_damage(tmp_path):
    now = [3600.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    path = tmp_path / "19700101T00.jsonl"
    path.write_text("[" * 20000 + "0" + "]" * 20000 + "\n")
    before = journal.query(0, 3600, 3600)
    assert before["storage"]["damaged_segments"]
    assert before["history"][0]["requests"] is None
    assert before["history"][0]["coverage"] == "unknown"
    journal.flush()
    assert journal.error is None
    assert (tmp_path / "hourly" / path.name).exists()
    # The derived marker survives a restart and removal of original minute detail.
    path.unlink()
    restarted = ServiceHistory(tmp_path, lambda: now[0])
    after = restarted.query(0, 3600, 3600)
    assert after["storage"]["damaged_segments"]
    assert after["history"][0]["requests"] is None
    assert after["history"][0]["coverage"] == "unknown"


def test_writer_survives_unexpected_failure_and_retries(tmp_path, monkeypatch):
    journal = ServiceHistory(tmp_path, lambda: 3600)
    calls = []

    class BoundedTicks:
        def wait(self, seconds):
            return len(calls) >= 2

    def flush():
        calls.append(True)
        if len(calls) == 1:
            raise RecursionError("damaged telemetry")

    monkeypatch.setattr(journal, "stop_event", BoundedTicks())
    monkeypatch.setattr(journal, "flush", flush)
    journal._run()
    assert len(calls) == 2
    assert journal.error == "collection_failed"


def test_failed_rollup_does_not_accumulate_temporary_files(tmp_path, monkeypatch):
    now = [0.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal)
    now[0] = 60
    journal.flush()
    original = journal._append

    def fail_index(path, data):
        original(path, data)
        if path.suffix == ".tmp":
            raise OSError("full storage")

    monkeypatch.setattr(journal, "_append", fail_index)
    now[0] = 3600
    journal.flush()
    assert journal.error == "storage_unavailable"
    assert not list((tmp_path / "hourly").glob("*.tmp"))
    # Original minute detail remains available after the failed index write.
    assert journal.query(0, 3600, 3600)["totals"]["requests"] == 1


def test_backward_clock_changes_keep_pending_minutes_bounded(tmp_path):
    now = [100000.0]
    journal = ServiceHistory(tmp_path, lambda: now[0])
    for _ in range(100):
        event(journal)
        now[0] -= 60
    assert len({minute for minute, _ in journal.pending}) <= 2


def sealed_hour(directory, now):
    journal = ServiceHistory(directory, lambda: now[0])
    event(journal, duration=1)
    for minute in range(60):
        now[0] = (minute + 1) * 60
        journal.flush()
    return journal


@pytest.mark.parametrize("restart", [False, True])
def test_clock_rollback_adds_to_sealed_hour_without_replacing_its_index(tmp_path, restart):
    now = [0.0]
    journal = sealed_hour(tmp_path, now)
    index = tmp_path / "hourly/19700101T00.jsonl"
    original = index.read_bytes()
    now[0] = 1800
    if restart:
        journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal, duration=10000)
    journal.operation(1, "agent")
    journal.flush()
    now[0] = 1860
    journal.flush()
    now[0] = 3600
    journal.flush()
    assert parse_range("1970-01-01T00:00:00Z", "1970-01-01T01:00:00Z", 3600, now[0]) == (0, 3600, 3600)
    for resolution in (60, 900, 3600, 21600):
        for scope in (None, 1):
            result = journal.query(0, 3600, resolution, scope)
            assert result["totals"]["requests"] == 2
            assert result["totals"]["p95_ms"] == 10000
            assert sum(row["operations"]["agent"] for row in result["history"]) == 1
            assert result["coverage"]["unknown_buckets"] == 0
    assert index.read_bytes() == original
    late = tmp_path / "late/19700101T00.jsonl"
    assert late.exists() and late.stat().st_mode & 0o777 == 0o600
    # A second restart/read never applies the delta twice.
    restarted = ServiceHistory(tmp_path, lambda: now[0])
    assert restarted.query(0, 3600, 3600)["totals"]["requests"] == 2


def late_hour(directory, *, expire_detail=False):
    now = [0.0]
    journal = sealed_hour(directory, now)
    if expire_detail:
        now[0] = 27 * 3600
        journal.flush()
        assert not (directory / "19700101T00.jsonl").exists()
    now[0] = 1800
    journal = ServiceHistory(directory, lambda: now[0])
    event(journal)
    now[0] = 1860
    journal.flush()
    return journal, now


def test_late_hour_preserves_compacted_counts_after_detail_expiry(tmp_path):
    journal, now = late_hour(tmp_path, expire_detail=True)
    now[0] = 3600
    result = journal.query(0, 3600, 3600)
    assert result["totals"]["requests"] == 2
    assert result["history"][0]["coverage"] == "complete"
    detail = journal.query(0, 3600, 60)
    assert detail["totals"]["requests"] == 1
    assert detail["coverage"]["unknown_buckets"] == 59


def test_clock_rollback_before_process_start_retains_new_observations(tmp_path):
    now = [0.0]
    sealed_hour(tmp_path, now)
    now[0] = 7200
    journal = ServiceHistory(tmp_path, lambda: now[0])
    now[0] = 1800
    event(journal)
    now[0] = 1860
    journal.flush()
    for resolution in (60, 3600):
        assert journal.query(0, 3600, resolution)["totals"]["requests"] == 2


@pytest.mark.parametrize("failure", ["damaged", "read_budget"])
def test_unread_late_records_never_leave_a_complete_hour(tmp_path, monkeypatch, failure):
    import lockbot.backend.app.service_history as module

    journal, _ = late_hour(tmp_path)
    if failure == "damaged":
        with (tmp_path / "late/19700101T00.jsonl").open("ab") as stream:
            stream.write(b"invalid completed record\n")
    else:
        monkeypatch.setattr(module, "MAX_READ_BYTES", (tmp_path / "hourly/19700101T00.jsonl").stat().st_size)
    result = journal.query(0, 3600, 3600)
    assert result["history"][0]["coverage"] == "partial"
    assert result["storage"]["damaged_segments" if failure == "damaged" else "query_truncated"]


def test_late_segments_share_disk_budget_and_expire_after_retention(tmp_path):
    journal, now = late_hour(tmp_path)
    late = tmp_path / "late/19700101T00.jsonl"
    journal.disk_budget = late.stat().st_size + 1
    journal._cleanup(now[0])
    assert late.exists()
    assert not (tmp_path / "hourly/19700101T00.jsonl").exists()
    assert sum(path.stat().st_size for path in tmp_path.rglob("*.jsonl")) <= journal.disk_budget
    assert journal.query(0, 3600, 3600)["history"][0]["coverage"] == "partial"
    now[0] = 32 * 86400
    journal._cleanup(now[0])
    assert not late.exists()


def test_failed_late_write_does_not_replace_sealed_history(tmp_path, monkeypatch):
    now = [0.0]
    sealed_hour(tmp_path, now)
    index = tmp_path / "hourly/19700101T00.jsonl"
    original = index.read_bytes()
    now[0] = 1800
    journal = ServiceHistory(tmp_path, lambda: now[0])
    event(journal)

    def unavailable(*args):
        raise OSError("unavailable history storage")

    monkeypatch.setattr(journal, "_append", unavailable)
    now[0] = 1860
    journal.flush()
    assert journal.error == "storage_unavailable"
    assert index.read_bytes() == original
    assert journal.query(0, 3600, 3600)["totals"]["requests"] == 1
    event(journal)  # Collection failure does not prevent resource-path recording.
