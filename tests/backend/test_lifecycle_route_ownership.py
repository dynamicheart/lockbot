"""Round-1 probes: the Round-0 review's five lifecycle-ownership schedules, adopted as repository
regression tests.

Four are the reviewer's route-level schedules, which exercise the *full* start/stop/delete/restart
transactions (fresh validation under ownership, commit tail under ownership, refusal without
durable effect, restart draining before it reads config). The fifth (fatal completion racing
retirement, B2) is adapted to the corrected atomic-retirement protocol: the reviewer's version
paused the old separate ``_drain`` method, which no longer exists now that the wait and the
instance removal are one atomic ``_lock`` hold. The adapted version asserts the corrected outcome
directly — a fatal callback that arrives after retirement finds no owned instance and does nothing,
so a cleanly-stopped row is never overwritten with ``error``. The "fatal registers first → stop is
fenced" direction is covered by ``test_transition_owner.test_fatal_commit_cannot_outlive_generation``.
"""

import json
import threading
from unittest.mock import MagicMock, patch

from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.test_generation_edit_protocol import create_bot
from backend.test_scheduler_generation_exactness import finish, worker_call


def test_fatal_after_retirement_is_noop(tmp_path):
    # Independent file-backed sessions, not the suite's single-connection StaticPool.
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(
            Bot(
                id=1,
                user_id=1,
                name="probe",
                bot_type="NODE",
                webhook_url="unused",
                cluster_configs='["n1"]',
                status="running",
                pid=111,
            )
        )
        db.commit()
    manager = BotManager()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
        manager.start_bot(1, {"BOT_TYPE": "NODE"})
    callback = manager._make_fatal_error_handler(1)  # bound to generation 1

    # Fully retire generation 1 (atomic wait-and-retire). A clean stop commits 'stopped'.
    manager.stop_bot(1)
    with sessions() as db:
        row = db.get(Bot, 1)
        row.status, row.pid = "stopped", None
        db.commit()

    # A delayed fatal callback for the retired generation now runs. Because retirement already
    # removed the instance atomically, the callback finds nothing it owns and must do nothing:
    # no obligation, no _failed retention, and crucially no 'error' commit over the stopped row.
    with patch("lockbot.backend.app.database.SessionLocal", sessionmaker(bind=engine, autoflush=False)):
        callback(1)

    assert 1 not in manager._failed
    assert manager._fatal_pending.get(1, 0) == 0
    with sessions() as db:
        assert db.get(Bot, 1).status == "stopped", "a delayed fatal callback overwrote a cleanly-stopped row"
    engine.dispose()


def test_review_start_route_commit_after_delete(client, admin_header, tmp_path, db_session, monkeypatch):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    started, finish_start = threading.Event(), threading.Event()
    start = manager.start_bot

    def publish_then_pause(*args, **kwargs):
        result = start(*args, **kwargs)
        started.set()
        assert finish_start.wait(5)
        return result

    monkeypatch.setattr(manager, "start_bot", publish_then_pause)
    results = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: results.append(client.post(f"/api/bots/{bot_id}/start", headers=admin_header)),
            "start-before-route-commit",
        )
        assert started.wait(5)
        try:
            deleted = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        finally:
            finish_start.set()
            finish(thread, errors)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert not (deleted.status_code == 204 and results[0].status_code == 200 and row.status == "running"), (
        deleted.status_code,
        results[0].status_code,
        row.status,
        row.is_deleted,
        manager.is_running(bot_id),
    )


def test_review_restart_revalidates_after_delete(client, admin_header, tmp_path, db_session, monkeypatch):
    from lockbot.backend.app.bots import router

    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    fetched, resume = threading.Event(), threading.Event()
    get_bot = router._get_user_bot
    first = [True]

    def pause_after_fetch(*args, **kwargs):
        bot = get_bot(*args, **kwargs)
        if first[0]:
            first[0] = False
            fetched.set()
            assert resume.wait(5)
        return bot

    monkeypatch.setattr(router, "_get_user_bot", pause_after_fetch)
    results = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: results.append(client.post(f"/api/bots/{bot_id}/restart", headers=admin_header)),
            "restart-stale-read",
        )
        assert fetched.wait(5)
        try:
            deleted = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        finally:
            resume.set()
            finish(thread, errors)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert not (deleted.status_code == 204 and manager.is_running(bot_id)), (
        deleted.status_code,
        results[0].status_code,
        row.is_deleted,
        row.status,
    )


def test_review_start_route_refusal_has_no_durable_effect(client, admin_header, tmp_path, db_session, monkeypatch):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    held, release = threading.Event(), threading.Event()

    def owner():
        with manager.transition(bot_id):
            held.set()
            assert release.wait(5)

    thread, errors = worker_call(owner, "competing-owner")
    assert held.wait(5)
    try:
        with patch("lockbot.backend.app.bots.router.bot_manager", manager):
            responses = [client.post(f"/api/bots/{bot_id}/start", headers=admin_header) for _ in range(3)]
    finally:
        release.set()
        finish(thread, errors)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert all(r.status_code == 503 for r in responses) and row.status == "stopped" and row.consecutive_failures == 0, (
        [r.status_code for r in responses],
        row.status,
        row.consecutive_failures,
    )


def test_review_restart_cannot_load_pre_drain_language(client, admin_header, tmp_path, db_session, monkeypatch):
    from lockbot.backend.app.bots import router

    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    row = db_session.get(Bot, bot_id)
    manager.start_bot(bot_id, router._build_config_dict(row, db_session))
    row.status = "running"
    db_session.commit()
    read, resume = threading.Event(), threading.Event()
    build = router._build_config_dict

    def config_then_pause(*args, **kwargs):
        config = build(*args, **kwargs)
        read.set()
        assert resume.wait(5)
        return config

    monkeypatch.setattr(router, "_build_config_dict", config_then_pause)
    results = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: results.append(client.post(f"/api/bots/{bot_id}/restart", headers=admin_header)),
            "restart-config-before-drain",
        )
        assert read.wait(5)
        try:
            edited = client.put(f"/api/bots/{bot_id}/language", headers=admin_header, json={"language": "en"})
        finally:
            resume.set()
            finish(thread, errors)
    db_session.expire_all()
    persisted = json.loads(db_session.get(Bot, bot_id).config_overrides)["LANGUAGE"]
    live = manager.get_instance(bot_id).bot.config.get_val("LANGUAGE")
    assert live == persisted, (edited.status_code, results[0].status_code, live, persisted)
