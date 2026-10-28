"""Dry-run legacy-file inventory and refusal to silently lose old occupancy."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from lockbot.backend.app import config
from lockbot.backend.app.bots import router
from lockbot.backend.app.state_migration import (
    StateMigrationRequired,
    assert_bot_state_ready,
    inventory_bot,
)


def record(tmp_path, **extra):
    return SimpleNamespace(
        id=1,
        name="legacy",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url="",
        aes_key="",
        token="",
        cluster_configs='["n1"]',
        config_overrides=json.dumps({"DATA_DIR": str(tmp_path / "old"), **extra}),
    )


def test_inventory_conflict_is_read_only_and_blocks_empty_start(tmp_path):
    bot = record(tmp_path)
    legacy = tmp_path / "old/1/bot_state.json"
    legacy.parent.mkdir(parents=True)
    content = '{"bot_state":{"n1":{"status":"exclusive","current_users":[{"user_id":"old"}]}}}'
    legacy.write_text(content)
    report = inventory_bot(bot, config.MANAGED_PATHS)
    assert report["requires_migration"]
    source = next(row for row in report["legacy_candidates"] if row["path"] == str(legacy))
    assert source["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert not config.MANAGED_PATHS.state_file(1).exists()
    with pytest.raises(StateMigrationRequired, match="No files were moved"):
        router._build_config_dict(bot)
    assert legacy.read_text() == content


def test_identical_legacy_copy_must_be_archived_before_managed_writes(tmp_path):
    bot = record(tmp_path)
    legacy = tmp_path / "old/1/bot_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"bot_state":{}}')
    target = config.MANAGED_PATHS.state_file(1)
    target.parent.mkdir(parents=True)
    target.write_bytes(legacy.read_bytes())
    first = inventory_bot(bot, config.MANAGED_PATHS)
    assert first == inventory_bot(bot, config.MANAGED_PATHS)
    assert first["requires_migration"]
    # Even identical source files must leave the historical candidate filename: a
    # normal managed save would otherwise make the next restart look conflicted.
    legacy.rename(legacy.with_suffix(".archived"))
    assert_bot_state_ready(bot, config.MANAGED_PATHS)
    target.write_text('{"bot_state":{"n1":{"status":"idle"}}}')
    assert_bot_state_ready(bot, config.MANAGED_PATHS)


def test_inventory_conflicting_copies_require_operator_choice(tmp_path):
    bot = record(tmp_path)
    legacy = tmp_path / "old/1/bot_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old")
    target = config.MANAGED_PATHS.state_file(1)
    target.parent.mkdir(parents=True)
    target.write_text("different")
    with pytest.raises(StateMigrationRequired):
        assert_bot_state_ready(bot, config.MANAGED_PATHS)
    assert target.read_text() == "different"
    assert legacy.read_text() == "old"


def test_inventory_does_not_follow_external_symlinks(tmp_path):
    bot = record(tmp_path)
    legacy = tmp_path / "old/1/bot_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(tmp_path / "unreadable-source")
    report = inventory_bot(bot, config.MANAGED_PATHS)
    assert report["requires_migration"]
    source = next(row for row in report["legacy_candidates"] if row["path"] == str(legacy))
    assert "symlink" in source["error"]


def test_deployment_check_precedes_scheduler_and_recovery(monkeypatch):
    import lockbot.backend.app.main as main
    import lockbot.backend.app.state_migration as migration
    from fastapi import FastAPI

    events = []
    monkeypatch.setattr(
        migration,
        "assert_state_store_ready",
        lambda *args: (_ for _ in ()).throw(StateMigrationRequired("needs operator migration")),
    )
    for name in (
        "_acquire_state_store_ownership",
        "_migrate_bot_logs_category",
        "_migrate_bot_consecutive_failures",
        "_migrate_users_must_change_password",
        "_migrate_users_token_version",
        "_migrate_bot_soft_delete",
        "_migrate_audit_logs",
        "_migrate_bots_api_key",
        "_migrate_bots_uuid",
        "_migrate_bots_resource_auth_enabled",
        "_migrate_resource_bindings",
    ):
        monkeypatch.setattr(main, name, lambda: None)
    monkeypatch.setattr(main, "_reset_running_bots", lambda: events.append("recover"))
    import lockbot.backend.app.bots.manager as manager

    monkeypatch.setattr(manager.bot_manager, "start_scheduler", lambda: events.append("scheduler"))
    import asyncio

    async def enter():
        async with main.lifespan(FastAPI()):
            events.append("serving")

    with pytest.raises(StateMigrationRequired):
        asyncio.run(enter())
    assert events == []


def test_inventory_includes_deprecated_file_without_modifying_it(tmp_path):
    bot = record(tmp_path)
    legacy = tmp_path / "old/1/cluster_status.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"cluster_status":{"n1":{"status":"exclusive"}}}')
    report = inventory_bot(bot, config.MANAGED_PATHS)
    assert report["requires_migration"]
    assert any(row.get("exists") and row["path"] == str(legacy) for row in report["legacy_candidates"])
    assert legacy.exists()
    assert not config.MANAGED_PATHS.state_file(1).exists()


def test_cli_dry_run_is_read_only_and_does_not_create_missing_database(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    database = tmp_path / "inventory.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, username="inventory", email="i@test.invalid", password_hash="unused", role="admin"))
        db.add(
            Bot(
                id=1,
                uuid="inventory",
                user_id=1,
                name="inventory",
                bot_type="NODE",
                webhook_url="",
                cluster_configs='["n1"]',
                config_overrides=json.dumps({"DATA_DIR": str(tmp_path / "old")}),
            )
        )
        db.commit()
    engine.dispose()
    # No ORM/database writes or source-file migration should occur in the command.
    legacy = tmp_path / "old/1/bot_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"bot_state":{}}')
    original = database.read_bytes()
    env = dict(
        os.environ,
        DATA_DIR=str(tmp_path / "managed"),
        DATABASE_URL=f"sqlite:///{database}",
        PYTHONPATH=str(Path("python").resolve()),
    )
    result = subprocess.run(
        [sys.executable, "-m", "lockbot.backend.app.state_migration"],
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stdout)["requires_migration"]
    assert database.read_bytes() == original
    assert legacy.read_text() == '{"bot_state":{}}'
    assert not (tmp_path / "managed/bots/1/bot_state.json").exists()
    missing = tmp_path / "missing.db"
    env["DATABASE_URL"] = f"sqlite:///{missing}"
    result = subprocess.run(
        [sys.executable, "-m", "lockbot.backend.app.state_migration"],
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )
    assert result.returncode != 0 and not missing.exists()
    # Explicitly verify the source schema/rows remain accessible after inventory.
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM bots WHERE id=1").fetchone() == ("inventory",)


def test_literal_tilde_legacy_override_is_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEGACY_WORKING_DIR", tmp_path)
    bot = record(tmp_path)
    bot.config_overrides = '{"DATA_DIR":"~/legacy"}'
    actual_old_file = tmp_path / "~/legacy/1/bot_state.json"
    actual_old_file.parent.mkdir(parents=True)
    actual_old_file.write_text('{"bot_state":{"n1":{"status":"exclusive"}}}')
    report = inventory_bot(bot, config.MANAGED_PATHS)
    assert report["requires_migration"]
    assert any(row.get("exists") and row["path"] == str(actual_old_file) for row in report["legacy_candidates"])
    with pytest.raises(StateMigrationRequired):
        router._build_config_dict(bot)


def test_restore_missing_state_keeps_raw_override_migration_evidence(tmp_path, db_session, monkeypatch):
    from unittest.mock import patch

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.backup.service import restore_apply
    from lockbot.backend.app.bots.models import Bot

    from backend.test_backup_restore import _build_backup_zip

    db_session.add(User(id=1, username="legacy-import", email="l@test.invalid", password_hash="unused", role="admin"))
    db_session.commit()
    legacy = tmp_path / "legacy/77/bot_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"bot_state":{"n1":{"status":"exclusive"}}}')
    rows = [
        {
            "id": 77,
            "uuid": "legacy-import",
            "name": "legacy",
            "bot_type": "NODE",
            "cluster_configs": '["n1"]',
            "config_overrides": json.dumps({"DATA_DIR": str(tmp_path / "legacy")}),
        }
    ]
    with patch("lockbot.backend.app.backup.service._read_backup_bots", return_value=rows):
        result = restore_apply(
            _build_backup_zip(bot_uuid="legacy-import", bot_id=77), None, None, ["legacy-import"], 1, db_session
        )
    assert result["restored"] == 0 and result["errors"]
    assert db_session.query(Bot).count() == 0
    assert legacy.read_text() == '{"bot_state":{"n1":{"status":"exclusive"}}}'
