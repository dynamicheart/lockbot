"""Ordinary archives must never present an in-progress cutover as a resumable backup."""

import json
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from lockbot.backend.app.backup import service as backup
from lockbot.backend.app.feishu.connectors import connectors
from lockbot.backend.app.managed_paths import ManagedPaths


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    database = tmp_path / "database.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE bot_migrations (phase TEXT, schema_version INTEGER)")
        connection.execute("CREATE TABLE bots (id INTEGER, platform TEXT)")
        connection.execute("INSERT INTO bots VALUES (1, 'Infoflow')")
    paths = ManagedPaths(tmp_path / "data")
    paths.bot_dir(1).mkdir(parents=True)
    paths.state_file(1).write_text(json.dumps({"bot_state": {"node1": {"status": "idle"}}}))
    checkpoint = paths.bot_file(1, "migration-" + "a" * 32 + ".json")
    checkpoint.write_text("fixture evidence must remain untouched")
    monkeypatch.setattr(backup, "DATABASE_URL", "sqlite:///" + str(database))
    monkeypatch.setattr(backup, "get_managed_paths", lambda: paths)
    return database, paths, checkpoint


@pytest.mark.parametrize(
    "phase,version",
    [
        ("planned", 1),
        ("freezing", 1),
        ("checkpointed", 1),
        ("switched", 1),
        ("activating", 1),
        ("aborting", 1),
        ("unknown_future_phase", 1),
        ("completed", 99),
        ("aborted", None),
    ],
)
def test_active_or_unknown_journal_refuses_before_any_archive_is_created(deployment, monkeypatch, phase, version):
    database, paths, checkpoint = deployment
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO bot_migrations VALUES (?, ?)", (phase, version))
    before = paths.state_file(1).read_bytes(), checkpoint.read_bytes()
    monkeypatch.setattr(backup.tempfile, "mkdtemp", lambda **_: pytest.fail("no artifact may be created"))
    with pytest.raises(RuntimeError, match="migrations"):
        backup.create_backup_archive()
    assert (paths.state_file(1).read_bytes(), checkpoint.read_bytes()) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT phase, schema_version FROM bot_migrations").fetchone() == (phase, version)


@pytest.mark.parametrize("phase", ["completed", "aborted"])
def test_terminal_journal_allows_normal_backup_without_deleting_evidence(deployment, phase):
    database, paths, checkpoint = deployment
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO bot_migrations VALUES (?, 1)", (phase,))
    archive = backup.create_backup_archive()
    try:
        with zipfile.ZipFile(archive) as source:
            assert source.read("bots/1/bot_state.json") == paths.state_file(1).read_bytes()
            assert "lockbot.db" in source.namelist()
            assert not any("migration-" in name for name in source.namelist())
        assert checkpoint.read_text() == "fixture evidence must remain untouched"
    finally:
        archive.unlink()
        archive.parent.rmdir()


def test_unknown_journal_schema_is_refused_before_artifact_creation(deployment, monkeypatch):
    database, _, _ = deployment
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE bot_migrations")
        connection.execute("CREATE TABLE bot_migrations (future TEXT)")
    monkeypatch.setattr(backup.tempfile, "mkdtemp", lambda **_: pytest.fail("no artifact may be created"))
    with pytest.raises(RuntimeError, match="Unsupported migration journal"):
        backup.create_backup_archive()


def test_coordinator_phase_cannot_change_mid_archive_copy(deployment, monkeypatch):
    database, _, _ = deployment
    entered, release, migration_started = threading.Event(), threading.Event(), threading.Event()
    original = backup._build_manifest

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def begin_migration():
        with connectors.application_guard():
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO bot_migrations VALUES ('freezing', 1)")
            migration_started.set()

    monkeypatch.setattr(backup, "_build_manifest", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(backup.create_backup_archive)
        assert entered.wait(5)
        migration = pool.submit(begin_migration)
        assert not migration_started.wait(0.1)
        release.set()
        archive = future.result(timeout=5)
        migration.result(timeout=5)
    try:
        with zipfile.ZipFile(archive) as source:
            snapshot = database.parent / "archived.sqlite"
            snapshot.write_bytes(source.read("lockbot.db"))
        with sqlite3.connect(snapshot) as connection:
            assert connection.execute("SELECT COUNT(*) FROM bot_migrations").fetchone()[0] == 0
        with pytest.raises(RuntimeError, match="migrations"):
            backup.create_backup_archive()
    finally:
        archive.unlink()
        archive.parent.rmdir()


def test_backup_waits_for_coordinator_then_refuses_new_incomplete_phase(deployment, monkeypatch):
    database, _, _ = deployment
    started = threading.Event()

    def create():
        started.set()
        return backup.create_backup_archive()

    monkeypatch.setattr(backup.tempfile, "mkdtemp", lambda **_: pytest.fail("no artifact may be created"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connectors.application_guard():
            future = pool.submit(create)
            assert started.wait(5)
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO bot_migrations VALUES ('checkpointed', 1)")
        with pytest.raises(RuntimeError, match="migrations"):
            future.result(timeout=5)
