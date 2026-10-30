"""Unknown/NULL journals retain target dependencies in actual malformed SQLite storage."""

import sqlite3

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.encryption import encrypt
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.feishu.models import FeishuApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.parametrize("version,phase", [(99, "completed"), (None, "completed"), (1, None), (1, "planned")])
def test_application_delete_preserves_unknown_or_unfinished_journal_dependency(
    tmp_path, client, admin_header, version, phase
):
    engine = create_engine("sqlite:///" + str(tmp_path / "malformed.sqlite"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            User(
                id=1,
                username="adminuser",
                email="admin-fixture@example.invalid",
                password_hash="FAKE_UNUSABLE_HASH",
                role="super_admin",
                token_version=0,
                must_change_password=False,
            )
        )
        db.add(
            FeishuApplication(
                id=42,
                app_id="cli_preserve_fixture",
                name="Preserved fixture",
                tenant_key="tenant",
                bot_open_id="ou_fixture",
                app_secret=encrypt("FAKE_SECRET"),
                verification_token=encrypt("FAKE_VERIFY"),
                encrypt_key=encrypt(""),
                transport="websocket",
            )
        )
        db.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE bot_migrations RENAME TO strict_original")
        connection.exec_driver_sql("CREATE TABLE bot_migrations AS SELECT * FROM strict_original WHERE 0")
        connection.exec_driver_sql(
            "INSERT INTO bot_migrations (id, bot_id, target_application_id, schema_version, phase) "
            "VALUES (?, ?, ?, ?, ?)",
            ("f" * 32, 99, 42, version, phase),
        )

    def isolated_db():
        with factory() as db:
            yield db

    test_app = client.app
    previous = test_app.dependency_overrides.get(get_db)
    test_app.dependency_overrides[get_db] = isolated_db
    try:
        response = client.delete("/api/feishu/applications/42", headers=admin_header)
        assert response.status_code == 409, response.text
        with factory() as db:
            assert db.get(FeishuApplication, 42) is not None
        with sqlite3.connect(tmp_path / "malformed.sqlite") as db:
            assert db.execute("SELECT schema_version, phase FROM bot_migrations").fetchone() == (version, phase)
    finally:
        if previous is None:
            test_app.dependency_overrides.pop(get_db, None)
        else:
            test_app.dependency_overrides[get_db] = previous
        engine.dispose()
