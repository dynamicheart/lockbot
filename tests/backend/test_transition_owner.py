"""Round-0 (new loop) probes for the single-owner lifecycle transition protocol (AC-7).

These verify the *class* invariant, not just individual interleavings:

    One transition owner per bot  ⇒  every lifecycle transition (start / stop / full restart /
    delete / offline state+language edit) is serialized per bot; a competing transition on
    another thread is refused up front (503 ``instance_retiring``) with no durable effect.

    Counted fatal-completion obligation  ⇒  a fatal DB write is kept outstanding (and drained
    by a stop) through its off-lock SQL, so no asynchronous completion of a retired generation
    can publish a replacement or commit for one.

The first three are the reviewer's own R9 ownership probes (file-backed DB / real routes),
adopted here so the exact interleavings they exercised are now pinned green. The remainder are
round-authored and go *beyond* the reviewer's examples (restart-gap atomicity, and the two
offline-edit races resolved as explicit transient refusals rather than divergence).

DB status is never used as an ownership predicate anywhere below: ownership is read from the
manager's transition owner, and the routes refuse / commit under it.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager, InstanceRetiring, MayHaveExecuted
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.test_generation_edit_protocol import create_bot
from backend.test_scheduler_generation_exactness import finish, worker_call

# ── B4: counted fatal-completion obligation (reviewer R9 probe, file-backed) ──────────────


def test_fatal_commit_cannot_outlive_generation(tmp_path, monkeypatch):
    # Independent connections to a real file, not the suite's single-connection StaticPool, so
    # the stale UPDATE (if any) is a genuine cross-session commit, not a fixture artifact.
    engine = create_engine(f"sqlite:///{tmp_path / 'fatal.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(
            Bot(
                id=1,
                user_id=1,
                name="fatal-probe",
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
    paused, resume = threading.Event(), threading.Event()

    def paused_session():
        db = sessions()
        commit = db.commit

        def pause_before_commit():
            # The still_ours check has passed; the fatal obligation is outstanding; no SQL
            # UPDATE has happened yet.
            paused.set()
            assert resume.wait(5)
            return commit()

        db.commit = pause_before_commit
        return db

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", paused_session)
    callback = manager._make_fatal_error_handler(1)
    thread, errors = worker_call(lambda: callback(1), "old-fatal-at-commit")
    assert paused.wait(5)
    replacement = None
    try:
        # A correct counted completion either times out fenced or refuses, but NEVER lets a
        # replacement publish while the fatal write for the prior generation is outstanding.
        try:
            stopped = manager.stop_bot(1, drain_timeout=0.05)
        except (MayHaveExecuted, InstanceRetiring):
            stopped = False
        if stopped is False:
            with pytest.raises(InstanceRetiring):
                manager.start_bot(1, {"BOT_TYPE": "NODE"})
        else:
            replacement = MagicMock()
            with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=replacement):
                manager.start_bot(1, {"BOT_TYPE": "NODE"})
            with sessions() as db:
                row = db.get(Bot, 1)
                row.status, row.pid = "running", 222
                db.commit()
    finally:
        resume.set()
        finish(thread, errors)

    if replacement is not None:
        assert manager.get_instance(1) is replacement
        with sessions() as db:
            assert (db.get(Bot, 1).status, db.get(Bot, 1).pid) == ("running", 222)
    else:
        # Once the fatal completion finishes, stopping and restarting become safe again.
        manager.stop_bot(1)
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            manager.start_bot(1, {"BOT_TYPE": "NODE"})
    engine.dispose()


# ── B19: delete must consult the manager, not the stopped DB row ──────────────────────────


def test_delete_cannot_skip_unpublished_start(client, admin_header, tmp_path, db_session, monkeypatch):
    bot_id = create_bot(client, admin_header, tmp_path)
    assert db_session.get(Bot, bot_id).status == "stopped"
    manager = BotManager()
    paused, resume = threading.Event(), threading.Event()

    def construct(*args, **kwargs):
        paused.set()
        assert resume.wait(5)
        return MagicMock()

    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", construct)
    responses = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: responses.append(client.post(f"/api/bots/{bot_id}/start", headers=admin_header)),
            "start-deleted-bot",
        )
        assert paused.wait(5)
        try:
            # The start owns the transition (stopped DB row notwithstanding); delete is refused.
            deletion = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        finally:
            resume.set()
            finish(thread, errors)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert not (deletion.status_code == 204 and manager.is_running(bot_id)), (
        deletion.status_code,
        responses[0].status_code,
        row.is_deleted,
        row.status,
        bot_id in manager._scheduler._instances,
    )
    # The start published; the bot is running and NOT deleted.
    assert manager.is_running(bot_id)
    assert row.is_deleted is False


# ── B19: stop route and restart route cannot interleave (route-commit under the owner) ────


def test_stop_route_refuses_concurrent_restart(client, admin_header, tmp_path, db_session, monkeypatch):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    row = db_session.get(Bot, bot_id)
    row.status = "running"
    db_session.commit()
    paused, resume = threading.Event(), threading.Event()
    stop = manager.stop_bot
    stop_calls = []

    def stop_then_pause(*args, **kwargs):
        result = stop(*args, **kwargs)
        stop_calls.append(result)
        if len(stop_calls) == 1:
            # The stop route still owns the transition here (its ``with transition`` is open and
            # its DB commit has not run yet).
            paused.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(manager, "stop_bot", stop_then_pause)
    responses = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: responses.append(client.post(f"/api/bots/{bot_id}/stop", headers=admin_header)),
            "old-stop-route",
        )
        assert paused.wait(5)
        try:
            # The stop route owns the transition across its own DB commit, so a concurrent
            # restart is refused up front (transient 503) rather than publishing a generation
            # the stop route could then overwrite with status='stopped'.
            refused = client.post(f"/api/bots/{bot_id}/restart", headers=admin_header)
            assert refused.status_code == 503, refused.text
            assert refused.json()["detail"]["code"] == "instance_retiring"
        finally:
            resume.set()
            finish(thread, errors)
    db_session.expire_all()
    assert responses[0].status_code == 200
    assert not manager.is_running(bot_id)
    assert db_session.get(Bot, bot_id).status == "stopped"
    # A clean retry after the stop fully committed restarts the bot to a consistent running row.
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        restarted = client.post(f"/api/bots/{bot_id}/restart", headers=admin_header)
    assert restarted.status_code == 200, restarted.text
    db_session.expire_all()
    assert manager.is_running(bot_id)
    row = db_session.get(Bot, bot_id)
    assert row.status == "running" and row.pid is not None


# ── Round-authored: restart is one atomic operation under one owner (restart-gap) ─────────


def test_restart_is_atomic_under_one_owner(monkeypatch):
    manager = BotManager()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
        manager.start_bot(1, {"BOT_TYPE": "NODE"})
    gen1 = manager.generation(1)
    paused, resume = threading.Event(), threading.Event()

    def construct_then_pause(*args, **kwargs):
        # Reached only by the restart's start half, AFTER its stop half retired generation 1.
        paused.set()
        assert resume.wait(5)
        return MagicMock()

    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", construct_then_pause)
    thread, errors = worker_call(lambda: manager.restart_bot(1, {"BOT_TYPE": "NODE"}), "restart")
    assert paused.wait(5)
    # The restart owns the transition across BOTH halves: in the gap between the stop and the
    # start, a competing stop AND a competing start are both refused up front, with no effect.
    with pytest.raises(InstanceRetiring):
        manager.stop_bot(1)
    with pytest.raises(InstanceRetiring):
        manager.start_bot(1, {"BOT_TYPE": "NODE"})
    resume.set()
    finish(thread, errors)
    assert manager.is_running(1)
    # Exactly one new generation came from the whole restart; the refused competitors added none.
    assert manager.generation(1) == gen1 + 1


# ── Round-authored: offline edits resolve a mid-construction start as a transient refusal ──


def _state_edit_setup(client, admin_header, tmp_path):
    bot_id = create_bot(client, admin_header, tmp_path)
    state_file = tmp_path / "bots" / str(bot_id) / "bot_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    old = {"n1": {"status": "idle", "current_users": [], "booking_list": []}}
    state_file.write_text(json.dumps({"bot_state": old}))
    new = {
        "n1": {
            "status": "exclusive",
            "current_users": [{"user_id": "alice", "start_time": 1792640000, "duration": 3600, "is_notified": False}],
            "booking_list": [],
        }
    }
    config = {
        "BOT_TYPE": "NODE",
        "BOT_ID": bot_id,
        "DATA_DIR": str(tmp_path / "bots"),
        "CLUSTER_CONFIGS": ["n1"],
        "MAX_LOCK_DURATION": -1,
        "LANGUAGE": "zh",
    }
    return bot_id, state_file, old, new, config


def test_offline_state_edit_refuses_mid_construction_start(client, admin_header, tmp_path, monkeypatch):
    from lockbot.core.bot_instance import BotInstance

    bot_id, state_file, old, new, config = _state_edit_setup(client, admin_header, tmp_path)
    manager = BotManager()
    paused, resume = threading.Event(), threading.Event()

    def construct_then_pause(*args, **kwargs):
        instance = BotInstance(*args, **kwargs)
        paused.set()
        assert resume.wait(5)
        return instance

    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", construct_then_pause)
    thread, errors = worker_call(lambda: manager.start_bot(bot_id, config), "unpublished-start")
    assert paused.wait(5)
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        response = client.put(f"/api/bots/{bot_id}/state", headers=admin_header, json=new)
    resume.set()
    finish(thread, errors)
    # The start owns the transition while it loads state; the edit is refused with NO durable
    # effect, so live state and the file cannot diverge.
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "instance_retiring"
    assert json.loads(state_file.read_text())["bot_state"] == old, "refused edit still changed the file"
    assert manager.get_instance(bot_id).bot.state.bot_state == old, "started bot diverged from its loaded file"


def test_offline_language_edit_refuses_mid_construction_start(client, admin_header, tmp_path, db_session, monkeypatch):
    from lockbot.core.bot_instance import BotInstance

    bot_id, _state_file, _old, _new, config = _state_edit_setup(client, admin_header, tmp_path)
    manager = BotManager()
    paused, resume = threading.Event(), threading.Event()

    def construct_then_pause(*args, **kwargs):
        instance = BotInstance(*args, **kwargs)
        paused.set()
        assert resume.wait(5)
        return instance

    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", construct_then_pause)
    thread, errors = worker_call(lambda: manager.start_bot(bot_id, config), "unpublished-language")
    assert paused.wait(5)
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        response = client.put(f"/api/bots/{bot_id}/language", headers=admin_header, json={"language": "en"})
    resume.set()
    finish(thread, errors)
    # Refused with no durable effect: neither the override nor the live config moved to 'en'.
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "instance_retiring"
    db_session.expire_all()
    assert json.loads(db_session.get(Bot, bot_id).config_overrides)["LANGUAGE"] == "zh"
    assert manager.get_instance(bot_id).bot.config.get_val("LANGUAGE") == "zh"
