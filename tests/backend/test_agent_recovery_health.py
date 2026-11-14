"""Recovery health never changes the authority of an existing personal key."""

# ruff: noqa: F811
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.discovery import _backfill_presented_key
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_agent_binding_directory import second_binding
from backend.test_resource_api_resources import api  # noqa: F401


def source(db):
    return db.query(ResourceBinding).filter_by(external_user_id="alice").order_by(ResourceBinding.id).first()


@pytest.mark.parametrize("copy", ["healthy", None, "", "corrupt", "swapped"])
def test_context_health_is_caller_only_and_does_not_backfill(client, api, db_session, copy):
    _, issued = second_binding(client, api)
    row = source(db_session)
    if copy != "healthy":
        row.credential_encrypted = (
            credentials.seal_credential(issued["binding_id"], issued["credential"]) if copy == "swapped" else copy
        )
    before = row.credential_encrypted
    db_session.commit()
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    expected = "recoverable" if copy == "healthy" else ("legacy_missing" if copy is None else "unreadable")
    assert response.json()["credential_recovery_health"] == expected
    assert "credential_recovery_health" in response.json()["server"]["capabilities"]
    assert "bindings" not in response.json()
    assert issued["binding_id"] not in response.text and issued["credential"] not in response.text
    assert api["headers"]["Authorization"][7:] not in response.text
    db_session.refresh(row)
    assert row.credential_encrypted == before
    # Another caller's healthy state is independent of Alice's missing/bad copy.
    assert client.get("/api/agent/context", headers=api["other_headers"]).json()["credential_recovery_health"] == (
        "recoverable"
    )


@pytest.mark.parametrize("copy", [None, "corrupt"])
def test_backfill_preserves_exact_key_scope_and_healthy_ciphertext(client, api, db_session, copy):
    row = source(db_session)
    row.credential_encrypted = copy
    db_session.commit()
    before = (row.public_id, row.credential_digest, row.bound_bot_uuid, row.bound_identity_revision, row.revoked_at)
    key = api["headers"]["Authorization"][7:]
    assert client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"]).status_code == 200
    result = client.post("/api/agent/bindings/recover", headers=api["headers"])
    assert result.status_code == 200
    current = next(item for item in result.json()["bindings"] if item["current"])
    assert current["credential"] == key and current["credential_recoverable"] is True
    db_session.refresh(row)
    ciphertext = row.credential_encrypted
    assert credentials.recover_credential(row) == key
    assert (
        row.public_id,
        row.credential_digest,
        row.bound_bot_uuid,
        row.bound_identity_revision,
        row.revoked_at,
    ) == before
    assert (
        client.get("/api/agent/context", headers=api["headers"]).json()["credential_recovery_health"] == "recoverable"
    )
    assert client.post("/api/agent/bindings/recover", headers=api["headers"]).status_code == 200
    db_session.refresh(row)
    assert row.credential_encrypted == ciphertext
    assert client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"]).status_code == 200


@pytest.mark.parametrize("legacy", [False, True])
def test_unavailable_encryption_has_explicit_failure_and_keeps_valid_key(client, api, db_session, monkeypatch, legacy):
    from lockbot.backend.app.bots import encryption

    row = source(db_session)
    if legacy:
        row.credential_encrypted = None
        db_session.commit()
    original = row.credential_encrypted
    digest = row.credential_digest

    def unavailable(*args):
        raise RuntimeError("private encryption failure")

    monkeypatch.setattr(encryption, "decrypt", unavailable)
    expected = "legacy_missing" if legacy else "unreadable"
    assert client.get("/api/agent/context", headers=api["headers"]).json()["credential_recovery_health"] == expected
    monkeypatch.setattr(encryption, "encrypt", unavailable)
    response = client.post("/api/agent/bindings/recover", headers=api["headers"])
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "credential_recovery_unavailable"}}
    assert "private encryption failure" not in response.text
    assert client.get("/api/agent/context", headers=api["headers"]).status_code == 200
    db_session.refresh(row)
    assert row.credential_digest == digest and row.credential_encrypted == original


def test_interleaved_backfill_keeps_first_committed_copy(client, api, db_session, session_factory, monkeypatch):
    row = source(db_session)
    row.credential_encrypted = None
    db_session.commit()
    original = credentials.seal_credential
    sealed_by_competitor = []

    def competing_backfill(binding_id, token):
        if not sealed_by_competitor:
            # Interleave another transaction at the exact encrypt-before-update
            # window, without relying on thread scheduling or shared SQLite IO.
            with session_factory() as other:
                competitor = other.query(ResourceBinding).filter_by(public_id=binding_id).one()
                monkeypatch.setattr(credentials, "seal_credential", original)
                _backfill_presented_key(other, competitor, token)
                other.commit()
                sealed_by_competitor.append(other.get(ResourceBinding, row.id).credential_encrypted)
        return original(binding_id, token)

    monkeypatch.setattr(credentials, "seal_credential", competing_backfill)
    assert client.post("/api/agent/bindings/recover", headers=api["headers"]).status_code == 200
    db_session.refresh(row)
    assert row.credential_encrypted == sealed_by_competitor[0]
    assert credentials.recover_credential(row) == api["headers"]["Authorization"][7:]


@pytest.mark.parametrize("change", ["revoked", "expired", "credential", "subject", "uuid", "revision", "deleted"])
def test_backfill_cannot_write_or_release_key_after_authority_changes(client, api, db_session, monkeypatch, change):
    row = source(db_session)
    row.credential_encrypted = None
    db_session.commit()
    original = credentials.seal_credential

    def change_before_update(binding_id, token):
        victim = source(db_session)
        bot = db_session.get(Bot, victim.bound_bot_id)
        if change == "revoked":
            victim.revoked_at = datetime.utcnow()
        elif change == "expired":
            victim.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
        elif change == "credential":
            victim.credential_digest = credentials.digest_credential(credentials.generate_credential())
        elif change == "subject":
            victim.external_user_id = "replacement-subject"
        elif change == "uuid":
            bot.uuid = "replacement-bot"
        elif change == "revision":
            bot.identity_revision += 1
        else:
            bot.is_deleted = True
        db_session.commit()
        return original(binding_id, token)

    monkeypatch.setattr(credentials, "seal_credential", change_before_update)
    response = client.post("/api/agent/bindings/recover", headers=api["headers"])
    assert response.status_code == 401
    assert api["headers"]["Authorization"][7:] not in response.text
    db_session.refresh(row)
    assert row.credential_encrypted is None
