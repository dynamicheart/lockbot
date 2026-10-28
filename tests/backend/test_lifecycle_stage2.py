"""Stage-2 lifecycle failure-tail regressions.

Permanent versions of the independent R4 lifecycle probes (B29 recovery-vs-newer-failed,
B30 restart-constructor-failure, B31 recovery-batch-isolation), plus B17 commit-during-drain
and the admin-write-under-transition-owner serialization. The original probe file under
``.humanize`` is intentionally not modified.
"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name

import contextlib
import json
import os
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from lockbot.backend.app.bots import router
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot

from backend.test_lifecycle_recovery_canonical import actor, independent_db, request  # noqa: F401
from backend.test_scheduler_generation_exactness import finish, worker_call


def test_restart_constructor_failure_resets_running_row(independent_db, monkeypatch):
    """B30: the stop half drained and removed the instance; a start-half construction failure
    must reset the row to stopped/pid None rather than leaving a phantom ``running`` row."""
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    constructor = MagicMock()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", constructor)
    manager.start_bot(1, {"BOT_TYPE": "NODE"})
    with independent_db() as db:
        row = db.get(Bot, 1)
        row.status = "running"
        row.pid = os.getpid()
        db.commit()
    constructor.side_effect = RuntimeError("replacement construction failed")
    with independent_db() as db, pytest.raises(HTTPException):
        router.restart_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
    with independent_db() as db:
        row = db.get(Bot, 1)
        assert row.status != "running" and row.pid is None, (row.status, row.pid, manager.is_running(1))


def test_recovery_compensation_does_not_overwrite_newer_failed_generation(independent_db, monkeypatch):
    """B29: a newer start that published (and then failed) while our recovery construct was
    failing must keep its ``error`` status; the stale compensation must not reset it to stopped."""
    import lockbot.backend.app.bots.manager as manager_module
    import lockbot.backend.app.database as database
    import lockbot.backend.app.main as main

    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "running"
        db.commit()
    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(
        manager_module, "BotInstance", MagicMock(side_effect=[RuntimeError("recover failed"), MagicMock()])
    )
    original = manager.transition
    first = [True]
    observed = []

    @contextlib.contextmanager
    def newer_start_after_recovery_release(bot_id):
        outer = first[0]
        first[0] = False
        try:
            with original(bot_id):
                yield
        except RuntimeError:
            if outer:

                def start_then_fail():
                    with sessions() as db:
                        router.start_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
                    manager._make_fatal_error_handler(1)(1)
                    with sessions() as db:
                        observed.append(db.get(Bot, 1).status)

                thread, errors = worker_call(start_then_fail, "new-start-then-fatal")
                finish(thread, errors)
            raise

    monkeypatch.setattr(manager, "transition", newer_start_after_recovery_release)
    main._reset_running_bots()
    with sessions() as db:
        row = db.get(Bot, 1)
        assert observed == ["error"] and row.status == "error" and manager.is_failed(1), (
            observed,
            row.status,
            manager.is_failed(1),
        )


def test_recovery_compensation_failure_does_not_abort_other_candidates(independent_db, monkeypatch):
    """B31: an unexpected failure compensating one candidate (here a failing compensation commit)
    must not abort recovery of the remaining candidates."""
    import lockbot.backend.app.bots.manager as manager_module
    import lockbot.backend.app.main as main

    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "running"
        db.add(
            Bot(
                id=2,
                uuid="review-2",
                user_id=1,
                name="review-2",
                bot_type="NODE",
                webhook_url="",
                cluster_configs='["n1"]',
                status="running",
            )
        )
        db.commit()
    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(
        manager_module, "BotInstance", MagicMock(side_effect=[RuntimeError("construct failed"), MagicMock()])
    )

    def session_with_failed_compensation():
        db = sessions()
        commit = db.commit
        first = [True]

        def fail_compensation_once():
            if first[0]:
                first[0] = False
                raise RuntimeError("compensation commit failed")
            return commit()

        db.commit = fail_compensation_once
        return db

    monkeypatch.setattr(main, "SessionLocal", session_with_failed_compensation)
    main._reset_running_bots()
    assert manager.is_running(2)


def test_restart_absorbs_config_edit_committed_during_drain(independent_db, monkeypatch):
    """B17: a config edit that commits while the restart is draining the old generation must be
    reflected in the replacement, because the start half re-reads the fresh row after the drain."""
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    constructed = []

    def fake_instance(bot_type, config_dict, scheduler=None):
        constructed.append(config_dict)
        return MagicMock()

    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock(side_effect=fake_instance))
    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "running"
        db.commit()
        cfg = router._build_config_dict(db.get(Bot, 1), db)
    manager.start_bot(1, cfg)  # live generation to restart

    original_stop = manager.stop_bot

    def stop_then_edit(bot_id, drain_timeout=None):
        # Stands in for a language/config edit whose commit lands during the restart's drain
        # window, before the start half re-reads the authoritative row.
        result = original_stop(bot_id, drain_timeout=drain_timeout)
        with sessions() as db:
            row = db.get(Bot, bot_id)
            row.config_overrides = json.dumps({"LANGUAGE": "en"})
            db.commit()
        return result

    monkeypatch.setattr(manager, "stop_bot", stop_then_edit)
    with sessions() as db:
        router.restart_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
    assert constructed[-1].get("LANGUAGE") == "en", constructed[-1]


def test_api_key_write_refused_while_transition_held(independent_db, monkeypatch):
    """D5: an admin credential write is serialized under the transition owner, so a concurrent
    lifecycle transition makes it a transient 503 with no durable effect."""
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    sessions = independent_db
    held, release = threading.Event(), threading.Event()

    def hold_owner():
        with manager.transition(1):
            held.set()
            release.wait(5)

    thread, errors = worker_call(hold_owner, "hold-transition")
    try:
        assert held.wait(5)
        with sessions() as db, pytest.raises(HTTPException) as exc_info:
            router.generate_api_key(bot_id=1, request=request(), user=actor(), db=db)
        assert exc_info.value.status_code == 503
        with sessions() as db:
            assert db.get(Bot, 1).api_key is None
    finally:
        release.set()
        finish(thread, errors)
