"""Opt-in rotation requires fresh purpose proof and server-side human intent."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.personal_panel.router import create_panel
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_personal_panel_connections import ORIGIN, PREFIX, env, login  # noqa: F401
from backend.test_personal_panel_credential_views import issue as issue_view
from backend.test_personal_panel_credential_views import post, reveal
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture
def rotation(env):
    provider, _, old_browser, path = env
    provider.config["allow_credential_rotation"] = True
    parent = FastAPI()
    parent.mount(PREFIX, create_panel(ORIGIN, path, provider))
    agent, browser = TestClient(parent, base_url=ORIGIN), TestClient(parent, base_url=ORIGIN)
    browser.cookies.update(old_browser.cookies)
    return provider, agent, browser, path


def issue(rotation, api):
    response = rotation[1].post(PREFIX + "/credential-rotation-links", headers=api["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return parse_qs(urlsplit(response.json()["rotation_url"]).fragment.split("?", 1)[1])["credential_rotation"][0]


def prepare(rotation, api):
    token = issue(rotation, api)
    response = post(rotation[2], "/credential-rotation-preview", token)
    assert response.status_code == 200, response.text
    assert api["headers"]["Authorization"][7:] not in response.text
    assert post(rotation[2], "/credential-rotation-preview", token).status_code == 410
    return response.json()


def confirm(browser, preview, **changes):
    data = {
        "intent": preview["intent"],
        "typed_group": preview["connection"]["bot"]["name"],
        "confirmation": "invalidate_all_agents_using_this_key",
        **changes,
    }
    return browser.post(PREFIX + "/credential-rotation-confirm", json=data, headers={"Origin": ORIGIN})


def test_disabled_by_default_and_readonly_or_view_proofs_cannot_upgrade(env, rotation, api):
    # The old app remains default-off even though the independent opt-in app
    # shares its existing session database and authoritative native bindings.
    assert "credential_rotation" not in env[2].get(PREFIX + "/capabilities").json()["connection_management"]
    assert env[1].post(PREFIX + "/credential-rotation-links", headers=api["headers"]).status_code == 404
    for headers in ({}, api["auth_header"], {"Authorization": "Bearer readonly-view-code"}):
        assert rotation[2].post(PREFIX + "/credential-rotation-links", headers=headers).status_code == 401
    view = issue_view(rotation, api)
    assert post(rotation[2], "/credential-rotation-preview", view).status_code == 410
    token = issue(rotation, api)
    assert reveal(rotation[2], token).status_code == 410
    assert post(rotation[2], "/login", token, mode="append").status_code == 410


def test_rotation_changes_only_this_binding_and_requires_fresh_login(rotation, api, db_session):
    provider, agent, browser, _ = rotation
    login(agent, browser, api["other_headers"]["Authorization"][7:], "append")
    grant = provider.authenticate(api["headers"]["Authorization"][7:])
    assert "credential_generation" not in grant["scope"]
    old_view = issue_view(rotation, api)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_expires_at = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()
    expiry, public_id = row.credential_expires_at, row.public_id
    bot_revision = db_session.get(Bot, api["bot_id"]).identity_revision
    before_bob = provider.authenticate(api["other_headers"]["Authorization"][7:])
    preview = prepare(rotation, api)
    response = confirm(browser, preview)
    assert response.status_code == 200, response.text
    key = response.json()["credential"]
    assert key != api["headers"]["Authorization"][7:] and key.startswith("lbr_")
    assert response.headers["cache-control"] == "no-store" and "set-cookie" not in response.headers
    assert response.json()["requires_fresh_login"] is True
    db_session.refresh(row)
    assert row.credential_generation == 1 and row.credential_expires_at == expiry
    assert row.public_id == public_id and row.revoked_at is None
    assert db_session.get(Bot, api["bot_id"]).identity_revision == bot_revision
    assert credentials.recover_credential(row) == key
    assert not provider.validate(grant)
    assert provider.authenticate(key)["scope"] != grant["scope"]
    assert provider.validate(before_bob)
    assert [item["subject"] for item in browser.get(PREFIX + "/session").json()["identities"]] == ["bob"]
    assert reveal(browser, old_view).status_code == 403
    assert agent.post(PREFIX + "/links", headers=api["headers"]).status_code == 401
    login(agent, browser, key, "append")
    assert {item["subject"] for item in browser.get(PREFIX + "/session").json()["identities"]} == {"alice", "bob"}


def test_group_name_and_second_confirmation_are_enforced_by_server_without_changing_key(rotation, api, db_session):
    preview = prepare(rotation, api)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    before = (row.credential_digest, row.credential_encrypted, row.credential_generation)
    for changes in ({"typed_group": "wrong-group"}, {"confirmation": ""}, {"confirmation": "view_current_key"}):
        assert confirm(rotation[2], preview, **changes).status_code == 400
    db_session.refresh(row)
    assert (row.credential_digest, row.credential_encrypted, row.credential_generation) == before
    assert confirm(rotation[2], preview).status_code == 200
    assert confirm(rotation[2], preview).status_code in {403, 410}


@pytest.mark.parametrize("change", ["expired", "revoked", "revision", "instance", "subject", "disabled", "renamed"])
def test_authority_or_canonical_group_changes_reject_stale_intent(rotation, api, db_session, change):
    from lockbot.backend.app.settings.models import SiteSetting

    preview = prepare(rotation, api)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    digest = row.credential_digest
    bot = db_session.get(Bot, api["bot_id"])
    if change == "expired":
        row.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "revoked":
        row.revoked_at = datetime.utcnow()
    elif change == "revision":
        bot.identity_revision += 1
    elif change == "instance":
        db_session.get(SiteSetting, "resource_api_instance_id").value = "new-instance"
    elif change == "subject":
        row.external_user_id = "other-person"
    elif change == "disabled":
        bot.resource_auth_enabled = False
    else:
        bot.name = "renamed-group"
    db_session.commit()
    assert confirm(rotation[2], preview).status_code in {403, 409}
    db_session.refresh(row)
    assert row.credential_digest == digest and row.credential_generation == 0


@pytest.mark.parametrize("failure", ["seal", "commit", "cas", "deadline"])
def test_failed_rotation_preserves_old_key_and_cannot_replay_intent(rotation, api, db_session, monkeypatch, failure):
    from sqlalchemy.orm import Session

    preview = prepare(rotation, api)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    before = (row.credential_digest, row.credential_encrypted, row.credential_generation)
    if failure == "seal":
        monkeypatch.setattr(
            credentials, "seal_credential", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
        )
    elif failure == "commit":
        monkeypatch.setattr(Session, "commit", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    elif failure == "deadline":
        clock = service._utcnow
        monkeypatch.setattr(service, "_utcnow", lambda: clock() + timedelta(minutes=3))
    else:
        original = credentials.seal_credential

        def seal(*args):
            value = original(*args)
            bot = db_session.get(Bot, api["bot_id"])
            bot.name = "changed-during-seal"
            db_session.commit()
            return value

        monkeypatch.setattr(credentials, "seal_credential", seal)
    assert confirm(rotation[2], preview).status_code in {409, 503}
    assert confirm(rotation[2], preview).status_code == 410
    db_session.refresh(row)
    assert (row.credential_digest, row.credential_encrypted, row.credential_generation) == before
    assert credentials.credential_matches(row.credential_digest, api["headers"]["Authorization"][7:])


def test_two_fresh_intents_cannot_cause_two_rotations(rotation, api, db_session):
    previews = [prepare(rotation, api), prepare(rotation, api)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda value: confirm(rotation[2], value).status_code, previews))
    assert codes.count(200) == 1
    assert all(code in {200, 403, 409, 410} for code in codes)
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    db_session.refresh(row)
    assert row.credential_generation == 1


def test_other_browser_cannot_preview_or_confirm_a_rotation(rotation, api):
    token = issue(rotation, api)
    assert post(rotation[1], "/credential-rotation-preview", token).status_code == 403
    preview = post(rotation[2], "/credential-rotation-preview", token).json()
    assert confirm(rotation[1], preview).status_code == 403
    assert confirm(rotation[2], preview).status_code == 200


@pytest.mark.parametrize("loss", ["logout", "cleanup"])
def test_post_commit_failure_is_reported_without_key_and_never_rotates_again(
    rotation, api, db_session, monkeypatch, loss
):
    from lockbot.backend.app.personal_panel.sessions import PanelSessions

    preview = prepare(rotation, api)
    provider, _, browser, _ = rotation
    if loss == "logout":
        original = provider.rotate_credential

        def rotate(*args):
            result = original(*args)
            assert browser.post(PREFIX + "/logout", json={}, headers={"Origin": ORIGIN}).status_code == 200
            return result

        monkeypatch.setattr(provider, "rotate_credential", rotate)
    else:
        monkeypatch.setattr(
            PanelSessions, "remove_grant", lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("synthetic"))
        )
    response = confirm(browser, preview)
    assert response.status_code in {403, 503}
    assert "do not retry" in response.text or "do not replay" in response.text
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    db_session.refresh(row)
    assert row.credential_generation == 1
    assert credentials.recover_credential(row) not in response.text
    assert confirm(browser, preview).status_code in {403, 410}
    assert browser.get(PREFIX + "/session").json()["authenticated"] is False
    db_session.refresh(row)
    assert row.credential_generation == 1


def test_cas_serializes_independent_database_writers_without_process_guards(tmp_path, monkeypatch):
    import threading
    import time

    from cryptography.fernet import Fernet
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bots import encryption
    from lockbot.backend.app.database import Base
    from lockbot.backend.app.settings.models import SiteSetting
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(encryption, "_fernet", Fernet(Fernet.generate_key()))

    engine = create_engine(f"sqlite:///{tmp_path / 'rotation.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    old = "lbr_SYNTHETIC_ORIGINAL_FOR_CONCURRENT_CAS"
    with sessions() as db:
        db.add(User(id=1, username="fixture", email="fixture@example.invalid", password_hash="unused", role="user"))
        db.add(
            Bot(
                id=1,
                uuid="fixture-bot",
                user_id=1,
                name="Fixture",
                bot_type="NODE",
                platform="infoflow",
                webhook_url="",
                cluster_configs='["n1"]',
                resource_auth_enabled=True,
            )
        )
        db.add(SiteSetting(key="resource_api_instance_id", value="fixture-instance"))
        db.add(
            ResourceBinding(
                id=1,
                public_id="fixture-binding",
                platform="infoflow",
                external_user_id="alice",
                bound_bot_id=1,
                bound_bot_uuid="fixture-bot",
                bound_identity_revision=0,
                credential_digest=credentials.digest_credential(old),
                credential_encrypted=credentials.seal_credential("fixture-binding", old),
            )
        )
        db.commit()
    barrier = threading.Barrier(2)

    def rotate(_index):
        with sessions() as db:
            row = db.get(ResourceBinding, 1)
            assert row.credential_generation == 0
            barrier.wait(timeout=5)
            try:
                return service.rotate_confirmed_credential(
                    db, row=row, instance_id="fixture-instance", canonical_name="Fixture", deadline=time.time() + 120
                )
            except ValueError:
                return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(rotate, range(2)))
        keys = [value for value in results if value is not None]
        assert len(keys) == 1
        with sessions() as db:
            row = db.get(ResourceBinding, 1)
            assert row.credential_generation == 1
            assert credentials.recover_credential(row) == keys[0]
            assert not credentials.credential_matches(row.credential_digest, old)
    finally:
        engine.dispose()
