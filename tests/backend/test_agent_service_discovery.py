"""Discover a key's own bot and separate services without breaking released clients."""

# ruff: noqa: F811
import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_api.skill import skill_artifacts
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.settings.models import SiteSetting
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_skill_cache():
    skill_artifacts.cache_clear()
    snapshot_cache.clear()
    yield
    skill_artifacts.cache_clear()
    snapshot_cache.clear()


def test_manifest_adds_stable_service_identity_and_namespaced_bundle(client, db_session):
    response = client.get("/api/agent-skill")
    meta = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert meta["name"] == "lockbot-agent-api"  # published compatibility fields stay intact
    assert meta["service_id"] == "lockbot.resource-api"
    assert meta["credential_scheme"] == "lbr_bot_scoped" and meta["api_version"] == "1"
    assert meta["server_version"] == client.get("/api/public-openapi.json").json()["info"]["version"]
    assert meta["context_url"] == "/api/agent/context"
    assert client.get("/api/agent-skill").json()["instance_id"] == meta["instance_id"]
    db_session.expire_all()
    assert db_session.get(SiteSetting, "resource_api_instance_id").value == meta["instance_id"]
    assert meta["skill_id"] == "lockbot-resource-agent"
    for descriptor, root in [(meta, "lockbot-agent-api"), (meta["installation"], meta["skill_id"])]:
        bundle = client.get(descriptor["download_url"])
        assert hashlib.sha256(bundle.content).hexdigest() == descriptor["sha256"]
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as zipped:
            assert zipped.namelist() == [root + "/SKILL.md"]
            markdown = zipped.read(zipped.namelist()[0])
            assert hashlib.sha256(markdown).hexdigest() == meta["skill_sha256"]
            assert b"name: lockbot-resource-agent" in markdown
    operator = client.get("/api/op-skill").json()
    assert operator["instance_id"] == meta["instance_id"]
    assert operator["credential_scheme"] == "lbk_bot_scoped"
    assert "credential_context" not in operator["capabilities"]
    assert "context_url" not in operator and "enrollment_url" not in operator
    assert operator["name"] == "lockbot-operator-api"
    assert operator["skill_id"] == "lockbot-operator-agent"
    with zipfile.ZipFile(io.BytesIO(client.get(operator["installation"]["download_url"]).content)) as zipped:
        assert zipped.namelist() == ["lockbot-operator-agent/SKILL.md"]


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_context_resolves_bound_bot_without_a_bot_id_and_retains_old_routes(client, api):
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200, response.text
    context = response.json()
    assert context["bot"]["id"] == api["bot_id"]
    assert context["bot"]["type"] in ["NODE", "DEVICE", "QUEUE"]
    assert context["actor"] == {"platform": "infoflow", "external_user_id": "alice"}
    assert context["credential_scope"] == "one_user_one_bot"
    assert context["server"]["instance_id"] == client.get("/api/agent-skill").json()["instance_id"]
    assert "no-store" in response.headers["cache-control"]
    assert "Authorization" in {value.strip() for value in response.headers["vary"].split(",")}
    assert api["headers"]["Authorization"][7:] not in response.text
    assert client.get(context["links"]["resources"], headers=api["headers"]).status_code == 200
    assert response.headers["x-lockbot-skill-version"] == context["skill"]["version"]
    assert response.headers["x-lockbot-service-id"] == context["server"]["service_id"]


def test_enrollment_still_needs_no_bot_id_or_new_request_fields(client, api):
    issued = client.post("/api/resource-auth/bindings", json={"label": "new owner"}).json()
    assert set(issued) == {"binding_id", "credential", "bind_code", "bind_expires_at"}
    status = client.get("/api/resource-auth/bindings/" + issued["binding_id"])
    assert status.json() == {"binding_id": issued["binding_id"], "status": "pending"}
    assert (
        client.get("/api/agent/context", headers={"Authorization": "Bearer " + issued["credential"]}).status_code == 401
    )
    service.handle_chat_binding(
        platform="infoflow",
        external_user_id="new-owner",
        bot_id=api["bot_id"],
        action="bind",
        argument=issued["bind_code"],
    )
    context = client.get("/api/agent/context", headers={"Authorization": "Bearer " + issued["credential"]}).json()
    assert context["bot"]["id"] == api["bot_id"]
    assert context["actor"]["external_user_id"] == "new-owner"


