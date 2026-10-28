"""Round 7 review probes for scheduler/fatal/health generation-exactness (B18 + B4/B6),
adopted as repository regression tests. Each assertion states a required invariant.

The four companion probes for the single-owner lifecycle transition token and offline-edit
reservation (stop-overtakes-start and the offline state/language edits) are tracked as the next
mainline (B19 + B17 offline) and are intentionally not duplicated here.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager, InstanceRetiring
from lockbot.backend.app.bots.models import Bot

from backend.test_generation_edit_protocol import create_bot
from backend.test_resource_api_resources import api  # noqa: F401


def start(manager):
    instance = MagicMock()
    instance.bot._check_and_notify.return_value = None
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=instance):
        manager.start_bot(1, {"BOT_TYPE": "NODE"})
    return instance


def worker_call(function, name):
    errors = []

    def run():
        try:
            function()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, errors


def finish(thread, errors):
    thread.join(5)
    assert not thread.is_alive()
    assert not errors, [type(error).__name__ for error in errors]


def test_stale_due_entry_cannot_execute_replacement(monkeypatch):
    manager = BotManager()
    old = start(manager)
    paused, resume = threading.Event(), threading.Event()
    run_check = manager._scheduler._run_check

    def pause_before_admission(bot_id, instance):
        paused.set()
        assert resume.wait(5)
        return run_check(bot_id, instance)

    monkeypatch.setattr(manager._scheduler, "_run_check", pause_before_admission)
    thread, errors = worker_call(manager._scheduler._fire_due, "old-due")
    assert paused.wait(5)
    manager.stop_bot(1)
    replacement = start(manager)
    manager._scheduler._failure_counts[1] = 4
    resume.set()
    finish(thread, errors)
    old.bot._check_and_notify.assert_not_called()
    assert replacement.bot._check_and_notify.call_count == 0, "old due entry swept the replacement"


def test_stale_success_cannot_reset_replacement_failure_count(monkeypatch):
    manager = BotManager()
    start(manager)
    paused, resume = threading.Event(), threading.Event()
    run_check = manager._scheduler._run_check

    def pause_after_lease_release(bot_id, instance):
        result = run_check(bot_id, instance)
        paused.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(manager._scheduler, "_run_check", pause_after_lease_release)
    thread, errors = worker_call(manager._scheduler._fire_due, "old-success")
    assert paused.wait(5)
    manager.stop_bot(1)
    start(manager)
    manager._scheduler._failure_counts[1] = 4
    resume.set()
    finish(thread, errors)
    assert manager._scheduler._failure_counts[1] == 4, "old success reset the replacement's failure count"


def test_fatal_conditional_remove_is_atomic(monkeypatch):
    manager = BotManager()
    old = start(manager)
    old.bot._check_and_notify.side_effect = RuntimeError("fifth failure")
    manager._scheduler._failure_counts[1] = 4
    paused, resume = threading.Event(), threading.Event()
    remove = manager._scheduler.remove

    def pause_before_remove(bot_id):
        if threading.current_thread().name == "old-fatal":
            paused.set()
            assert resume.wait(5)
        return remove(bot_id)

    monkeypatch.setattr(manager._scheduler, "remove", pause_before_remove)
    thread, errors = worker_call(manager._scheduler._fire_due, "old-fatal")
    assert paused.wait(5)
    manager.stop_bot(1)
    replacement = start(manager)
    resume.set()
    finish(thread, errors)
    assert manager.get_instance(1) is replacement
    assert manager._scheduler._instances.get(1) is replacement, "old fatal completion unscheduled the replacement"


def test_delayed_stop_cannot_unschedule_replacement(monkeypatch):
    manager = BotManager()
    start(manager)
    paused, resume = threading.Event(), threading.Event()
    remove = manager._scheduler.remove

    def pause_before_remove(bot_id):
        if threading.current_thread().name == "old-stop":
            paused.set()
            assert resume.wait(5)
        return remove(bot_id)

    monkeypatch.setattr(manager._scheduler, "remove", pause_before_remove)
    thread, errors = worker_call(lambda: manager.stop_bot(1), "old-stop")
    assert paused.wait(5)
    # The in-progress stop owns the lifecycle transition for its whole duration, so a competing
    # start is refused up front and cannot publish a replacement that the delayed remove could
    # then unschedule. (Single-owner protocol: no concurrent transition can interleave.)
    with pytest.raises(InstanceRetiring):
        start(manager)
    resume.set()
    finish(thread, errors)
    assert not manager.is_running(1)
    # Once the owner is released, a start publishes and schedules cleanly.
    replacement = start(manager)
    assert manager.get_instance(1) is replacement
    assert manager._scheduler._instances.get(1) is replacement, "replacement not scheduled after the owner released"


def test_old_fatal_db_write_cannot_mark_replacement_error(
    client, admin_header, tmp_path, db_session, session_factory, monkeypatch
):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
        manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    row = db_session.get(Bot, bot_id)
    row.status = "running"
    db_session.commit()
    paused, resume = threading.Event(), threading.Event()

    def session_after_pause():
        paused.set()
        assert resume.wait(5)
        return session_factory()

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_after_pause)
    callback = manager._make_fatal_error_handler(bot_id)
    thread, errors = worker_call(lambda: callback(bot_id), "old-fatal-db")
    assert paused.wait(5)
    manager.stop_bot(bot_id)
    replacement = MagicMock()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=replacement):
        manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    row.status = "running"
    db_session.commit()
    resume.set()
    finish(thread, errors)
    db_session.expire_all()
    assert manager.get_instance(bot_id) is replacement
    assert db_session.get(Bot, bot_id).status == "running", "old callback committed error for the replacement"


def test_live_health_is_owned_by_generation(client, api, db_session):  # noqa: F811
    bot_id = api["bot_id"]
    row = db_session.get(Bot, bot_id)
    row.status = "error"
    db_session.commit()
    assert api["manager"].is_running(bot_id)
    response = client.get(f"/api/bots/{bot_id}/resources", headers=api["headers"])
    assert response.status_code == 200
    assert response.json()["health"] == "running", "live generation health came from stale DB bookkeeping"
