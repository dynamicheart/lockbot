"""Only current browser-proven native bindings can supply secret-free handoffs."""

# ruff: noqa: F811
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.personal_panel.native import NativeProvider
from lockbot.backend.app.personal_panel.router import create_panel
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_agent_binding_directory import second_binding
from backend.test_personal_panel_http import Directory
from backend.test_resource_api_resources import api  # noqa: F401

ORIGIN = "https://panel.example.test"
PREFIX = "/api/personal-panel"


def login(agent, browser, key, mode="replace"):
    issued = agent.post(PREFIX + "/links", headers={"Authorization": "Bearer " + key})
    assert issued.status_code == 200, issued.text
    ticket = parse_qs(urlsplit(issued.json()["login_url"]).fragment.split("?", 1)[1])["ticket"][0]
    response = browser.post(PREFIX + "/login", json={"ticket": ticket, "mode": mode}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text


@pytest.fixture
def env(api, db_session, session_factory, tmp_path):
    ensure_instance_id(db_session)
    provider = NativeProvider({"issuer": "logical-issuer-not-a-deployment-url", "session_factory": session_factory})
    path = tmp_path / "panel.db"
    parent = FastAPI()
    parent.mount(PREFIX, create_panel(ORIGIN, path, provider))
    agent, browser = TestClient(parent, base_url=ORIGIN), TestClient(parent, base_url=ORIGIN)
    key = api["headers"]["Authorization"][7:]
    login(agent, browser, key)
    return provider, agent, browser, path


def test_current_binding_metadata_uses_approved_deployment_and_never_contains_keys(env, api, db_session):
    _, agent, browser, _ = env
    caps = browser.get(PREFIX + "/capabilities").json()["connection_management"]
    assert caps["version"] == 1 and caps["credential_disclosure_supported"] is False
    assert caps["refresh_semantics"] == "revalidate_only"
    assert caps["skill"]["bundle_url"] == ORIGIN + "/api/agent-skill/lockbot-resource-agent.zip"
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 200, response.text
    data = response.json()
    item = data["connections"][0]
    assert item["bot"]["id"] == api["bot_id"]
    assert item["actor"] == {"platform": "infoflow", "external_user_id": "alice"}
    assert item["identity"]["issuer"] == "logical-issuer-not-a-deployment-url"
    assert item["service_origin"] == ORIGIN
    assert item["credential_recovery_health"] == "recoverable"
    assert ORIGIN + "/api/agent-skill/SKILL.md" in item["instructions"]
    assert item["bot"]["name"] in item["instructions"] and item["bot"]["uuid"] in item["instructions"]
    assert "logical-issuer-not-a-deployment-url" not in item["instructions"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    for secret in (api["headers"]["Authorization"][7:], row.credential_digest, row.credential_encrypted, row.public_id):
        assert secret not in response.text
    for headers in ({}, api["auth_header"], {"Authorization": "Bearer readonly-view-code"}):
        assert agent.get(PREFIX + "/connections", headers=headers).status_code == 401
    assert browser.get(PREFIX + "/connections?bot_id=999").status_code == 403


def test_same_person_other_bot_requires_an_independent_browser_proof(env, client, api):
    _, agent, browser, _ = env
    other_bot, issued = second_binding(client, api)
    assert (
        client.put(
            f"/api/bots/{other_bot['id']}/resource-auth", headers=api["auth_header"], json={"enabled": True}
        ).status_code
        == 200
    )
    items = browser.get(PREFIX + "/connections").json()["connections"]
    assert [item["bot"]["id"] for item in items] == [api["bot_id"]]
    login(agent, browser, issued["credential"], "append")
    items = browser.get(PREFIX + "/connections").json()["connections"]
    assert [item["bot"]["id"] for item in items] == [api["bot_id"], other_bot["id"]]
    assert len({item["id"] for item in items}) == 2
    assert len({item["identity"]["realm"] for item in items}) == 2


@pytest.mark.parametrize("material,health", [(None, "legacy_missing"), ("damaged", "unreadable")])
def test_missing_recovery_material_retains_key_and_does_not_backfill(env, api, db_session, material, health):
    _, _, browser, _ = env
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_encrypted = material
    db_session.commit()
    before = (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at)
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 200
    assert response.json()["connections"][0]["credential_recovery_health"] == health
    db_session.refresh(row)
    assert (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at) == before
    assert credentials.credential_matches(row.credential_digest, api["headers"]["Authorization"][7:])


@pytest.mark.parametrize("change", ["expired", "revoked", "subject", "revision", "instance"])
def test_invalid_binding_denies_metadata_and_provides_secret_free_recovery(env, api, db_session, change):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.settings.models import SiteSetting

    _, agent, browser, _ = env
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    if change == "expired":
        row.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "revoked":
        row.revoked_at = datetime.utcnow()
    elif change == "subject":
        row.external_user_id = "changed-person"
    elif change == "revision":
        db_session.get(Bot, api["bot_id"]).identity_revision += 1
    else:
        db_session.get(SiteSetting, "resource_api_instance_id").value = "changed-instance"
    db_session.commit()
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 401
    guide = response.json()["detail"]["recovery_instructions"]
    assert ORIGIN + "/api/agent-skill/lockbot-resource-agent.zip" in guide
    assert "刷新仅检查状态" in guide and "保留仍有效的本地 Key" in guide
    assert api["headers"]["Authorization"][7:] not in response.text
    if change in {"expired", "revoked", "revision"}:
        assert agent.post(PREFIX + "/links", headers=api["headers"]).status_code == 401


def test_status_refresh_does_not_extend_browser_session_or_binding(env, db_session):
    _, _, browser, path = env
    with sqlite3.connect(path) as store:
        expiry = store.execute("SELECT expires FROM panel_sessions").fetchone()[0]
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    before = (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at)
    for _ in range(2):
        result = browser.get(PREFIX + "/connections")
        assert result.status_code == 200 and "set-cookie" not in result.headers
    with sqlite3.connect(path) as store:
        assert store.execute("SELECT expires FROM panel_sessions").fetchone()[0] == expiry
    db_session.refresh(row)
    assert (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at) == before


def test_revocation_during_metadata_observation_fails_closed(env, db_session, monkeypatch):
    _, _, browser, _ = env
    original = credentials.recover_credential

    def revoke(binding):
        result = original(binding)
        row = db_session.query(ResourceBinding).filter_by(public_id=binding.public_id).one()
        row.revoked_at = datetime.utcnow()
        db_session.commit()
        return result

    monkeypatch.setattr(credentials, "recover_credential", revoke)
    assert browser.get(PREFIX + "/connections").status_code == 403


def test_partial_expiry_preserves_only_the_other_independently_proven_identity(env, api, db_session):
    _, agent, browser, _ = env
    login(agent, browser, api["other_headers"]["Authorization"][7:], "append")
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 200
    assert response.json()["some_authorizations_unavailable"] is True
    assert [item["actor"]["external_user_id"] for item in response.json()["connections"]] == ["bob"]
    assert "alice" not in response.text


def test_revocation_of_an_earlier_scope_while_a_later_scope_loads_denies_the_batch(env, api, db_session, monkeypatch):
    provider, agent, browser, _ = env
    login(agent, browser, api["other_headers"]["Authorization"][7:], "append")
    original = provider.connection

    def inspect(grant):
        result = original(grant)
        if grant["subject"] == "bob":
            db_session.query(ResourceBinding).filter_by(external_user_id="alice").one().revoked_at = datetime.utcnow()
            db_session.commit()
        return result

    monkeypatch.setattr(provider, "connection", inspect)
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 403
    assert "connections" not in response.json()


def test_logout_wins_while_connection_metadata_is_in_flight(env, monkeypatch):
    provider, _, browser, _ = env
    original = provider.connection

    def inspect(grant):
        result = original(grant)
        response = browser.post(PREFIX + "/logout", json={}, headers={"Origin": ORIGIN})
        assert response.status_code == 200
        return result

    monkeypatch.setattr(provider, "connection", inspect)
    assert browser.get(PREFIX + "/connections").status_code == 401


def test_third_party_provider_cannot_advertise_native_connections(tmp_path):
    class CustomProvider(Directory):
        def connection(self, grant):
            raise AssertionError("A plugin cannot acquire native credential authority")

    parent = FastAPI()
    parent.mount(PREFIX, create_panel(ORIGIN, tmp_path / "custom.db", CustomProvider()))
    browser = TestClient(parent, base_url=ORIGIN)
    assert "connection_management" not in browser.get(PREFIX + "/capabilities").json()
    assert browser.get(PREFIX + "/connections").status_code == 404