@pytest.mark.parametrize("change", ["revoked", "expired", "revision", "uuid", "deleted"])
def test_context_rejects_invalidated_credentials(client, api, db_session, change):
    bot = db_session.get(Bot, api["bot_id"])
    binding = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    if change == "revoked":
        binding.revoked_at = datetime.utcnow()
    elif change == "expired":
        binding.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "revision":
        bot.identity_revision += 1
    elif change == "uuid":
        bot.uuid = "replacement-identity"
    else:
        bot.is_deleted = True
    db_session.commit()
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credential"
    assert response.json()["code"] == "credential_invalid"
    assert "bot" not in response.json()


@pytest.mark.parametrize("token", ["eyJforeign.jwt.token", "lbk_operator", "lbr_not-issued", ""])
def test_foreign_and_wrong_principal_types_never_gain_context(client, token):
    headers = {"Authorization": "Bearer " + token} if token else {}
    response = client.get("/api/agent/context", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == ("credential_invalid" if token else "credential_missing")


def test_wrong_bot_error_is_additive_and_points_to_context_not_id_scanning(client, api):
    other = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "outside scope",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["n1"],
        },
    ).json()
    response = client.get(f"/api/bots/{other['id']}/resources", headers=api["headers"])
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credential"
    assert response.json()["code"] == "wrong_bot"
    assert response.json()["context_url"] == "/api/agent/context"
    invalid = client.get(f"/api/bots/{other['id']}/resources", headers={"Authorization": "Bearer lbr_unknown"})
    assert invalid.status_code == 401 and invalid.json()["detail"] == "Invalid credential"
    assert invalid.json()["code"] == "credential_invalid"
    assert client.get("/api/agent/context", headers=api["headers"]).json()["bot"]["id"] == api["bot_id"]


def test_context_works_for_stopped_or_suspended_bots_without_resource_access(client, api, db_session):
    api["manager"].stop_bot(api["bot_id"])
    bot = db_session.get(Bot, api["bot_id"])
    bot.status = "stopped"
    bot.resource_auth_enabled = False
    db_session.commit()
    context = client.get("/api/agent/context", headers=api["headers"])
    assert context.status_code == 200 and context.json()["agent_access_enabled"] is False
    denied = client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "agent_access_disabled"


def test_version_headers_follow_skill_updates_and_old_etag_does_not_hide_new_content(
    client, api, tmp_path, monkeypatch
):
    before = client.get("/api/agent-skill").json()
    markdown = tmp_path / "agent_skill/SKILL.md"
    markdown.parent.mkdir()
    markdown.write_text("---\nname: lockbot-resource-agent\n---\nUpdated instructions\n")
    monkeypatch.setattr("lockbot.backend.app.resource_api.skill.files", lambda package: tmp_path)
    skill_artifacts.cache_clear()
    context = client.get("/api/agent/context", headers=api["headers"])
    after = client.get("/api/agent-skill").json()
    assert after["instance_id"] == before["instance_id"]
    assert after["version"] != before["version"]
    assert context.headers["x-lockbot-skill-version"] == after["version"]
    text = client.get(after["skill_url"], headers={"If-None-Match": '"' + before["skill_sha256"] + '"'})
    assert text.status_code == 200 and hashlib.sha256(text.content).hexdigest() == after["skill_sha256"]


def test_public_schema_authenticates_context_but_not_manifest_or_downloads(client):
    paths = client.get("/api/public-openapi.json").json()["paths"]
    assert paths["/api/agent/context"]["get"]["security"] == [{"AgentKey": []}]
    assert paths["/api/agent-skill"]["get"]["security"] == []
    assert paths["/api/agent-skill/lockbot-resource-agent.zip"]["get"]["security"] == []
    assert "AgentContextResponse" in json.dumps(paths["/api/agent/context"])


def test_new_context_uses_the_shared_credential_read_budget(client, api, monkeypatch):
    from lockbot.backend.app.resource_api.traffic import credential_budget

    monkeypatch.setattr(credential_budget, "read_limit", 1)
    assert client.get("/api/agent/context", headers=api["headers"]).status_code == 200
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 429 and response.json()["detail"]["code"] == "rate_limited"
    assert response.headers["x-lockbot-api-version"] == "1"


def test_instance_identity_is_created_once_across_sessions_and_simultaneous_initialization(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "instances.db"), connect_args={"check_same_thread": False})
    SiteSetting.__table__.create(engine)

    def identify(_):
        with Session(engine) as db:
            return ensure_instance_id(db)

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(identify, range(10)))
        assert len(set(ids)) == 1
        with Session(engine) as db:
            assert ensure_instance_id(db) == ids[0]
            assert db.query(SiteSetting).count() == 1
    finally:
        engine.dispose()
