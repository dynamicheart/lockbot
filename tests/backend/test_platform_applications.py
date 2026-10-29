"""Typed provider envelopes never change resource or API credential identity."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication
from lockbot.backend.app.platform_applications import service
from lockbot.backend.app.platform_applications.service import CredentialUnavailable, PlatformApplication
from sqlalchemy import update
from sqlalchemy.orm.exc import StaleDataError

from backend.test_bots import _sample_bot


@pytest.fixture
def sources(client, auth_header, db_session):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    bot_id = client.post("/api/bots", headers=auth_header, json=_sample_bot("provider-fixture")).json()["id"]
    app = FeishuApplication(
        app_id="cli_providerfixture",
        name="Application",
        tenant_key="tenant",
        bot_open_id="ou_fixture",
        transport="websocket",
        app_secret=encryption.encrypt("FAKE_APP_SECRET"),
        verification_token="",
        encrypt_key="",
    )
    db_session.add(app)
    db_session.commit()
    return bot_id, app.id, user.id


def test_encrypted_typed_payloads_preserve_legacy_fields_and_ids(db_session, sources):
    bot = db_session.get(Bot, sources[0])
    app = db_session.get(FeishuApplication, sources[1])
    records = db_session.query(PlatformApplication).all()
    assert len(records) == 2
    by_provider = {row.provider: row for row in records}
    assert by_provider["infoflow"].stable_identity == bot.uuid
    assert by_provider["feishu"].stable_identity == app.app_id
    assert "FAKE_APP_SECRET" not in by_provider["feishu"].encrypted_payload
    assert service.credentials_for(app)["app_secret"] == encryption.decrypt(app.app_secret)
    assert service.credentials_for(bot)["token"] == encryption.decrypt(bot.token)


def test_upgrade_backfill_is_restartable_and_never_rewrites_legacy(db_session, sources, session_factory):
    before = [(row.id, row.uuid, row.webhook_url, row.token, row.aes_key) for row in db_session.query(Bot)]
    db_session.query(PlatformApplication).delete()
    db_session.commit()

    def interrupted(table, identifier):
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        service.backfill(session_factory, after=interrupted)
    assert db_session.query(PlatformApplication).count() == 1
    assert service.backfill(session_factory) == 1
    assert service.backfill(session_factory) == 0
    assert before == [(row.id, row.uuid, row.webhook_url, row.token, row.aes_key) for row in db_session.query(Bot)]


@pytest.mark.parametrize(
    "failure", ["unknown_version", "unknown_provider", "corrupt", "wrong_type", "extra", "expired"]
)
def test_known_envelope_never_falls_back_to_legacy(db_session, sources, failure):
    app = db_session.get(FeishuApplication, sources[1])
    row = db_session.query(PlatformApplication).filter_by(provider="feishu").one()
    if failure == "unknown_version":
        row.schema_version = 99
    elif failure == "unknown_provider":
        row.encrypted_payload = encryption.encrypt('{"provider":"unrecognized"}')
    elif failure == "corrupt":
        row.encrypted_payload = "invalid-ciphertext"
    elif failure == "wrong_type":
        values = service.credentials_for(app)
        values["app_secret"] = {"nested": "FAKE_APP_SECRET"}
        row.encrypted_payload = encryption.encrypt(json.dumps(values))
    elif failure == "extra":
        values = service.credentials_for(app)
        values["arbitrary"] = "FAKE_APP_SECRET"
        row.encrypted_payload = encryption.encrypt(json.dumps(values))
    else:
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(CredentialUnavailable) as error:
        service.credentials_for(app)
    assert "FAKE_APP_SECRET" not in str(error.value)
    assert encryption.decrypt(app.app_secret) == "FAKE_APP_SECRET"


def test_missing_envelope_uses_validated_legacy_during_interrupted_migration(db_session, sources):
    row = db_session.query(PlatformApplication).filter_by(provider="feishu").one()
    db_session.delete(row)
    db_session.commit()
    app = db_session.get(FeishuApplication, sources[1])
    assert service.credentials_for(app)["app_secret"] == "FAKE_APP_SECRET"


def test_legacy_rotation_is_atomic_and_preserves_application_identity(db_session, sources, monkeypatch):
    app = db_session.get(FeishuApplication, sources[1])
    row = db_session.query(PlatformApplication).filter_by(provider="feishu").one()
    identity, old_generation = app.app_id, row.generation
    app.app_secret = encryption.encrypt("FAKE_ROTATED_SECRET")
    db_session.commit()
    assert row.generation > old_generation and app.app_id == identity
    assert service.credentials_for(app)["app_secret"] == "FAKE_ROTATED_SECRET"
    before = app.app_secret, row.encrypted_payload
    replacement = encryption.encrypt("FAKE_FAILED_SECRET")
    with monkeypatch.context() as patched:
        patched.setattr(service.encryption, "encrypt", lambda value: (_ for _ in ()).throw(RuntimeError("fixture")))
        app.app_secret = replacement
        with pytest.raises(RuntimeError):
            db_session.commit()
    db_session.rollback()
    assert (app.app_secret, row.encrypted_payload) == before


def test_sql_legacy_write_is_rejected_until_explicit_upgrade_reconciliation(db_session, sources, session_factory):
    db_session.execute(
        update(FeishuApplication)
        .where(FeishuApplication.id == sources[1])
        .values(app_secret=encryption.encrypt("FAKE_OLD_VERSION_WRITE"))
    )
    db_session.commit()
    db_session.expire_all()
    with pytest.raises(CredentialUnavailable, match="disagree"):
        service.credentials_for(db_session.get(FeishuApplication, sources[1]))
    service.backfill(session_factory)
    db_session.expire_all()
    assert (
        service.credentials_for(db_session.get(FeishuApplication, sources[1]))["app_secret"] == "FAKE_OLD_VERSION_WRITE"
    )


def test_optimistic_generation_rejects_overlapping_application_edits(db_session, sources, session_factory):
    identifier = db_session.query(PlatformApplication).filter_by(provider="feishu").one().id
    with session_factory() as first, session_factory() as second:
        one, two = first.get(PlatformApplication, identifier), second.get(PlatformApplication, identifier)
        one.name, two.name = "First", "Second"
        first.commit()
        with pytest.raises(StaleDataError):
            second.commit()
        second.rollback()


def test_superadmin_metadata_reveal_and_reference_delete_guard(client, auth_header, db_session, sources):
    path = "/api/admin/platform-applications"
    listing = client.get(path, headers=auth_header)
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert "FAKE_APP_SECRET" not in listing.text and "encrypted_payload" not in listing.text
    record = next(item for item in listing.json()["items"] if item["provider"] == "feishu")
    assert record["provider_management_path"] == f"/api/feishu/applications/{sources[1]}"
    response = client.post(f"{path}/{record['id']}/reveal", headers=auth_header)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["credentials"]["app_secret"] == "FAKE_APP_SECRET"
    assert client.delete(f"{path}/{record['id']}", headers=auth_header).status_code == 409
    audit = db_session.query(AuditLog).filter_by(action="platform_application.reveal").one()
    assert "SECRET" not in str(audit.__dict__)
    result = client.patch(
        f"{path}/{record['id']}",
        headers=auth_header,
        json={"name": "Registry label", "expected_generation": record["generation"]},
    )
    assert result.status_code == 200
    assert (
        client.patch(
            f"{path}/{record['id']}",
            headers=auth_header,
            json={"name": "Stale", "expected_generation": record["generation"]},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("role", ["user", "admin"])
def test_non_superadmin_cannot_read_or_manage_registry(client, auth_header, db_session, sources, role):
    identifier = db_session.query(PlatformApplication).first().id
    db_session.get(User, sources[2]).role = role
    db_session.commit()
    path = f"/api/admin/platform-applications/{identifier}"
    assert client.get("/api/admin/platform-applications", headers=auth_header).status_code == 403
    assert client.post(path + "/reveal", headers=auth_header).status_code == 403
    assert client.patch(path, headers=auth_header, json={"name": "x", "expected_generation": 1}).status_code == 403
    assert client.delete(path, headers=auth_header).status_code == 403


def test_startup_skips_bad_envelope_without_unblocking_it(db_session, sources, session_factory, caplog):
    row = db_session.query(PlatformApplication).filter_by(provider="feishu").one()
    row.schema_version = 999
    db_session.commit()
    assert service.backfill(session_factory, strict=False) == 0
    assert "administrator repair" in caplog.text and "FAKE_APP_SECRET" not in caplog.text
    with pytest.raises(CredentialUnavailable):
        service.credentials_for(db_session.get(FeishuApplication, sources[1]))


def test_provider_column_corruption_cannot_create_legacy_fallback(db_session, sources):
    row = db_session.query(PlatformApplication).filter_by(provider="feishu").one()
    row.provider = "future"
    db_session.commit()
    with pytest.raises(CredentialUnavailable, match="provider mismatch"):
        service.credentials_for(db_session.get(FeishuApplication, sources[1]))


def test_detached_application_cannot_bypass_current_registry(db_session, sources):
    app = db_session.get(FeishuApplication, sources[1])
    db_session.expunge(app)
    with pytest.raises(CredentialUnavailable, match="current database session"):
        service.credentials_for(app)


def test_reveal_rechecks_role_and_audit_failure_returns_no_secret(
    client, auth_header, db_session, sources, monkeypatch
):
    from lockbot.backend.app.platform_applications import router

    identifier = db_session.query(PlatformApplication).filter_by(provider="feishu").one().id
    original = router._audit

    def demote(db, user, *args):
        original(db, user, *args)
        user.role = "user"

    monkeypatch.setattr(router, "_audit", demote)
    result = client.post(f"/api/admin/platform-applications/{identifier}/reveal", headers=auth_header)
    assert result.status_code == 403 and "FAKE_APP_SECRET" not in result.text


def test_registry_reveal_requires_durable_audit(client, auth_header, db_session, sources, monkeypatch):
    from lockbot.backend.app.platform_applications import router

    identifier = db_session.query(PlatformApplication).filter_by(provider="feishu").one().id
    monkeypatch.setattr(router, "_audit", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture-audit")))
    with pytest.raises(RuntimeError, match="fixture-audit"):
        client.post(f"/api/admin/platform-applications/{identifier}/reveal", headers=auth_header)


@pytest.mark.parametrize("provider", ["infoflow", "feishu"])
def test_backup_envelope_validation_is_versioned_and_provider_typed(provider):
    from lockbot.backend.app.platform_applications.backup import validate_archived

    expected = (
        {"provider": "infoflow", "webhook_url": "https://example.invalid/hook", "token": "FAKE_TOKEN", "aes_key": ""}
        if provider == "infoflow"
        else {
            "provider": "feishu",
            "app_id": "cli_fixture",
            "app_secret": "FAKE_SECRET",
            "verification_token": "",
            "encrypt_key": "",
        }
    )
    envelope = {
        "provider": provider,
        "schema_version": 1,
        "encrypted_payload": encryption.encrypt(json.dumps(expected)),
    }
    validate_archived(provider, envelope, None, expected)
    with pytest.raises(ValueError, match="schema"):
        validate_archived(provider, {**envelope, "schema_version": 2}, None, expected)
    with pytest.raises(ValueError, match="decrypt"):
        validate_archived(provider, {**envelope, "encrypted_payload": "invalid"}, None, expected)
    with pytest.raises(ValueError, match="disagree"):
        validate_archived(provider, envelope, None, {**expected, "new": "mismatch"})


@pytest.mark.parametrize("change", ["role", "version"])
@pytest.mark.parametrize("operation", ["create", "rotate"])
def test_feishu_external_verification_rechecks_admin(
    client, auth_header, db_session, sources, monkeypatch, change, operation
):
    from lockbot.core.platforms.feishu import FeishuClient

    def verify(client):
        user = db_session.get(User, sources[2])
        if change == "role":
            user.role = "user"
        else:
            user.token_version += 1
        db_session.commit()
        return "ou_fixture"

    monkeypatch.setattr(FeishuClient, "bot_identity", verify)
    if operation == "create":
        response = client.post(
            "/api/feishu/applications",
            headers=auth_header,
            json={"name": "New", "app_id": "cli_new", "app_secret": "FAKE_NEW_SECRET", "transport": "websocket"},
        )
        assert db_session.query(FeishuApplication).count() == 1
    else:
        response = client.patch(
            f"/api/feishu/applications/{sources[1]}",
            headers=auth_header,
            json={"name": "Unchanged", "app_secret": "FAKE_NEW_SECRET"},
        )
        db_session.expire_all()
        assert encryption.decrypt(db_session.get(FeishuApplication, sources[1]).app_secret) == "FAKE_APP_SECRET"
    assert response.status_code == 403 and "FAKE_NEW_SECRET" not in response.text


def test_infoflow_display_name_survives_restart_backfill(client, auth_header, db_session, sources, session_factory):
    row = db_session.query(PlatformApplication).filter_by(provider="infoflow").one()
    response = client.patch(
        f"/api/admin/platform-applications/{row.id}",
        headers=auth_header,
        json={"name": "Independent app label", "expected_generation": row.generation},
    )
    assert response.status_code == 200
    service.backfill(session_factory)
    db_session.refresh(row)
    assert row.name == "Independent app label"
    assert db_session.get(Bot, sources[0]).name == "provider-fixture"


def test_source_recreation_serializes_against_orphan_deletion(db_session, sources, session_factory):
    app = db_session.get(FeishuApplication, sources[1])
    fields = {
        key: getattr(app, key)
        for key in (
            "app_id",
            "name",
            "tenant_key",
            "bot_open_id",
            "app_secret",
            "verification_token",
            "encrypt_key",
            "transport",
        )
    }
    identifier = db_session.query(PlatformApplication).filter_by(provider="feishu").one().id
    db_session.query(FeishuApplication).filter_by(id=app.id).delete()
    db_session.commit()
    with session_factory() as deleting, session_factory() as recreating:
        old = deleting.get(PlatformApplication, identifier)
        recreating.add(FeishuApplication(**fields))
        recreating.commit()
        deleting.delete(old)
        with pytest.raises(StaleDataError):
            deleting.commit()
        deleting.rollback()
    assert db_session.get(PlatformApplication, identifier) is not None
