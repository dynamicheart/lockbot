"""Upgrade old state filenames without resetting holders or overwriting conflicts."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from lockbot.backend.app import config
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.router import _build_config_dict
from lockbot.backend.app.managed_paths import ManagedPathError
from lockbot.backend.app.state_migration import (
    StateMigrationRequired,
    assert_state_store_ready,
    inventory,
    migrate_legacy_state_files,
)
from lockbot.core.bot_instance import BotInstance


def add_bot(db, bot_id=1, kind="NODE", overrides=None):
    if db.get(User, 1) is None:
        db.add(User(id=1, username="legacy-upgrade", email="legacy@test.invalid", password_hash="unused", role="admin"))
    bot = Bot(
        id=bot_id,
        user_id=1,
        uuid=f"legacy-{bot_id}",
        name=f"legacy-{bot_id}",
        bot_type=kind,
        webhook_url="",
        cluster_configs=json.dumps({"n1": ["GPU"]} if kind == "DEVICE" else ["n1"]),
        config_overrides=json.dumps(overrides or {}),
        status="running",
    )
    db.add(bot)
    db.commit()
    return bot


def write_legacy(bot_id=1, payload=None):
    source = config.MANAGED_PATHS.bot_file(bot_id, "cluster_status.json")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({"cluster_status": {}} if payload is None else payload))
    return source, config.MANAGED_PATHS.state_file(bot_id)


@pytest.mark.parametrize("kind", ["NODE", "DEVICE", "QUEUE"])
@pytest.mark.parametrize("wrapper", ["bot_state", "cluster_state", "cluster_status"])
def test_upgrade_loads_holders_and_queue_through_existing_core(db_session, kind, wrapper):
    bot = add_bot(db_session, kind=kind)
    holder = {"user_id": "alice", "start_time": int(time.time()), "duration": 3600}
    node = {"status": "exclusive", "current_users": [holder]}
    if kind == "QUEUE":
        node["booking_list"] = [{"user_id": "bob", "duration": 1800, "start_time": 0, "notified": False}]
    state = {"n1": [{"dev_id": 0, "dev_model": "GPU", **node}] if kind == "DEVICE" else node}
    source, target = write_legacy(payload={wrapper: state})
    original = source.read_bytes()
    source.chmod(0o640)
    # Inventory remains read-only, even for the newly supported same-directory case.
    assert inventory(db_session, config.MANAGED_PATHS)["requires_migration"]
    assert source.exists() and not target.exists()
    assert migrate_legacy_state_files(db_session, config.MANAGED_PATHS) == [1]
    assert target.read_bytes() == original and target.stat().st_mode & 0o777 == 0o640
    assert target.stat().st_nlink == 1 and not source.exists()
    assert_state_store_ready(db_session, config.MANAGED_PATHS)
    instance = BotInstance(kind, _build_config_dict(bot, db_session), auto_start=False)
    _, loaded = instance.bot.state_snapshot()
    actual = loaded["n1"][0] if kind == "DEVICE" else loaded["n1"]
    assert actual["current_users"] == [holder]
    if kind == "QUEUE":
        assert [u["user_id"] for u in actual["booking_list"]] == ["bob"]
    assert migrate_legacy_state_files(db_session, config.MANAGED_PATHS) == []


@pytest.mark.parametrize("same_contents", [False, True])
def test_conflict_preserves_both_files_and_does_not_partially_migrate_other_bot(db_session, same_contents):
    add_bot(db_session, 1)
    add_bot(db_session, 2)
    first, first_target = write_legacy(1)
    conflict, target = write_legacy(2)
    target.write_bytes(conflict.read_bytes() if same_contents else b'{"bot_state":{"n1":{}}}')
    before = {p: p.read_bytes() for p in [first, conflict, target]}
    with pytest.raises(StateMigrationRequired, match="bots 2"):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not first_target.exists()


@pytest.mark.parametrize("content", ["{", "[]", '{"unknown":{}}', '{"bot_state":["bad"],"cluster_status":{}}'])
def test_unreadable_or_unrecognized_state_is_not_migrated(db_session, content):
    add_bot(db_session)
    source, target = write_legacy()
    source.write_text(content)
    with pytest.raises(StateMigrationRequired):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert source.read_text() == content and not target.exists()


def test_other_legacy_directory_still_requires_operator_choice(db_session, tmp_path):
    old_root = tmp_path / "old"
    add_bot(db_session, overrides={"DATA_DIR": str(old_root)})
    old_file = old_root / "1/cluster_status.json"
    old_file.parent.mkdir(parents=True)
    old_file.write_text('{"cluster_status":{}}')
    with pytest.raises(StateMigrationRequired):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert old_file.exists() and not config.MANAGED_PATHS.state_file(1).exists()


@pytest.mark.parametrize("link", ["source", "target", "hardlink"])
def test_linked_state_is_not_followed_or_migrated(db_session, tmp_path, link):
    add_bot(db_session)
    source, target = write_legacy()
    external = tmp_path / "outside.json"
    external.write_bytes(source.read_bytes())
    if link == "target":
        target.symlink_to(external)
    else:
        source.unlink()
        if link == "source":
            source.symlink_to(external)
        else:
            source.hardlink_to(external)
    original = external.read_bytes()
    with pytest.raises((StateMigrationRequired, ManagedPathError)):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert external.read_bytes() == original and source.exists()


def test_destination_appearing_during_migration_is_never_overwritten(db_session, monkeypatch):
    add_bot(db_session)
    source, target = write_legacy()
    original_link = Path.hardlink_to

    def destination_appeared(path, origin):
        path.write_text("newer state")
        return original_link(path, origin)

    monkeypatch.setattr(Path, "hardlink_to", destination_appeared)
    with pytest.raises(StateMigrationRequired, match="source preserved"):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert target.read_text() == "newer state" and source.exists()


def test_unlink_failure_leaves_original_and_removes_new_link(db_session, monkeypatch):
    add_bot(db_session)
    source, target = write_legacy()
    original_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == source:
            raise PermissionError("cannot remove original")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    with pytest.raises(StateMigrationRequired, match="source preserved"):
        migrate_legacy_state_files(db_session, config.MANAGED_PATHS)
    assert source.exists() and not target.exists() and source.stat().st_nlink == 1


def test_startup_migrates_before_scheduler_and_bot_recovery(db_session, session_factory, monkeypatch):
    import lockbot.backend.app.main as main
    from lockbot.backend.app.backup.scheduler import backup_scheduler
    from lockbot.backend.app.bots.manager import bot_manager

    add_bot(db_session)
    source, target = write_legacy()
    events = []
    monkeypatch.setattr(main, "engine", db_session.get_bind())
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "_acquire_state_store_ownership", lambda: events.append("ownership"))

    def scheduler():
        assert events == ["ownership"]
        assert target.exists() and not source.exists()
        events.append("scheduler")

    monkeypatch.setattr(bot_manager, "start_scheduler", scheduler)
    monkeypatch.setattr(main, "_reset_running_bots", lambda: events.append("recover"))
    monkeypatch.setattr(backup_scheduler, "start", lambda: None)
    monkeypatch.setattr(backup_scheduler, "stop", lambda: None)
    monkeypatch.setattr(bot_manager, "shutdown_all", lambda: None)

    async def start():
        async with main.lifespan(FastAPI()):
            assert events == ["ownership", "scheduler", "recover"]

    asyncio.run(start())
