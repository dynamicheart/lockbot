# ruff: noqa: F811
"""Durable aggregation never substitutes for disclosure authorization or commit."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit import delegation
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.audit.read_windows import AuditReadWindow, record_read
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.test_audit_note_delegation import history_url, setup  # noqa: F401


def test_repeated_reads_and_same_denials_have_durable_totals(client, db_session, setup, monkeypatch):
    instant = delegation._now()
    monkeypatch.setattr(delegation, "_now", lambda: instant)
    # Freeze within the actual fixture credential lifetime, independent of CI clock.
    for _ in range(3):
        assert client.get(history_url(setup), headers=setup["headers"]).status_code == 200
    for _ in range(2):
        assert (
            client.get(f"/api/audit/bots/{setup['other']}/agent-history", headers=setup["headers"]).status_code == 403
        )
    rows = db_session.query(AuditLog).filter(AuditLog.action.in_(["task_notes.read", "task_notes.denied"])).all()
    assert len(rows) == 2
    details = {row.action: json.loads(row.detail) for row in rows}
    assert details["task_notes.read"]["request_count"] == 3
    assert details["task_notes.read"]["returned_count"] == 9
    assert details["task_notes.denied"]["request_count"] == 2
    assert details["task_notes.denied"]["returned_count"] == 0
    assert details["task_notes.denied"]["reason"] == "bot_scope"
    encoded = json.dumps(details)
    for canary in ("NOTE_CANARY", "AGENT_CANARY", setup["grant"]["credential"], "RAW_COMMAND_CANARY"):
        assert canary not in encoded
    assert db_session.query(AuditReadWindow).count() == 2


def _inputs():
    return dict(
        user=SimpleNamespace(
            id=1, username="fixture", role="super_admin", effective_token_version=1, must_change_password=False
        ),
        grant=SimpleNamespace(
            public_id="fixture-public",
            generation=1,
            scope="task_notes:read",
            issuer_token_version=1,
            bots='[{"id": 1, "uuid": "stable"}]',
        ),
        action="task_notes.read",
        bot=SimpleNamespace(uuid="stable", identity_revision=0, user_id=1, is_deleted=False),
        bot_id=1,
        surface="agent_history",
        reason=None,
        result="success",
        count=3,
        ip="127.0.0.1",
        now=datetime(2026, 10, 30, 0, 1),
    )


@pytest.fixture
def durable(tmp_path):
    path = tmp_path / "audit.sqlite"
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 20})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine), engine
    engine.dispose()


def test_concurrent_first_bucket_and_counts_survive_new_sessions_and_restart(durable):
    factory, engine = durable

    def write(_):
        with factory() as db:
            record_read(db, **_inputs())
            db.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    engine.dispose()  # New connections do not rely on a process-local bucket cache.
    write(None)
    with factory() as db:
        window = db.query(AuditReadWindow).one()
        detail = json.loads(db.get(AuditLog, window.audit_id).detail)
        assert window.request_count == detail["request_count"] == 41
        assert window.returned_count == detail["returned_count"] == 123
        assert db.query(AuditLog).count() == 1


def test_rollback_leaves_no_orphan_and_does_not_increment_existing_counter(durable):
    factory, _ = durable
    with factory() as db:
        record_read(db, **_inputs())
        db.rollback()
        assert db.query(AuditReadWindow).count() == db.query(AuditLog).count() == 0
    with factory() as db:
        record_read(db, **_inputs())
        db.commit()
        record_read(db, **_inputs())
        db.rollback()
        assert db.query(AuditReadWindow).one().request_count == 1
        assert json.loads(db.query(AuditLog).one().detail)["request_count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "principal",
        "generation",
        "scope",
        "grant_scope",
        "issuer",
        "issuer_id",
        "bot",
        "bot_id",
        "surface",
        "reason",
        "ip",
        "window",
        "action",
    ],
)
def test_safety_boundaries_do_not_share_windows(durable, change):
    factory, _ = durable
    values = _inputs()
    with factory() as db:
        record_read(db, **values)
        db.commit()
    if change == "principal":
        values["grant"].public_id = "other-public"
    elif change == "scope":
        values["grant"].scope = "different-scope"
    elif change == "grant_scope":
        values["grant"].bots = '[{"id": 2, "uuid": "other"}]'
    elif change == "issuer_id":
        values["user"].id = 2
    elif change == "bot_id":
        values["bot_id"] = 2
    elif change == "generation":
        values["grant"].generation += 1
    elif change == "issuer":
        values["user"].effective_token_version += 1
    elif change == "bot":
        values["bot"].identity_revision += 1
    elif change == "surface":
        values["surface"] = "current_task_notes"
    elif change == "reason":
        values["reason"] = "issuer_authority"
    elif change == "ip":
        values["ip"] = "127.0.0.2"
    elif change == "action":
        values.update(action="task_notes.denied", result="failure", count=0)
    else:
        values["now"] += timedelta(minutes=5)
    with factory() as db:
        record_read(db, **values)
        db.commit()
        assert db.query(AuditReadWindow).count() == db.query(AuditLog).count() == 2


def test_clock_rollback_keeps_minimum_first_and_maximum_last(durable):
    factory, _ = durable
    with factory() as db:
        for minute in (3, 1, 4, 2):
            values = _inputs()
            values["now"] = datetime(2026, 10, 30, 0, minute)
            record_read(db, **values)
            db.commit()
        window = db.query(AuditReadWindow).one()
        assert window.first_at.minute == 1 and window.last_at.minute == 4
        assert window.request_count == 4


def test_audit_commit_failure_does_not_disclose_or_persist_counts(client, db_session, setup, monkeypatch):
    from sqlalchemy.orm import Session

    def fail(db):
        raise RuntimeError("fixture commit unavailable")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError):
        client.get(history_url(setup), headers=setup["headers"])
    db_session.rollback()
    assert db_session.query(AuditReadWindow).count() == 0
    assert db_session.query(AuditLog).filter_by(action="task_notes.read").count() == 0


def test_damaged_summary_fails_closed_without_overwriting_evidence(durable):
    factory, _ = durable
    with factory() as db:
        record_read(db, **_inputs())
        db.commit()
        log = db.query(AuditLog).one()
        log.detail = '{"aggregation_version": 99}'
        db.commit()
        with pytest.raises((RuntimeError, KeyError), match="inconsistent|first_at"):
            record_read(db, **_inputs())
        db.rollback()
        assert db.query(AuditReadWindow).one().request_count == 1
        assert db.query(AuditLog).one().detail == '{"aggregation_version": 99}'


def test_new_table_is_additive_for_existing_database(durable):
    from sqlalchemy import inspect

    factory, engine = durable
    AuditReadWindow.__table__.drop(engine)
    with factory() as db:
        db.add(
            AuditLog(
                operator_username="legacy", operator_role="super_admin", action="legacy.event", detail='{"kept":true}'
            )
        )
        db.commit()
    Base.metadata.create_all(engine)
    assert "audit_read_windows" in inspect(engine).get_table_names()
    with factory() as db:
        record_read(db, **_inputs())
        db.commit()
        assert db.query(AuditLog).filter_by(action="legacy.event").one().detail == '{"kept":true}'
        assert db.query(AuditReadWindow).count() == 1


@pytest.mark.parametrize(
    "field",
    ["action", "target_id", "operator_id", "operator_role", "ip_address", "window_start", "first_at", "last_at"],
)
def test_corrupt_summary_identity_or_times_are_not_silently_rewritten(durable, field):
    factory, _ = durable
    with factory() as db:
        record_read(db, **_inputs())
        db.commit()
        log = db.query(AuditLog).one()
        if field in {"window_start", "first_at", "last_at"}:
            value = json.loads(log.detail)
            value[field] = "2000-01-01T00:00:00Z"
            log.detail = json.dumps(value)
        else:
            setattr(log, field, 999 if field in {"target_id", "operator_id"} else "changed")
        db.commit()
        before = log.detail
        with pytest.raises(RuntimeError, match="inconsistent"):
            record_read(db, **_inputs())
        db.rollback()
        assert db.query(AuditReadWindow).one().request_count == 1
        assert db.query(AuditLog).one().detail == before
