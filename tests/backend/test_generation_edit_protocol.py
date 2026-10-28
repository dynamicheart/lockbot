"""Round 6 review probes, adopted as repository regression tests for the generation and
live-edit admission protocol (B6/B17/B18/B19/B20). Each assertion states a required invariant.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import AdmissionFull, BotManager, InstanceRetiring
from lockbot.backend.app.bots.models import Bot


def start(manager, instance=None, config=None):
    instance = instance or MagicMock()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=instance):
        manager.start_bot(1, config or {"BOT_TYPE": "NODE"})
    return instance


def test_old_fatal_callback_cannot_fail_replacement(monkeypatch):
    manager = BotManager()
    old = start(manager)
    old.bot._check_and_notify.side_effect = RuntimeError("fifth failure")
    manager._scheduler._failure_counts[1] = 4
    paused, resume = threading.Event(), threading.Event()
    original_remove = manager._scheduler.remove

    def remove_then_pause(bot_id):
        original_remove(bot_id)
        if threading.current_thread().name == "fatal-sweeper":
            paused.set()
            assert resume.wait(5)

    monkeypatch.setattr(manager._scheduler, "remove", remove_then_pause)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", MagicMock())
    worker = threading.Thread(target=manager._scheduler._fire_due, name="fatal-sweeper")
    worker.start()
    assert paused.wait(5)
    manager.stop_bot(1)
    replacement = start(manager)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    assert manager.get_instance(1) is replacement, "old fatal callback moved replacement into _failed"


def test_removed_managed_sweep_cannot_fall_back_to_standalone(monkeypatch):
    manager = BotManager()
    old = start(manager)
    paused, resume = threading.Event(), threading.Event()
    run_check = manager._scheduler._run_check

    def pause_before_check(bot_id, instance):
        paused.set()
        assert resume.wait(5)
        return run_check(bot_id, instance)

    monkeypatch.setattr(manager._scheduler, "_run_check", pause_before_check)
    worker = threading.Thread(target=manager._scheduler._fire_due)
    worker.start()
    assert paused.wait(5)
    manager.stop_bot(1)  # no lease acquired yet, so this returns immediately
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    old.bot._check_and_notify.assert_not_called()  # removal erased _admissions, enabling raw fallback


def test_failed_read_admission_remains_bounded(monkeypatch):
    manager = BotManager(max_inflight=1)
    instance = start(manager)
    manager._bots.pop(1)
    manager._failed[1] = instance
    with manager.admit(1, for_read=True), pytest.raises(AdmissionFull), manager.admit(1, for_read=True):
        pass


def create_bot(client, admin_header, tmp_path):
    response = client.post(
        "/api/bots",
        headers=admin_header,
        json={
            "name": "review-probe",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["n1"],
            "config_overrides": {"LANGUAGE": "zh"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_refused_language_edit_does_not_commit(client, admin_header, tmp_path, db_session):
    bot_id = create_bot(client, admin_header, tmp_path)
    with patch("lockbot.backend.app.bots.router.bot_manager") as manager:
        manager.admit.side_effect = InstanceRetiring("retiring")
        response = client.put(f"/api/bots/{bot_id}/language", headers=admin_header, json={"language": "en"})
    assert response.status_code == 503
    db_session.expire_all()
    assert json.loads(db_session.get(Bot, bot_id).config_overrides)["LANGUAGE"] == "zh", "503 committed LANGUAGE=en"


def test_failed_state_edit_cannot_bypass_retained_lease(client, admin_header, tmp_path, db_session):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    instance = MagicMock()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=instance):
        manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    manager._bots.pop(bot_id)
    manager._failed[bot_id] = instance
    row = db_session.get(Bot, bot_id)
    row.status = "error"
    db_session.commit()
    state = {"n1": {"status": "idle", "current_users": [], "booking_list": []}}
    with manager.admit(bot_id, for_read=True):
        with patch("lockbot.backend.app.bots.router.bot_manager", manager):
            response = client.put(f"/api/bots/{bot_id}/state", headers=admin_header, json=state)
        assert response.status_code != 200, "failed generation accepted unleased disk edit during a retained lease"


def test_delayed_stop_cannot_remove_replacement(monkeypatch):
    manager = BotManager()
    start(manager)
    paused, resume = threading.Event(), threading.Event()
    remove = manager._scheduler.remove

    def remove_then_pause(bot_id):
        remove(bot_id)
        if threading.current_thread().name == "delayed-stop":
            # Paused mid-stop, while the stop still owns the lifecycle transition (before its
            # atomic wait-and-retire finalizes). _drain no longer exists as a separate boundary;
            # the scheduler.remove inside the owner is an equivalent mid-stop pause point.
            paused.set()
            assert resume.wait(5)

    monkeypatch.setattr(manager._scheduler, "remove", remove_then_pause)
    worker = threading.Thread(target=manager.stop_bot, args=(1,), name="delayed-stop")
    worker.start()
    assert paused.wait(5)
    # The delayed stop owns the lifecycle transition until it finalizes, so a competing start is
    # refused up front and cannot publish a later generation that the stop could then remove.
    with pytest.raises(InstanceRetiring):
        start(manager)
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    assert not manager.is_running(1)
    # Once the owner is released, a start publishes cleanly as the live instance.
    replacement = start(manager)
    assert manager.get_instance(1) is replacement, "a start after the owner released did not publish"


def test_refused_start_cannot_persist_clamps(tmp_path):
    import time

    from lockbot.core.config import Config
    from lockbot.core.io import _bot_dir

    manager = BotManager()
    config = {
        "BOT_TYPE": "NODE",
        "BOT_ID": 1,
        "DATA_DIR": str(tmp_path / "bots"),
        "CLUSTER_CONFIGS": ["n1"],
        "MAX_LOCK_DURATION": 60,
    }
    state_file = __import__("pathlib").Path(_bot_dir(Config(config))) / "bot_state.json"
    original = {
        "bot_state": {
            "n1": {
                "status": "exclusive",
                "booking_list": [],
                "current_users": [
                    {"user_id": "alice", "duration": 3600, "start_time": int(time.time()), "is_notified": False}
                ],
            }
        }
    }
    state_file.write_text(json.dumps(original))
    manager._failed[1] = MagicMock()
    with patch("lockbot.core.base_bot.BaseLockBot._notify_clamped_users"), pytest.raises(InstanceRetiring):
        manager.start_bot(1, config)
    persisted = json.loads(state_file.read_text())
    assert persisted == original, "start was refused after its constructor persisted a clamped lock"


# Import the repository's real-resource-route fixture as a fixture in this module. tests/backend
# is the ``backend`` package (it has __init__.py), so use the package-qualified import so this
# runs under the normal ``pytest tests/`` collection, not only with PYTHONPATH=tests/backend.
from backend.test_resource_api_resources import api  # noqa: E402, F401


def test_failed_health_does_not_depend_on_db_status_write(client, api, db_session):  # noqa: F811
    from lockbot.backend.app.resource_api.resources import snapshot_cache

    snapshot_cache.clear()
    bot_id, manager = api["bot_id"], api["manager"]
    row = db_session.get(Bot, bot_id)
    row.status = "running"
    db_session.commit()
    manager._scheduler.remove(bot_id)
    with patch("lockbot.backend.app.database.SessionLocal", side_effect=RuntimeError("DB temporarily unavailable")):
        manager._make_fatal_error_handler(bot_id)(bot_id)
    response = client.get(f"/api/bots/{bot_id}/resources", headers=api["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["health"] == "error", "retained failed generation still reports health=running"


def test_stopped_state_file_write_excludes_concurrent_start(client, admin_header, tmp_path, monkeypatch):
    import os
    from pathlib import Path

    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
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
    paused, resume = threading.Event(), threading.Event()
    original_replace = os.replace

    def pause_replace(source, destination):
        if Path(destination) == state_file:
            paused.set()
            assert resume.wait(5)
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", pause_replace)
    responses = []
    real_config = {
        "BOT_TYPE": "NODE",
        "BOT_ID": bot_id,
        "DATA_DIR": str(tmp_path / "bots"),
        "CLUSTER_CONFIGS": ["n1"],
        "MAX_LOCK_DURATION": -1,
    }
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        writer = threading.Thread(
            target=lambda: responses.append(client.put(f"/api/bots/{bot_id}/state", headers=admin_header, json=new))
        )
        writer.start()
        assert paused.wait(5)
        # The stopped edit owns the lifecycle transition while it writes the file, so a
        # concurrent start is refused up front (transient refusal, no durable effect). The edit
        # and the start therefore cannot diverge: exactly one proceeds. Both use the
        # deployment root's canonical bots/{id} directory.
        with pytest.raises(InstanceRetiring):
            manager.start_bot(bot_id, real_config)
        resume.set()
        writer.join(5)
    assert not writer.is_alive()
    assert responses[0].status_code == 200, responses[0].text
    assert not manager.is_running(bot_id), "a start was published while the stopped edit owned the transition"
    # The edit's file is authoritative: a later start loads exactly what the edit wrote.
    manager.start_bot(bot_id, real_config)
    assert manager.get_instance(bot_id).bot.state.bot_state == json.loads(state_file.read_text())["bot_state"], (
        "a later start did not load the file the stopped edit wrote"
    )
