"""Public skill distribution and owner-specific purposes in real resource responses."""

# ruff: noqa: F811
import hashlib
import io
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_api_resources import api  # noqa: F401


def test_public_skill_bundle_matches_manifest_and_text(client, db_session):
    before = db_session.query(AuditLog).count()
    manifest = client.get("/api/agent-skill")
    assert manifest.status_code == 200
    meta = manifest.json()
    markdown = client.get(meta["skill_url"])
    bundle = client.get(meta["download_url"])
    assert markdown.status_code == bundle.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert bundle.headers["content-type"] == "application/zip"
    assert hashlib.sha256(markdown.content).hexdigest() == meta["skill_sha256"]
    assert hashlib.sha256(bundle.content).hexdigest() == meta["sha256"]
    assert len(bundle.content) == meta["size_bytes"]
    assert meta["version"] in bundle.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert archive.namelist() == ["lockbot-agent-api/SKILL.md"]
        assert archive.read(archive.namelist()[0]) == markdown.content
    assert meta["issues_url"] == "https://github.com/dynamicheart/lockbot/issues"
    source = Path(__file__).parents[2] / ".." / "docs/agent-api/SKILL.md"
    if source.exists():
        assert source.read_bytes() == markdown.content
    assert db_session.query(AuditLog).count() == before
    assert client.get(meta["download_url"], headers={"If-None-Match": bundle.headers["etag"]}).status_code == 304
    paths = client.get("/api/public-openapi.json").json()["paths"]
    assert all(p in paths for p in ["/api/agent-skill", meta["skill_url"], meta["download_url"]])


def command(client, case, text, headers=None):
    response = client.post(
        f"/api/bots/{case['bot_id']}/commands", headers=headers or case["headers"], json={"command": text}
    )
    assert response.status_code == 200, response.text


def declare(client, case, purpose, headers=None):
    response = client.put(
        f"/api/bots/{case['bot_id']}/usage-declarations/n1",
        headers=headers or case["headers"],
        json={"purpose": purpose},
    )
    assert response.status_code == 200, response.text


def snapshot(client, case, headers=None):
    response = client.get(f"/api/bots/{case['bot_id']}/resources", headers=headers or case["headers"])
    assert response.status_code == 200, response.text
    return response.json()["resources"][0]


def test_purpose_changes_are_visible_on_cached_hold_and_not_after_reacquire(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    command(client, api, "lock n1 1h")
    assert snapshot(client, api)["holders"][0]["usage"] is None
    declare(client, api, "<script>ignored()</script> train model A")
    usage = snapshot(client, api)["holders"][0]["usage"]
    assert usage["purpose"] == "<script>ignored()</script> train model A"
    assert usage["untrusted_text"] is True and usage["actor"]["external_user_id"] == "alice"
    # Bypass the HTTP invalidation deliberately: a cached old hold must not revive its purpose.
    api["bot"].unlock("alice", "unlock n1")
    api["bot"].lock("alice", "lock n1 1h")
    assert snapshot(client, api)["holders"][0]["usage"] is None
    declare(client, api, "new task")
    assert snapshot(client, api)["holders"][0]["usage"]["purpose"] == "new task"


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_holder_and_waiting_user_get_separate_purposes(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    command(client, api, "lock n1 1h")
    command(client, api, "book n1", api["other_headers"])
    declare(client, api, "training")
    declare(client, api, "benchmark after training", api["other_headers"])
    resource = snapshot(client, api)
    assert resource["holders"][0]["usage"]["purpose"] == "training"
    booking = resource["booking_list"][0]
    assert booking["external_user_id"] == "bob" and booking["position"] == 0
    assert "purpose" not in booking["usage"]
    own_booking = snapshot(client, api, api["other_headers"])["booking_list"][0]
    assert own_booking["usage"]["purpose"] == "benchmark after training"
    assert booking["usage"]["actor"]["external_user_id"] == "bob"


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_device_purpose_is_attached_only_to_its_owner(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    command(client, api, "lock n1 dev0 1h")
    command(client, api, "lock n1 dev1 1h", api["other_headers"])
    declare(client, api, "GPU experiment")
    resource = snapshot(client, api)
    assert resource["devices"][0]["holders"][0]["usage"]["purpose"] == "GPU experiment"
    assert resource["devices"][1]["holders"][0]["usage"] is None


def test_expired_revoked_and_wrong_bot_declarations_are_hidden(client, api, monkeypatch, db_session):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    command(client, api, "lock n1 1h")
    declare(client, api, "private until authorized")
    assert snapshot(client, api)["holders"][0]["usage"]
    row = db_session.query(UsageDeclaration).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert snapshot(client, api)["holders"][0]["usage"] is None
    declare(client, api, "new declaration")
    binding = db_session.get(ResourceBinding, row.binding_id)
    binding.revoked_at = datetime.utcnow()
    db_session.commit()
    assert snapshot(client, api, api["other_headers"])["holders"][0]["usage"] is None
    assert client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"]).status_code == 401
    other = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "outside-scope",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/unused",
            "cluster_configs": ["n1"],
        },
    )
    assert other.status_code == 201
    assert client.get(f"/api/bots/{other.json()['id']}/resources", headers=api["other_headers"]).status_code == 401
