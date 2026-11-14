"""Explicit purpose-bound disclosure, using synthetic native binding fixtures."""

# ruff: noqa: F811
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_personal_panel_connections import ORIGIN, PREFIX, env, login  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def issue(env, api):
    _, agent, _, _ = env
    response = agent.post(PREFIX + "/credential-view-links", headers=api["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return parse_qs(urlsplit(response.json()["view_url"]).fragment.split("?", 1)[1])["credential_view"][0]


def post(browser, endpoint, ticket, **extra):
    return browser.post(PREFIX + endpoint, json={"ticket": ticket, **extra}, headers={"Origin": ORIGIN})


def reveal(browser, ticket):
    return post(browser, "/credential-view", ticket, confirmation="view_current_key")


def test_explicit_preview_then_single_use_reveal_and_no_session_or_key_mutation(env, api, db_session):
    _, _, browser, path = env
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    before = (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at)
    ticket = issue(env, api)
    preview = post(browser, "/credential-view-preview", ticket)
    assert preview.status_code == 200
    assert "credential" not in preview.json()
    assert api["headers"]["Authorization"][7:] not in preview.text
    assert preview.json()["connection"]["actor"]["external_user_id"] == "alice"
    response = reveal(browser, ticket)
    assert response.status_code == 200, response.text
    assert response.json()["credential"] == api["headers"]["Authorization"][7:]
    assert response.headers["cache-control"] == "no-store" and "set-cookie" not in response.headers
    assert reveal(browser, ticket).status_code == 410
    db_session.refresh(row)
    assert (row.credential_digest, row.credential_encrypted, row.credential_expires_at, row.revoked_at) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM panel_credential_views").fetchone()[0] == 0


def test_readonly_session_admin_and_view_proofs_cannot_issue_or_login(env, api):
    _, agent, browser, _ = env
    for headers in ({}, api["auth_header"], {"Authorization": "Bearer readonly-view-code"}):
        assert browser.post(PREFIX + "/credential-view-links", headers=headers).status_code == 401
    assert (
        browser.post(PREFIX + "/credential-view-links", headers={**api["headers"], "Origin": ORIGIN}).status_code == 401
    )
    ticket = issue(env, api)
    assert post(browser, "/login", ticket, mode="append").status_code == 410
    assert agent.post(PREFIX + "/links", headers={"Authorization": "Bearer " + ticket}).status_code == 401
    assert post(browser, "/credential-view", ticket, confirmation="rotate").status_code == 400
    assert post(browser, "/credential-view", ticket).status_code == 400
    assert reveal(browser, ticket).status_code == 200


def test_other_browser_principal_denied_without_consuming_intended_ticket(env, api):
    _, agent, browser, _ = env
    ticket = issue(env, api)
    assert reveal(agent, ticket).status_code == 403
    login(agent, agent, api["other_headers"]["Authorization"][7:])
    assert reveal(agent, ticket).status_code == 403
    assert post(agent, "/credential-view-preview", ticket).status_code == 403
    assert reveal(browser, ticket).status_code == 200


@pytest.mark.parametrize("change", ["expired", "revoked", "disabled", "revision", "instance", "subject", "rotated"])
def test_stale_original_authority_denies_preview_reveal_and_new_view_links(env, api, db_session, change):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.settings.models import SiteSetting

    _, agent, browser, _ = env
    ticket = issue(env, api)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    if change == "expired":
        row.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "revoked":
        row.revoked_at = datetime.utcnow()
    elif change == "disabled":
        db_session.get(Bot, api["bot_id"]).resource_auth_enabled = False
    elif change == "revision":
        db_session.get(Bot, api["bot_id"]).identity_revision += 1
    elif change == "instance":
        db_session.get(SiteSetting, "resource_api_instance_id").value = "new-instance"
    elif change == "subject":
        row.external_user_id = "different-person"
    else:
        replacement = "lbr_SYNTHETIC_ROTATED_NOT_A_REAL_KEY"
        row.credential_digest = credentials.digest_credential(replacement)
        row.credential_encrypted = credentials.seal_credential(row.public_id, replacement)
    db_session.commit()
    assert post(browser, "/credential-view-preview", ticket).status_code == 403
    assert reveal(browser, ticket).status_code == 403
    if change not in {"subject", "instance"}:
        assert agent.post(PREFIX + "/credential-view-links", headers=api["headers"]).status_code == 401


@pytest.mark.parametrize("material", [None, "corrupt"])
def test_missing_copy_keeps_original_valid_key_and_consumes_only_disclosure_intent(env, api, db_session, material):
    _, _, browser, _ = env
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_encrypted = material
    db_session.commit()
    old_digest = row.credential_digest
    ticket = issue(env, api)
    assert post(browser, "/credential-view-preview", ticket).status_code == 200
    response = reveal(browser, ticket)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credential_recovery_unavailable"
    assert "保留仍有效的本地 Key" in response.json()["detail"]["recovery_instructions"]
    assert reveal(browser, ticket).status_code == 410
    db_session.refresh(row)
    assert row.credential_digest == old_digest and row.credential_encrypted == material
    assert credentials.credential_matches(old_digest, api["headers"]["Authorization"][7:])


def test_ticket_expiry_and_binding_shorter_deadline_are_enforced(env, api, db_session, monkeypatch):
    import lockbot.backend.app.personal_panel.credential_views as views

    _, _, browser, path = env
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_expires_at = datetime.utcnow() + timedelta(seconds=30)
    db_session.commit()
    ticket = issue(env, api)
    with sqlite3.connect(path) as db:
        deadline = db.execute("SELECT expires FROM panel_credential_views").fetchone()[0]
    assert 0 < deadline - views.time.time() < 31
    monkeypatch.setattr(views.time, "time", lambda: deadline + 1)
    assert reveal(browser, ticket).status_code == 410


def test_parallel_disclosure_has_only_one_success(env, api):
    _, _, browser, _ = env
    ticket = issue(env, api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: reveal(browser, ticket).status_code, range(2)))
    assert sorted(codes) == [200, 410]


@pytest.mark.parametrize("change", ["revoke", "logout", "deadline"])
def test_changes_during_decryption_never_release_the_key(env, api, db_session, monkeypatch, change):
    import lockbot.backend.app.personal_panel.router as router

    _, _, browser, path = env
    ticket = issue(env, api)
    original = credentials.recover_credential
    with sqlite3.connect(path) as db:
        deadline = db.execute("SELECT expires FROM panel_credential_views").fetchone()[0]

    def recover(binding):
        value = original(binding)
        if change == "revoke":
            row = db_session.query(ResourceBinding).filter_by(public_id=binding.public_id).one()
            row.revoked_at = datetime.utcnow()
            db_session.commit()
        elif change == "logout":
            assert browser.post(PREFIX + "/logout", json={}, headers={"Origin": ORIGIN}).status_code == 200
        else:
            monkeypatch.setattr(router.time, "time", lambda: deadline + 1)
        return value

    monkeypatch.setattr(credentials, "recover_credential", recover)
    response = reveal(browser, ticket)
    assert response.status_code == 403
    assert api["headers"]["Authorization"][7:] not in response.text


def test_same_principal_other_bot_requires_its_own_current_browser_grant(env, api, client):
    from backend.test_agent_binding_directory import second_binding

    _, agent, browser, _ = env
    bot, issued = second_binding(client, api)
    assert (
        client.put(
            f"/api/bots/{bot['id']}/resource-auth", headers=api["auth_header"], json={"enabled": True}
        ).status_code
        == 200
    )
    other_api = {"headers": {"Authorization": "Bearer " + issued["credential"]}}
    ticket = issue(env, other_api)
    assert post(browser, "/credential-view-preview", ticket).status_code == 403
    assert reveal(browser, ticket).status_code == 403
    login(agent, browser, issued["credential"], "append")
    result = reveal(browser, ticket)
    assert result.status_code == 200 and result.json()["credential"] == issued["credential"]


def test_login_ticket_is_not_a_view_proof_and_browser_posts_require_exact_origin(env, api):
    _, agent, browser, _ = env
    issued = agent.post(PREFIX + "/links", headers=api["headers"]).json()
    login_ticket = parse_qs(urlsplit(issued["login_url"]).fragment.split("?", 1)[1])["ticket"][0]
    assert reveal(browser, login_ticket).status_code == 410
    ticket = issue(env, api)
    body = {"ticket": ticket, "confirmation": "view_current_key"}
    assert browser.post(PREFIX + "/credential-view", json=body).status_code == 403
    assert (
        browser.post(PREFIX + "/credential-view", json=body, headers={"Origin": "https://other.example"}).status_code
        == 403
    )
    assert (
        browser.post(PREFIX + "/credential-view?ticket=" + ticket, json=body, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert reveal(browser, ticket).status_code == 200


def test_persisted_view_proof_survives_restart_without_storing_the_key(env, api):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lockbot.backend.app.personal_panel.router import create_panel

    provider, _, browser, path = env
    ticket = issue(env, api)
    with sqlite3.connect(path) as db:
        grant, pin = db.execute("SELECT grant_json,pin FROM panel_credential_views").fetchone()
        assert api["headers"]["Authorization"][7:] not in grant + pin
        assert ticket.encode() not in path.read_bytes()
    parent = FastAPI()
    parent.mount(PREFIX, create_panel(ORIGIN, path, provider))
    restarted = TestClient(parent, base_url=ORIGIN)
    restarted.cookies.update(browser.cookies)
    assert reveal(restarted, ticket).status_code == 200
    assert reveal(browser, ticket).status_code == 410
