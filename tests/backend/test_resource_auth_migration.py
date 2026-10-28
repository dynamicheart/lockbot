"""AC-12: the schema change must be idempotent and safe on upgrade.

There is no Alembic in this project; migrations are hand-written, inspector-guarded
functions called from the FastAPI lifespan. These tests exercise the three cases that
matter: a fresh install, an upgrade from a database that predates the change, and a
repeated startup.

A file-backed SQLite database is used rather than the shared in-memory one, so each
scenario starts from a genuinely separate schema.
"""

import pytest
from lockbot.backend.app import main as app_main
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def file_engine(tmp_path, monkeypatch):
    """An isolated on-disk database, with the migration helpers pointed at it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    monkeypatch.setattr(app_main, "engine", engine)
    yield engine
    engine.dispose()


def _tables(engine):
    return set(inspect(engine).get_table_names())


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


class TestFreshInstall:
    def test_create_all_includes_the_binding_table(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        assert "resource_bindings" in _tables(file_engine)

    def test_migration_is_a_noop_on_a_fresh_install(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        app_main._migrate_resource_bindings()
        app_main._migrate_bots_resource_auth_enabled()
        assert "resource_bindings" in _tables(file_engine)

    def test_the_model_is_registered_before_table_creation(self):
        """Importing main must register the model, or create_all would silently skip it."""
        assert "resource_bindings" in Base.metadata.tables


class TestUpgrade:
    def test_binding_table_is_created_on_a_database_without_it(self, file_engine):
        for name, table in Base.metadata.tables.items():
            if name != "resource_bindings":
                table.create(bind=file_engine)
        assert "resource_bindings" not in _tables(file_engine)

        app_main._migrate_resource_bindings()
        assert "resource_bindings" in _tables(file_engine)

    def test_enrollment_column_is_added_to_an_existing_bots_table(self, file_engine):
        """Simulate a bots table written before the column existed."""
        with file_engine.begin() as conn:
            conn.execute(text("CREATE TABLE bots (id INTEGER PRIMARY KEY, name VARCHAR(128))"))
            conn.execute(text("INSERT INTO bots (id, name) VALUES (1, 'legacy')"))
        assert "resource_auth_enabled" not in _columns(file_engine, "bots")

        app_main._migrate_bots_resource_auth_enabled()

        assert "resource_auth_enabled" in _columns(file_engine, "bots")
        with file_engine.connect() as conn:
            value = conn.execute(text("SELECT resource_auth_enabled FROM bots WHERE id = 1")).scalar()
        assert value, "schemas without an access setting adopt the enabled default"


class TestRepeatedStartup:
    def test_running_the_binding_migration_twice_does_not_error(self, file_engine):
        for name, table in Base.metadata.tables.items():
            if name != "resource_bindings":
                table.create(bind=file_engine)
        app_main._migrate_resource_bindings()
        app_main._migrate_resource_bindings()
        assert sorted(_tables(file_engine)).count("resource_bindings") == 1

    def test_running_the_column_migration_twice_does_not_error(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        app_main._migrate_bots_resource_auth_enabled()
        app_main._migrate_bots_resource_auth_enabled()
        assert "resource_auth_enabled" in _columns(file_engine, "bots")


class TestSchemaShape:
    """AC-12: the durable table describes a confirmed binding only."""

    def test_no_pending_state_columns_exist(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        columns = _columns(file_engine, "resource_bindings")
        for forbidden in ("code_digest", "bind_code_digest", "bind_expires_at", "status", "confirmed"):
            assert forbidden not in columns, f"{forbidden} belongs to the pending store, not the table"

    def test_the_expected_columns_exist(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        columns = _columns(file_engine, "resource_bindings")
        assert {
            "public_id",
            "platform",
            "external_user_id",
            "bound_bot_id",
            "bound_bot_uuid",
            "credential_digest",
            "issued_at",
            "credential_expires_at",
            "revoked_at",
            "last_used_at",
        } <= columns

    def test_credential_expiry_is_nullable(self, file_engine):
        Base.metadata.create_all(bind=file_engine)
        column = next(
            c for c in inspect(file_engine).get_columns("resource_bindings") if c["name"] == "credential_expires_at"
        )
        assert column["nullable"] is True


class TestBindingUniquenessMigration:
    @staticmethod
    def _legacy(engine, duplicate=False):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE resource_bindings (id INTEGER PRIMARY KEY, platform TEXT, "
                    "external_user_id TEXT, bound_bot_id INTEGER, revoked_at DATETIME)"
                )
            )
            conn.execute(text("INSERT INTO resource_bindings VALUES (1, 'im', 'alice', 1, NULL)"))
            if duplicate:
                conn.execute(text("INSERT INTO resource_bindings VALUES (2, 'im', 'alice', 1, NULL)"))

    def test_fresh_index_and_repeated_startup(self, file_engine):
        Base.metadata.create_all(file_engine)
        app_main._migrate_binding_uniqueness()
        app_main._migrate_binding_uniqueness()

    def test_upgrade_creates_the_constraint(self, file_engine):
        from sqlalchemy.exc import IntegrityError

        self._legacy(file_engine)
        app_main._migrate_binding_uniqueness()
        app_main._migrate_binding_uniqueness()
        with pytest.raises(IntegrityError), file_engine.begin() as conn:
            conn.execute(text("INSERT INTO resource_bindings VALUES (2, 'im', 'alice', 1, NULL)"))
        with file_engine.begin() as conn:
            conn.execute(text("INSERT INTO resource_bindings VALUES (3, 'im', 'alice', 1, '2026-01-01')"))
            conn.execute(text("INSERT INTO resource_bindings VALUES (4, 'im', 'alice', 2, NULL)"))
            conn.execute(text("INSERT INTO resource_bindings VALUES (5, 'im', 'bob', 1, NULL)"))

    def test_legacy_duplicates_fail_without_choosing_a_winner(self, file_engine):
        self._legacy(file_engine, duplicate=True)
        with pytest.raises(RuntimeError, match="will not choose"):
            app_main._migrate_binding_uniqueness()
        with file_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM resource_bindings WHERE revoked_at IS NULL")).scalar() == 2

    def test_malformed_existing_index_is_rejected(self, file_engine):
        self._legacy(file_engine)
        with file_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_resource_binding_live_principal_bot "
                    "ON resource_bindings(platform, external_user_id, bound_bot_id)"
                )
            )
        with pytest.raises(RuntimeError, match="Invalid"):
            app_main._migrate_binding_uniqueness()

    def test_concurrent_confirmations_leave_exactly_one_effective_key(self, file_engine, monkeypatch):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from lockbot.backend.app.auth.models import User
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.resource_auth import service
        from lockbot.backend.app.resource_auth.models import ResourceBinding
        from sqlalchemy.orm import sessionmaker

        Base.metadata.create_all(file_engine)
        sessions = sessionmaker(bind=file_engine)
        monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", sessions)
        with sessions() as db:
            db.add(User(id=1, username="owner", email="test@invalid", password_hash="unused", role="user"))
            db.add(
                Bot(
                    id=1,
                    uuid="concurrent-bot",
                    user_id=1,
                    name="test-bot",
                    bot_type="NODE",
                    platform="fakeim",
                    webhook_url="",
                    cluster_configs='["n1"]',
                    resource_auth_enabled=True,
                )
            )
            db.commit()
        issued = [service.create_binding(source="concurrent-test") for _ in range(2)]
        barrier = threading.Barrier(2)

        def confirm(item):
            barrier.wait(timeout=5)
            return service.handle_chat_binding(
                platform="fakeim", external_user_id="alice", bot_id=1, action="bind", argument=item["bind_code"]
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(confirm, issued))
        with sessions() as db:
            assert db.query(ResourceBinding).filter_by(revoked_at=None).count() == 1
            assert (
                sum(
                    service.authenticate(db, bot_id=1, bot_uuid="concurrent-bot", token=i["credential"]) is not None
                    for i in issued
                )
                == 1
            )
        assert sum("未激活" in reply for reply in replies) == 1
