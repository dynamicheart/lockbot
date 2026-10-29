"""Actual managed IO, confinement, ingestion, and legacy layout compatibility."""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from lockbot.backend.app import config
from lockbot.backend.app.bots import router
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.managed_paths import ManagedPathError, ManagedPaths
from lockbot.core.config import Config
from lockbot.core.io import _bot_dir, create_or_load_node_state, log_to_file, save_bot_state_to_file


def bot_record(overrides=None):
    return SimpleNamespace(
        id=1,
        name="path-test",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url="",
        aes_key="",
        token="",
        cluster_configs='["n1"]',
        config_overrides=json.dumps(overrides or {}),
    )


def test_managed_actual_save_load_and_logs_match_root(tmp_path):
    cfg = Config(router._build_config_dict(bot_record()))
    paths = config.MANAGED_PATHS
    assert paths.root == tmp_path
    assert cfg.get_val("DATA_DIR") == str(paths.bots_root)
    assert Path(_bot_dir(cfg)) == paths.bot_dir(1)
    state = {
        "n1": {
            "status": "exclusive",
            "current_users": [{"user_id": "a", "duration": 999999, "start_time": 1}],
            "booking_list": [],
        }
    }
    save_bot_state_to_file(state, cfg)
    loaded, _ = create_or_load_node_state(cfg)
    assert loaded == state
    log_to_file("a", "lock", "n1", config=cfg)
    router._write_log(1, "managed-test")
    assert paths.bot_file(1, "bot.log").is_file()
    assert "managed-test" in paths.bot_file(1, "logs.jsonl").read_text()
    assert json.loads(paths.state_file(1).read_text())["bot_state"] == state


