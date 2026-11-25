import hashlib
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_api.v2 import binding, identity_tickets
from lockbot.backend.app.resource_auth import credentials

ROUTE = "/api/agent/v2/identity-tickets"
AUDIENCE = "example-reports"
SUBJECT = "example-principal"
KEY = "lbv2_example-personal-key"


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "signing.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key_path.chmod(0o600)
    settings = tmp_path / "identity-tickets.json"
    settings.write_text(json.dumps({"version": 1, "key_file": str(key_path), "audiences": [AUDIENCE]}))
    settings.chmod(0o600)
    monkeypatch.setenv(identity_tickets.CONFIG_ENV, str(settings))
    return key, key_path, settings


@pytest.fixture()
def observed_at(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(identity_tickets, "_utcnow", lambda: now)
    monkeypatch.setattr(binding.service, "_utcnow", lambda: now)
    return now


@pytest.fixture()
def principal(db_session, observed_at):
    row = binding.PersonalCredential(
        public_id=SUBJECT,
        namespace="example",
        platform="example",
        external_user_id="private-external-user-id",
        credential_digest=credentials.digest_credential(KEY),
        credential_encrypted="example",
    )
    db_session.add(row)
    db_session.commit()
    return row


def issue(client, *, key=KEY, audience=AUDIENCE, **extra):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return client.post(ROUTE, headers=headers, json={"audience": audience, **extra})


def test_identity_ticket_claims_and_signature(client, db_session, principal, deployment, observed_at):
    key, _, _ = deployment
    issuer = ensure_instance_id(db_session)
    response = issue(client).raise_for_status()
    body = response.json()
    ticket = body["ticket"]
    assert response.headers["cache-control"] == "no-store"
    assert body["token_type"] == "Bearer"
    assert body["expires_at"] == int(observed_at.timestamp()) + 120

    public = key.public_key()
    kid = hashlib.sha256(public.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()[:16]
    assert jwt.get_unverified_header(ticket) == {"alg": "EdDSA", "typ": "lockbot-identity+jwt", "kid": kid}
    verify = {
        "algorithms": ["EdDSA"],
        "audience": AUDIENCE,
        "issuer": issuer,
        "options": {"require": ["iss", "sub", "aud", "iat", "exp"], "strict_aud": True},
    }
    assert jwt.decode(ticket, public, **verify) == {
        "iss": issuer,
        "sub": SUBJECT,
        "aud": AUDIENCE,
        "iat": int(observed_at.timestamp()),
        "exp": body["expires_at"],
    }
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(ticket, public, **{**verify, "audience": "another-service"})
    with pytest.raises(jwt.InvalidIssuerError):
        jwt.decode(ticket, public, **{**verify, "issuer": "another-instance"})
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(ticket, Ed25519PrivateKey.generate().public_key(), **verify)


def test_identity_ticket_expires_with_native_key(client, db_session, principal, deployment, observed_at):
    principal.credential_expires_at = (observed_at + timedelta(seconds=24)).replace(tzinfo=None)
    db_session.commit()
    body = issue(client).raise_for_status().json()
    assert body["expires_at"] == int(observed_at.timestamp()) + 24
    principal.credential_expires_at = observed_at.replace(tzinfo=None)
    db_session.commit()
    assert issue(client).status_code == 401


def test_identity_ticket_rejects_untrusted_requests(client, principal, deployment):
    assert issue(client, key=None).status_code == 401
    assert issue(client, key="lbv2_other-key").status_code == 401
    assert issue(client, audience="unknown-reports").status_code == 403
    assert issue(client, audience="invalid audience").status_code == 422
    assert issue(client, sub="another-person").status_code == 422


def test_identity_ticket_fails_closed_on_deployment_changes(client, principal, deployment, monkeypatch):
    _, key_path, settings = deployment
    settings.chmod(0o644)
    assert issue(client).status_code == 503
    settings.chmod(0o600)
    key_path.chmod(0o644)
    assert issue(client).status_code == 503
    key_path.chmod(0o600)
    issue(client).raise_for_status()
    settings.write_text(json.dumps({"version": 1, "key_file": str(key_path), "audiences": ["another-service"]}))
    assert issue(client).status_code == 403
    monkeypatch.delenv(identity_tickets.CONFIG_ENV)
    assert issue(client).status_code == 404
