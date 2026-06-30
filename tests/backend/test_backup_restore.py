"""Backup restore preview and apply tests."""

import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from io import BytesIO
from unittest.mock import patch

import pytest
from lockbot.backend.app.bots.encryption import decrypt, encrypt
from lockbot.backend.app.bots.models import Bot


# ---------------------------------------------------------------------------
# Helpers to access the shared _TestSession from conftest
# ---------------------------------------------------------------------------
def _get_test_session():
    """Return the _TestSession factory from conftest (already loaded by pytest)."""
    for _key, _mod in sys.modules.items():
        if "conftest" in _key and hasattr(_mod, "_TestSession"):
            return _mod._TestSession
    raise RuntimeError("Cannot locate _TestSession from conftest")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def super_admin_header(client):
    """Create a super_admin user and return its auth header."""
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password

    _TestSession = _get_test_session()
    with _TestSession() as session:
        sa = User(
            username="superadmin",
            email="sa@test.com",
            password_hash=_hash_password("sapass123"),
            role="super_admin",
        )
        session.add(sa)
        session.commit()

    resp = client.post("/api/auth/login", json={"username": "superadmin", "password": "sapass123"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _build_backup_zip(bot_uuid="bot-uuid-1", bot_name="restored-bot", state_data=None, bot_id=1):
    """Build a minimal backup zip with one bot.

    Args:
        bot_uuid: UUID for the bot in the backup DB.
        bot_name: Name for the bot.
        state_data: If provided, bytes or dict to include as a bot_state.json file
                    under bots/{bot_id}/bot_state.json in the zip.
        bot_id: The numeric id for the bot row (used for state file path).
    """
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn = sqlite3.connect(f.name)
        conn.execute(
            "CREATE TABLE bots ("
            "id INTEGER PRIMARY KEY, uuid TEXT, name TEXT, bot_type TEXT, platform TEXT, "
            "group_id TEXT, webhook_url TEXT, aes_key TEXT, token TEXT, "
            "cluster_configs TEXT, config_overrides TEXT, is_deleted INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO bots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, bot_uuid, bot_name, "NODE", "Infoflow", "g1", "bad", "bad", "bad", '["node1"]', "{}", 0),
        )
        conn.commit()
        conn.close()
        with open(f.name, "rb") as db_file:
            db_bytes = db_file.read()

    manifest = {"format": "lockbot-backup", "format_version": 1}
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("lockbot.db", db_bytes)
        if state_data is not None:
            if isinstance(state_data, dict):
                state_data = json.dumps(state_data).encode()
            zf.writestr(f"bots/{bot_id}/bot_state.json", state_data)
    buf.seek(0)
    return buf.getvalue()


def _build_backup_zip_with_encrypted_creds(
    bot_uuid="bot-uuid-enc",
    bot_name="enc-bot",
    webhook="https://example.com/hook",
    aes_key="myaeskey",
    token="mytoken",
):
    """Build a backup zip where credential fields are encrypted with the CURRENT key."""
    enc_webhook = encrypt(webhook)
    enc_aes_key = encrypt(aes_key)
    enc_token = encrypt(token)

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn = sqlite3.connect(f.name)
        conn.execute(
            "CREATE TABLE bots ("
            "id INTEGER PRIMARY KEY, uuid TEXT, name TEXT, bot_type TEXT, platform TEXT, "
            "group_id TEXT, webhook_url TEXT, aes_key TEXT, token TEXT, "
            "cluster_configs TEXT, config_overrides TEXT, is_deleted INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO bots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                bot_uuid,
                bot_name,
                "NODE",
                "Infoflow",
                "g1",
                enc_webhook,
                enc_aes_key,
                enc_token,
                '["node1"]',
                "{}",
                0,
            ),
        )
        conn.commit()
        conn.close()
        with open(f.name, "rb") as db_file:
            db_bytes = db_file.read()

    manifest = {"format": "lockbot-backup", "format_version": 1}
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("lockbot.db", db_bytes)
    buf.seek(0)
    return buf.getvalue()


# ===========================================================================
# Preview tests (existing)
# ===========================================================================


class TestRestorePreview:
    def test_restore_preview_create(self, client, super_admin_header):
        resp = client.post(
            "/api/admin/backup/restore/preview",
            files={"file": ("backup.zip", _build_backup_zip(), "application/zip")},
            headers=super_admin_header,
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["manifest"]["format"] == "lockbot-backup"
        assert payload["bots"][0]["name"] == "restored-bot"
        assert payload["bots"][0]["action"] == "create"
        assert payload["bots"][0]["credentials_ok"] is False

    def test_restore_preview_overwrite_by_uuid(self, client, admin_header, super_admin_header):
        bot = {
            "name": "existing-bot",
            "bot_type": "NODE",
            "webhook_url": "https://example.com/webhook",
            "aes_key": "testaeskey",
            "token": "testtoken",
            "cluster_configs": ["node1"],
        }
        created = client.post("/api/bots", json=bot, headers=admin_header).json()
        resp = client.post(
            "/api/admin/backup/restore/preview",
            files={"file": ("backup.zip", _build_backup_zip(bot_uuid=created["uuid"]), "application/zip")},
            headers=super_admin_header,
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["bots"][0]["action"] == "overwrite"
        assert payload["bots"][0]["existing_id"] == created["id"]


# ===========================================================================
# restore_apply function-level tests
# ===========================================================================


class TestRestoreApplyFunction:
    """Test restore_apply() directly (not via HTTP endpoint)."""

    def test_apply_creates_new_bot_when_uuid_not_exists(self, client, admin_header, db_session, tmp_path):
        """restore_apply creates a new bot when the uuid does not match any existing bot."""
        from lockbot.backend.app.backup.service import restore_apply

        bot_uuid = "new-uuid-not-in-db"
        state_content = {"bot_state": {"node1": {"status": "idle", "current_users": [], "booking_list": []}}}
        zip_bytes = _build_backup_zip(bot_uuid=bot_uuid, bot_name="new-bot", state_data=state_content)

        # Get the admin user id
        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=None,
                selected_uuids=[bot_uuid],
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 1
        assert result["overwritten"] == 0
        assert result["restored"] == 1
        assert result["errors"] == []

        # Verify bot exists in DB
        new_bot = db_session.query(Bot).filter(Bot.uuid == bot_uuid).first()
        assert new_bot is not None
        assert new_bot.name == "new-bot"
        assert new_bot.bot_type == "NODE"
        assert new_bot.user_id == admin_id

    def test_apply_overwrites_existing_bot_by_uuid(self, client, admin_header, db_session, tmp_path):
        """restore_apply overwrites a bot when the uuid already exists in DB."""
        from lockbot.backend.app.backup.service import restore_apply

        # Create a bot via API first
        resp = client.post(
            "/api/bots",
            json={
                "name": "original-bot",
                "bot_type": "NODE",
                "webhook_url": "https://example.com/hook",
                "aes_key": "origkey",
                "token": "origtoken",
                "cluster_configs": ["n1"],
            },
            headers=admin_header,
        )
        assert resp.status_code == 201
        existing_uuid = resp.json()["uuid"]
        existing_id = resp.json()["id"]

        # Build a backup zip containing the same uuid but different name/configs
        zip_bytes = _build_backup_zip(bot_uuid=existing_uuid, bot_name="overwritten-bot")

        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=None,
                selected_uuids=[existing_uuid],
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 0
        assert result["overwritten"] == 1
        assert result["restored"] == 1
        assert result["errors"] == []

        # Verify the name was changed
        db_session.expire_all()
        bot = db_session.get(Bot, existing_id)
        assert bot.name == "overwritten-bot"

    def test_apply_copies_state_file(self, client, admin_header, db_session, tmp_path):
        """restore_apply writes bot_state.json to the correct data directory."""
        from lockbot.backend.app.backup.service import restore_apply

        bot_uuid = "state-test-uuid"
        state_content = {
            "bot_state": {"node1": {"status": "exclusive", "current_users": [{"user_id": "u1"}], "booking_list": []}}
        }
        zip_bytes = _build_backup_zip(bot_uuid=bot_uuid, bot_name="state-bot", state_data=state_content)

        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=None,
                selected_uuids=[bot_uuid],
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 1

        # Get the newly created bot id
        new_bot = db_session.query(Bot).filter(Bot.uuid == bot_uuid).first()
        state_file_path = os.path.join(str(tmp_path), "bots", str(new_bot.id), "bot_state.json")
        assert os.path.exists(state_file_path)

        with open(state_file_path) as f:
            restored_state = json.load(f)
        assert restored_state["bot_state"]["node1"]["status"] == "exclusive"

    def test_apply_re_encrypts_credentials(self, client, admin_header, db_session, tmp_path):
        """restore_apply re-encrypts credentials with the current instance key."""
        from lockbot.backend.app.backup.service import restore_apply

        bot_uuid = "cred-test-uuid"
        zip_bytes = _build_backup_zip_with_encrypted_creds(
            bot_uuid=bot_uuid,
            bot_name="cred-bot",
            webhook="https://secret.webhook.com/hook",
            aes_key="secret-aes-key",
            token="secret-token",
        )

        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=None,
                selected_uuids=[bot_uuid],
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 1
        assert result["errors"] == []

        new_bot = db_session.query(Bot).filter(Bot.uuid == bot_uuid).first()
        # Credentials should be re-encrypted; decrypt should return original values
        assert decrypt(new_bot.webhook_url) == "https://secret.webhook.com/hook"
        assert decrypt(new_bot.aes_key) == "secret-aes-key"
        assert decrypt(new_bot.token) == "secret-token"

    def test_apply_skips_unselected_uuids(self, client, admin_header, db_session, tmp_path):
        """restore_apply ignores bots whose uuid is not in selected_uuids."""
        from lockbot.backend.app.backup.service import restore_apply

        bot_uuid = "skip-test-uuid"
        zip_bytes = _build_backup_zip(bot_uuid=bot_uuid, bot_name="skip-bot")

        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=None,
                selected_uuids=["some-other-uuid"],  # does not match
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 0
        assert result["overwritten"] == 0
        assert result["restored"] == 0

        # Bot should NOT exist
        bot = db_session.query(Bot).filter(Bot.uuid == bot_uuid).first()
        assert bot is None

    def test_apply_with_source_secret_key(self, client, admin_header, db_session, tmp_path):
        """restore_apply can decrypt credentials encrypted with a different key."""
        from cryptography.fernet import Fernet
        from lockbot.backend.app.backup.service import restore_apply

        # Generate a separate key to simulate "source instance" encryption
        source_key = Fernet.generate_key().decode()
        source_fernet = Fernet(source_key.encode())

        enc_webhook = source_fernet.encrypt(b"https://foreign.webhook.com").decode()
        enc_aes = source_fernet.encrypt(b"foreign-aes").decode()
        enc_token = source_fernet.encrypt(b"foreign-token").decode()

        bot_uuid = "foreign-key-uuid"
        # Build zip with credentials encrypted by the foreign key
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute(
                "CREATE TABLE bots ("
                "id INTEGER PRIMARY KEY, uuid TEXT, name TEXT, bot_type TEXT, platform TEXT, "
                "group_id TEXT, webhook_url TEXT, aes_key TEXT, token TEXT, "
                "cluster_configs TEXT, config_overrides TEXT, is_deleted INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO bots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    1,
                    bot_uuid,
                    "foreign-bot",
                    "NODE",
                    "Infoflow",
                    "g1",
                    enc_webhook,
                    enc_aes,
                    enc_token,
                    '["node1"]',
                    "{}",
                    0,
                ),
            )
            conn.commit()
            conn.close()
            with open(f.name, "rb") as db_file:
                db_bytes = db_file.read()

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({"format": "lockbot-backup", "format_version": 1}))
            zf.writestr("lockbot.db", db_bytes)
        zip_bytes = buf.getvalue()

        _TestSession = _get_test_session()
        with _TestSession() as session:
            from lockbot.backend.app.auth.models import User

            admin = session.query(User).filter(User.username == "adminuser").first()
            admin_id = admin.id

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            result = restore_apply(
                file_bytes=zip_bytes,
                password=None,
                source_secret_key=source_key,
                selected_uuids=[bot_uuid],
                current_user_id=admin_id,
                db=db_session,
            )

        assert result["created"] == 1
        assert result["errors"] == []

        new_bot = db_session.query(Bot).filter(Bot.uuid == bot_uuid).first()
        assert decrypt(new_bot.webhook_url) == "https://foreign.webhook.com"
        assert decrypt(new_bot.aes_key) == "foreign-aes"
        assert decrypt(new_bot.token) == "foreign-token"


# ===========================================================================
# REST endpoint tests for POST /admin/backup/restore/apply
# ===========================================================================


class TestRestoreApplyEndpoint:
    """Test the /api/admin/backup/restore/apply HTTP endpoint."""

    def test_apply_endpoint_creates_bot(self, client, admin_header, super_admin_header, tmp_path):
        """POST /admin/backup/restore/apply creates a new bot and returns counts."""
        bot_uuid = "endpoint-create-uuid"
        zip_bytes = _build_backup_zip(bot_uuid=bot_uuid, bot_name="endpoint-bot")

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            resp = client.post(
                "/api/admin/backup/restore/apply",
                files={"file": ("backup.zip", zip_bytes, "application/zip")},
                data={"selected_uuids": json.dumps([bot_uuid])},
                headers=super_admin_header,
            )

        assert resp.status_code == 200
        result = resp.json()
        assert result["created"] == 1
        assert result["overwritten"] == 0
        assert result["restored"] == 1

    def test_apply_endpoint_overwrites_existing(self, client, admin_header, super_admin_header, tmp_path):
        """POST /admin/backup/restore/apply overwrites when uuid matches."""
        # Create a bot first
        create_resp = client.post(
            "/api/bots",
            json={
                "name": "to-overwrite",
                "bot_type": "NODE",
                "webhook_url": "https://example.com/hook",
                "aes_key": "key1",
                "token": "tok1",
                "cluster_configs": ["n1"],
            },
            headers=admin_header,
        )
        existing_uuid = create_resp.json()["uuid"]

        zip_bytes = _build_backup_zip(bot_uuid=existing_uuid, bot_name="overwritten-name")

        with patch("lockbot.backend.app.backup.service.DATA_DIR", str(tmp_path)):
            resp = client.post(
                "/api/admin/backup/restore/apply",
                files={"file": ("backup.zip", zip_bytes, "application/zip")},
                data={"selected_uuids": json.dumps([existing_uuid])},
                headers=super_admin_header,
            )

        assert resp.status_code == 200
        result = resp.json()
        assert result["overwritten"] == 1
        assert result["created"] == 0

    def test_apply_endpoint_requires_super_admin(self, client, admin_header, tmp_path):
        """Regular admin cannot call restore/apply (requires super_admin)."""
        zip_bytes = _build_backup_zip()
        resp = client.post(
            "/api/admin/backup/restore/apply",
            files={"file": ("backup.zip", zip_bytes, "application/zip")},
            data={"selected_uuids": json.dumps(["any-uuid"])},
            headers=admin_header,
        )
        assert resp.status_code == 403

    def test_apply_endpoint_invalid_selected_uuids(self, client, super_admin_header):
        """Invalid selected_uuids format returns 422."""
        zip_bytes = _build_backup_zip()
        resp = client.post(
            "/api/admin/backup/restore/apply",
            files={"file": ("backup.zip", zip_bytes, "application/zip")},
            data={"selected_uuids": "not-valid-json["},
            headers=super_admin_header,
        )
        assert resp.status_code == 422

    def test_apply_endpoint_empty_file(self, client, super_admin_header):
        """Empty file returns 400."""
        resp = client.post(
            "/api/admin/backup/restore/apply",
            files={"file": ("backup.zip", b"", "application/zip")},
            data={"selected_uuids": json.dumps(["uuid1"])},
            headers=super_admin_header,
        )
        assert resp.status_code == 400