def test_unset_environment_actual_core_path_and_guard_agree():
    script = """
from types import SimpleNamespace
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.bots.router import _build_config_dict, _get_bot_data_dir
from lockbot.backend.app.state_ownership import state_lock_path
from lockbot.core.io import _bot_dir
from lockbot.core.config import Config
from unittest.mock import patch
from pathlib import Path
b=SimpleNamespace(id=987654321,name="p",bot_type="NODE",platform="Infoflow",webhook_url="",aes_key="",token="",
                  cluster_configs='["n1"]',config_overrides="{}")
p=get_managed_paths()
with patch("lockbot.core.io.os.makedirs"):
    actual=Path(_bot_dir(Config(_build_config_dict(b)))) / "bot_state.json"
assert actual == p.state_file(b.id) == Path(_get_bot_data_dir(b)) / "bot_state.json"
assert Path(state_lock_path(str(p.root))) == p.lock_file
assert actual.is_relative_to(p.root)
"""
    env = dict(os.environ, PYTHONPATH=str(Path("python").resolve()))
    env.pop("DATA_DIR", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_override_cannot_control_actual_io(tmp_path, caplog):
    outside = tmp_path / "outside"
    cfg = Config(
        router._build_config_dict(bot_record({"DATA_DIR": str(outside), "STATE_FILENAME": str(outside / "x")}))
    )
    save_bot_state_to_file({"probe": "persisted"}, cfg)
    assert config.MANAGED_PATHS.state_file(1).is_file()
    assert not outside.exists()
    assert "DATA_DIR" in caplog.text
    assert str(outside) not in caplog.text


@pytest.mark.parametrize(
    "key",
    [
        "DATA_DIR",
        "STATE_FILENAME",
        "CONFIG_FILENAME",
        "LOG_DIR",
        "BOT_ID",
        "_MANAGED_BOT_DIR",
        "_MESSAGING_CREDENTIAL_GUARD",
    ],
)
def test_create_update_reject_managed_controls(client, admin_header, db_session, key):
    payload = {"name": "ingest", "bot_type": "NODE", "webhook_url": "", "cluster_configs": ["n1"]}
    response = client.post("/api/bots", headers=admin_header, json={**payload, "config_overrides": {key: "outside"}})
    assert response.status_code == 422
    assert db_session.query(Bot).count() == 0
    created = client.post("/api/bots", headers=admin_header, json=payload)
    assert created.status_code == 201
    bot_id = created.json()["id"]
    response = client.put(
        f"/api/bots/{bot_id}", headers=admin_header, json={"config_overrides": {key: "outside", "LANGUAGE": "en"}}
    )
    assert response.status_code == 422
    db_session.expire_all()
    assert json.loads(db_session.get(Bot, bot_id).config_overrides) == {}


@pytest.mark.parametrize("target", ["bots", "bot", "state", "legacy", "core-log", "backend-log", "lock"])
def test_symlink_escape_refused_on_real_io(tmp_path, target):
    paths = config.MANAGED_PATHS
    cfg = Config(router._build_config_dict(bot_record()))
    directory = paths.bot_dir(1)
    directory.mkdir(parents=True)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    targets = {
        "bots": paths.bots_root,
        "bot": directory,
        "state": paths.state_file(1),
        "legacy": paths.bot_file(1, "cluster_status.json"),
        "core-log": paths.bot_file(1, "bot.log"),
        "backend-log": paths.bot_file(1, "logs.jsonl"),
        "lock": paths.lock_file,
    }
    victim = targets[target]
    if victim.is_dir():
        if victim == paths.bots_root:
            directory.rmdir()
        victim.rmdir()
        victim.symlink_to(outside, target_is_directory=True)
    else:
        (outside / "sensitive").write_text("untouched")
        victim.symlink_to(outside / "sensitive")
    with pytest.raises(ManagedPathError):
        if target == "backend-log":
            router._write_log(1, "refused")
        elif target == "lock":
            from lockbot.backend.app.state_ownership import acquire_state_store_ownership

            acquire_state_store_ownership(str(paths.root))
        else:
            # The callback must validate again even though cfg predates the symlink.
            save_bot_state_to_file({"bad": True}, cfg)
    assert not (outside / "bot_state.json").exists()
    if (outside / "sensitive").exists():
        assert (outside / "sensitive").read_text() == "untouched"


def test_root_alias_is_canonical_and_immutable(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    paths = ManagedPaths(alias)
    assert paths == ManagedPaths(actual)
    with pytest.raises(FrozenInstanceError):
        paths.root = tmp_path
    with patch.dict(os.environ, DATA_DIR="/different-root"):
        assert paths.root == actual


@pytest.mark.parametrize("bot_id", ["../2", "1/../../x", "0", -1, True, "１"])
def test_invalid_bot_ids_are_rejected(tmp_path, bot_id):
    with pytest.raises(ManagedPathError):
        ManagedPaths(tmp_path).state_file(bot_id)


def test_legacy_core_layout_unchanged(tmp_path):
    cfg = Config({"BOT_ID": "standalone", "DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": ["n1"]})
    save_bot_state_to_file({"n1": {"status": "idle", "current_users": [], "booking_list": []}}, cfg)
    assert (tmp_path / "standalone/bot_state.json").exists()
    assert not (tmp_path / "bots").exists()


def test_offline_edit_real_start_reload_uses_same_file(client, admin_header, db_session):
    from lockbot.backend.app.bots.manager import BotManager

    response = client.post(
        "/api/bots",
        headers=admin_header,
        json={"name": "offline-reload", "bot_type": "NODE", "webhook_url": "", "cluster_configs": ["n1"]},
    )
    assert response.status_code == 201
    bot_id = response.json()["id"]
    state = {
        "n1": {
            "status": "idle",
            "current_users": [],
            "booking_list": [{"user_id": "queued", "start_time": 1, "duration": 3600, "is_notified": False}],
        }
    }
    response = client.put(f"/api/bots/{bot_id}/state", headers=admin_header, json=state)
    assert response.status_code == 200, response.text
    persisted = json.loads(config.MANAGED_PATHS.state_file(bot_id).read_text())["bot_state"]
    manager = BotManager()
    with db_session:
        cfg = router._build_config_dict(db_session.get(Bot, bot_id), db_session)
    manager.start_bot(bot_id, cfg)
    try:
        with manager.admit(bot_id, for_read=True) as lease:
            _, actual = lease.bot.state_snapshot()
        assert actual == persisted
        assert actual["n1"]["booking_list"][0]["user_id"] == "queued"
    finally:
        manager.stop_bot(bot_id)


def test_archive_restore_and_actual_start_roundtrip(tmp_path, monkeypatch):
    import zipfile

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.backup import service as backup
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database_file = tmp_path / "source.sqlite"
    engine = create_engine(f"sqlite:///{database_file}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(User(id=1, username="archive-owner", email="a@test.invalid", password_hash="unused", role="admin"))
        row = Bot(
            id=1,
            uuid="archive-id",
            user_id=1,
            name="archive",
            bot_type="NODE",
            platform="Infoflow",
            webhook_url="",
            cluster_configs='["n1"]',
            config_overrides="{}",
            status="stopped",
        )
        db.add(row)
        db.commit()
        cfg = Config(router._build_config_dict(row, db))
    state = {
        "n1": {
            "status": "idle",
            "current_users": [],
            "booking_list": [{"user_id": "queued", "start_time": 1, "duration": 60, "is_notified": False}],
        }
    }
    save_bot_state_to_file(state, cfg)
    journal = config.MANAGED_PATHS.bot_dir(1) / ".transition"
    journal.mkdir()
    (journal / "bot_state.json").write_text("must not be archived")
    monkeypatch.setattr(backup, "DATABASE_URL", f"sqlite:///{database_file}?timeout=10")
    archive_path = backup.create_backup_archive()
    try:
        with zipfile.ZipFile(archive_path) as zf:
            assert "bots/1/bot_state.json" in zf.namelist()
            assert not any(".transition" in name for name in zf.namelist())
            assert json.loads(zf.read("bots/1/bot_state.json"))["bot_state"] == state
        content = archive_path.read_bytes()
    finally:
        archive_path.unlink()
        archive_path.parent.rmdir()
    target = ManagedPaths(tmp_path / "restored-deployment")
    monkeypatch.setattr(config, "MANAGED_PATHS", target)
    monkeypatch.setattr(config, "LEGACY_CORE_ROOT", target.root)
    with sessions() as db:
        result = backup.restore_apply(content, None, None, ["archive-id"], 1, db)
        assert result["restored"] == 1 and result["errors"] == []
        assert json.loads(target.state_file(1).read_text())["bot_state"] == state
        cfg = router._build_config_dict(db.get(Bot, 1), db)
    manager = BotManager()
    manager.start_bot(1, cfg)
    try:
        with manager.admit(1, for_read=True) as lease:
            _, loaded = lease.bot.state_snapshot()
        assert loaded == state
    finally:
        manager.stop_bot(1)
        engine.dispose()


def test_restored_control_overrides_are_sanitized(tmp_path, db_session):
    import io
    import zipfile
    from unittest.mock import patch

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.backup.service import restore_apply

    from backend.test_backup_restore import _build_backup_zip

    db_session.add(User(id=1, username="restore-owner", email="r@test.invalid", password_hash="unused", role="admin"))
    db_session.commit()
    payload = _build_backup_zip(
        state_data={"bot_state": {"node1": {"status": "idle", "current_users": [], "booking_list": []}}}
    )
    # Feed the actual archive parser its normal rows, with poisoned path control keys.
    from lockbot.backend.app.backup.service import _read_backup_bots

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        rows = _read_backup_bots(zf)
    rows[0]["config_overrides"] = json.dumps(
        {
            "DATA_DIR": str(tmp_path / "outside"),
            "STATE_FILENAME": "/outside",
            "LANGUAGE": "en",
            "BOT_ID": 999,
            "_MESSAGING_CREDENTIAL_GUARD": "fake-guard",
        }
    )
    with patch("lockbot.backend.app.backup.service._read_backup_bots", return_value=rows):
        result = restore_apply(payload, None, None, ["bot-uuid-1"], 1, db_session)
    assert result["created"] == 1
    row = db_session.query(Bot).one()
    assert json.loads(row.config_overrides) == {"LANGUAGE": "en"}
    save_bot_state_to_file({"saved": True}, Config(router._build_config_dict(row)))
    assert config.MANAGED_PATHS.state_file(row.id).is_file()
    assert not (tmp_path / "outside").exists()


def test_shared_database_cannot_bypass_root_lock_in_another_process(tmp_path):
    from lockbot.backend.app.state_ownership import (
        acquire_database_ownership,
        acquire_state_store_ownership,
        release_state_store_ownership,
    )

    database_url = f"sqlite:///{tmp_path / 'shared.db'}"
    acquire_state_store_ownership(str(tmp_path / "root-a"))
    acquire_database_ownership(database_url)
    script = """
import sys
from lockbot.backend.app.state_ownership import acquire_database_ownership, acquire_state_store_ownership
from lockbot.backend.app.state_ownership import StateStoreOwnershipError
acquire_state_store_ownership(sys.argv[1])
try:
    acquire_database_ownership(sys.argv[2])
except StateStoreOwnershipError:
    sys.exit(3)
sys.exit(0)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "root-b"), database_url],
            env=dict(os.environ, PYTHONPATH=str(Path("python").resolve())),
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 3, result.stdout + result.stderr
    finally:
        release_state_store_ownership()


def test_core_guard_rechecks_hardlinks_and_in_root_aliases(tmp_path):
    cfg = Config(router._build_config_dict(bot_record()))
    target = config.MANAGED_PATHS.state_file(1)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "other-file"
    outside.write_text("unchanged")
    os.link(outside, target)
    with pytest.raises(ManagedPathError, match="Hard-linked"):
        log_to_file("u", "query", "n1", config=cfg)
    assert outside.read_text() == "unchanged"
    target.unlink()
    target.parent.rmdir()
    sibling = config.MANAGED_PATHS.bot_dir(2)
    sibling.mkdir()
    target.parent.symlink_to(sibling, target_is_directory=True)
    with pytest.raises(ManagedPathError, match="Symlink"):
        save_bot_state_to_file({}, cfg)


def test_database_url_consumers_share_query_and_literal_tilde_semantics(tmp_path, monkeypatch):
    from lockbot.backend.app.sqlite_paths import canonical_sqlite_url, sqlite_database_file
    from sqlalchemy import create_engine, text

    monkeypatch.chdir(tmp_path)
    (tmp_path / "~").mkdir()
    url = canonical_sqlite_url("sqlite:///~/db.sqlite?timeout=10")
    expected = tmp_path / "~/db.sqlite"
    assert sqlite_database_file(url) == expected
    engine = create_engine(url)
    with engine.connect() as connection:
        actual = connection.execute(text("PRAGMA database_list")).fetchone()[2]
    engine.dispose()
    assert Path(actual) == expected
    for uri in (f"sqlite:///file:{expected}?uri=true&mode=rw", "sqlite:///file:memory?uri=true"):
        with pytest.raises(ValueError, match="URI"):
            canonical_sqlite_url(uri)


def test_database_sidecar_hardlink_is_refused_without_truncation(tmp_path):
    from lockbot.backend.app.state_ownership import StateStoreOwnershipError, acquire_database_ownership

    victim = tmp_path / "important"
    victim.write_text("do not truncate")
    database = tmp_path / "db.sqlite"
    os.link(victim, tmp_path / "db.sqlite.lockbot-owner.lock")
    with pytest.raises(StateStoreOwnershipError, match="non-hard-linked"):
        acquire_database_ownership(f"sqlite:///{database}")
    assert victim.read_text() == "do not truncate"
