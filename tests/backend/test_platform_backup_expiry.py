"""Backup ZIP -> typed evidence -> restored registry must never erase expiry."""

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.backup.service import _read_backup_bots
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.platform_applications.service import (
    CredentialUnavailable,
    PlatformApplication,
    credentials_for,
)

from backend.test_backup_restore import _build_backup_zip_with_encrypted_creds


def archive(tmp_path, provider, expiry):
    identity = "expiry-roundtrip-bot"
    data = _build_backup_zip_with_encrypted_creds(
        bot_uuid=identity, webhook="https://example.invalid/source", aes_key="FAKE_AES", token="FAKE_TOKEN"
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        contents = {name: zipped.read(name) for name in zipped.namelist()}
    path = tmp_path / "expiry-source.db"
    path.write_bytes(contents["lockbot.db"])
    connection = sqlite3.connect(path)
    if provider == "feishu":
        values = {
            "provider": "feishu",
            "app_id": "cli_expiry",
            "app_secret": "FAKE_SOURCE_SECRET",
            "verification_token": "",
            "encrypt_key": "",
        }
        connection.execute(
            "UPDATE bots SET platform='Feishu', group_id='oc_expiry', webhook_url='',aes_key='',token=''"
        )
        connection.execute(
            "CREATE TABLE feishu_applications (id INTEGER, app_id TEXT, name TEXT, tenant_key TEXT, "
            "bot_open_id TEXT, app_secret TEXT, verification_token TEXT, encrypt_key TEXT, transport TEXT)"
        )
        connection.execute(
            "INSERT INTO feishu_applications VALUES("
            "900,'cli_expiry','Source','tenant','ou_expiry',?,'','','websocket')",
            (encryption.encrypt(values["app_secret"]),),
        )
        connection.execute("CREATE TABLE feishu_group_routes(application_id INTEGER, bot_id INTEGER, chat_id TEXT)")
        connection.execute("INSERT INTO feishu_group_routes VALUES(900,1,'oc_expiry')")
        stable = "cli_expiry"
    else:
        values = {
            "provider": "infoflow",
            "webhook_url": "https://example.invalid/source",
            "token": "FAKE_TOKEN",
            "aes_key": "FAKE_AES",
        }
        stable = identity
    connection.execute(
        "CREATE TABLE platform_applications(provider TEXT, stable_identity TEXT, schema_version INTEGER, "
        "encrypted_payload TEXT, expires_at TEXT)"
    )
    connection.execute(
        "INSERT INTO platform_applications VALUES(?,?,1,?,?)",
        (provider, stable, encryption.encrypt(json.dumps(values)), expiry),
    )
    connection.commit()
    connection.close()
    contents["lockbot.db"] = path.read_bytes()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in contents.items():
            zipped.writestr(name, content)
    return identity, output.getvalue()


@pytest.fixture
def admin(client, auth_header, db_session):
    db_session.query(User).filter_by(username="testuser").one().role = "super_admin"
    db_session.commit()
    return auth_header


def apply(client, header, identity, data):
    return client.post(
        "/api/admin/backup/restore/apply",
        headers=header,
        files={"file": ("backup.zip", data, "application/zip")},
        data={"selected_uuids": json.dumps([identity])},
    )


def envelope(provider, data):
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        bot = _read_backup_bots(zipped)[0]
    if provider == "feishu":
        return bot["_feishu_application"]["_versioned_credentials"]
    return bot["_versioned_credentials"]


@pytest.mark.parametrize("provider", ["infoflow", "feishu"])
@pytest.mark.parametrize("expiry", ["2000-01-01 00:00:00.000000", "garbage", "", "2028-99-50T77:00:00"])
def test_expired_or_invalid_archive_cannot_restore_new_target(client, admin, db_session, tmp_path, provider, expiry):
    identity, data = archive(tmp_path, provider, expiry)
    assert envelope(provider, data)["expires_at"] == expiry
    response = apply(client, admin, identity, data)
    assert response.status_code == 400 or (response.status_code == 200 and response.json()["restored"] == 0)
    assert db_session.query(Bot).count() == db_session.query(FeishuApplication).count() == 0
    assert db_session.query(FeishuGroupRoute).count() == db_session.query(PlatformApplication).count() == 0


@pytest.mark.parametrize("provider", ["infoflow", "feishu"])
def test_valid_future_expiry_roundtrips_without_becoming_unlimited(client, admin, db_session, tmp_path, provider):
    deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    identity, data = archive(tmp_path, provider, deadline.isoformat())
    response = apply(client, admin, identity, data)
    assert response.status_code == 200 and response.json()["restored"] == 1, response.text
    row = db_session.query(PlatformApplication).one()
    assert row.expires_at == deadline
    source = db_session.query(FeishuApplication if provider == "feishu" else Bot).one()
    assert credentials_for(source)
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(CredentialUnavailable, match="expired"):
        credentials_for(source)


@pytest.mark.parametrize("target_expired,source_invalid", [(False, False), (True, False), (False, True)])
def test_feishu_selects_current_target_and_preserves_its_expiry(
    client, admin, db_session, tmp_path, target_expired, source_invalid
):
    current = FeishuApplication(
        app_id="cli_expiry",
        name="Current",
        tenant_key="tenant",
        bot_open_id="ou_expiry",
        app_secret=encryption.encrypt("FAKE_CURRENT_SECRET"),
        verification_token="",
        encrypt_key="",
        transport="websocket",
    )
    db_session.add(current)
    db_session.commit()
    row = db_session.query(PlatformApplication).one()
    current_deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=-1 if target_expired else 3)
    row.expires_at = current_deadline
    db_session.commit()
    source_deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2 if target_expired else -2)
    identity, data = archive(tmp_path, "feishu", "invalid" if source_invalid else source_deadline.isoformat())
    response = apply(client, admin, identity, data)
    assert response.status_code == 200
    assert response.json()["restored"] == (0 if target_expired or source_invalid else 1), response.text
    db_session.refresh(current)
    db_session.refresh(row)
    assert current.name == "Current" and encryption.decrypt(current.app_secret) == "FAKE_CURRENT_SECRET"
    assert row.expires_at == current_deadline
    if target_expired or source_invalid:
        assert db_session.query(Bot).count() == db_session.query(FeishuGroupRoute).count() == 0
    else:
        assert db_session.query(FeishuGroupRoute).one().application_id == current.id != 900


def test_infoflow_does_not_reuse_current_credentials_to_accept_expired_source(client, admin, db_session, tmp_path):
    identity, data = archive(tmp_path, "infoflow", "2000-01-01 00:00:00")
    user = db_session.query(User).filter_by(username="testuser").one()
    current = Bot(
        uuid=identity,
        user_id=user.id,
        name="Current",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url=encryption.encrypt("https://example.invalid/current"),
        aes_key=encryption.encrypt("CURRENT_AES"),
        token=encryption.encrypt("CURRENT_TOKEN"),
        cluster_configs='["node1"]',
        config_overrides="{}",
        status="stopped",
    )
    db_session.add(current)
    db_session.commit()
    row = db_session.query(PlatformApplication).one()
    deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
    row.expires_at = deadline
    db_session.commit()
    response = apply(client, admin, identity, data)
    assert response.status_code == 400
    db_session.refresh(current)
    db_session.refresh(row)
    assert current.name == "Current" and encryption.decrypt(current.token) == "CURRENT_TOKEN"
    assert row.expires_at == deadline


@pytest.mark.parametrize("value", [True, 42, "2030-01-01", "2030-01-01T00:00:00+99:00", "missing"])
def test_expiry_evidence_is_strict_and_cannot_default_to_unlimited(value):
    from lockbot.backend.app.platform_applications.backup import archived_expiry

    evidence = {} if value == "missing" else {"expires_at": value}
    with pytest.raises(ValueError, match="expiry"):
        archived_expiry(evidence)


@pytest.mark.parametrize("expired", [False, True])
def test_legacy_infoflow_archive_cannot_relax_known_target_expiry(client, admin, db_session, expired):
    identity = "legacy-expiry-target"
    user = db_session.query(User).filter_by(username="testuser").one()
    source = Bot(
        uuid=identity,
        user_id=user.id,
        name="Current",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url=encryption.encrypt("https://example.invalid/current"),
        aes_key="",
        token="",
        cluster_configs='["node1"]',
        config_overrides="{}",
        status="stopped",
    )
    db_session.add(source)
    db_session.commit()
    row = db_session.query(PlatformApplication).one()
    deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=-1 if expired else 2)
    row.expires_at = deadline
    db_session.commit()
    data = _build_backup_zip_with_encrypted_creds(bot_uuid=identity)
    response = apply(client, admin, identity, data)
    assert response.status_code == 200
    assert response.json()["restored"] == (0 if expired else 1), response.text
    db_session.refresh(row)
    assert row.expires_at == deadline
