"""Public skill distribution and owner-specific purposes in real resource responses."""

# ruff: noqa: F811
import hashlib
from datetime import datetime, timedelta
from io import BytesIO
from zipfile import ZipFile

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_api.v2.skill import SKILL_VERSION
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_api_resources import api  # noqa: F401


def test_public_skill_zip_matches_manifest_and_retires_old_downloads(client, db_session, monkeypatch):
    monkeypatch.delenv("PLATFORM_URL", raising=False)
    before = db_session.query(AuditLog).count()
    manifest = client.get("/api/agent/v2/manifest")
    assert manifest.status_code == 200
    skill = manifest.json()["skill"]
    assert skill["version"] == SKILL_VERSION
    assert skill["download_url"] == "/api/agent/v2/skill/lockbot.zip"
    download = client.get(skill["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["content-disposition"] == 'attachment; filename="lockbot.zip"'
    with ZipFile(BytesIO(download.content)) as bundle:
        assert set(bundle.namelist()) == {"lockbot/SKILL.md", "lockbot/agents/openai.yaml"}
        markdown = bundle.read("lockbot/SKILL.md")
        assert b'display_name: "LockBot"' in bundle.read("lockbot/agents/openai.yaml")
    assert hashlib.sha256(download.content).hexdigest() == skill["sha256"]
    assert b"name: lockbot" in markdown
    assert f'metadata:\n  version: "{skill["version"]}"'.encode() in markdown
    assert b"Use lb" in markdown
    assert b"Ask the user for the LockBot service address" in markdown
    assert db_session.query(AuditLog).count() == before

    paths = client.get("/api/public-openapi.json").json()["paths"]
    for obsolete in (
        "/api/agent/v2/skill/SKILL.md",
        "/api/agent-skill",
        "/api/agent-skill/SKILL.md",
        "/api/agent-skill/lockbot-resource-agent.zip",
    ):
        response = client.get(obsolete, follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers
        assert obsolete not in paths


def test_deployment_skill_uses_the_configured_address(client, monkeypatch):
    monkeypatch.delenv("PLATFORM_URL", raising=False)
    generic = client.get("/api/agent/v2/manifest").json()["skill"]
    monkeypatch.setenv("PLATFORM_URL", "http://192.0.2.10:8155/")
    headers = {"X-Forwarded-Host": "untrusted.example"}
    skill = client.get("/api/agent/v2/manifest", headers=headers).json()["skill"]
    download = client.get(skill["download_url"], headers=headers)
    with ZipFile(BytesIO(download.content)) as bundle:
        markdown = bundle.read("lockbot/SKILL.md")
    assert b"lb login http://192.0.2.10:8155" in markdown
    assert b"Use `lb resource list` to search all saved, authorized connections" in markdown
    assert b"lb --origin http://192.0.2.10:8155" not in markdown
    assert markdown.index(b"ask whether the user wants to start group authorization") < markdown.index(
        b"lb login http://192.0.2.10:8155"
    )
    assert b"untrusted.example" not in markdown
    assert skill["sha256"] != generic["sha256"]
    assert hashlib.sha256(download.content).hexdigest() == skill["sha256"]
    assert download.headers["etag"] == f'"{skill["sha256"]}"'


@pytest.mark.parametrize(
    ("url", "included"),
    [
        ("https://releases.example.test/lb/releases/latest.json", True),
        ("http://releases.example.test/lb/releases/latest.json", False),
        ("https://releases.example.test/lb/releases/latest.json?token=private", False),
    ],
)
def test_skill_zip_includes_only_a_configured_https_release_manifest(client, monkeypatch, url, included):
    monkeypatch.delenv("LB_RELEASE_MANIFEST_URL", raising=False)

    def read_skill():
        skill = client.get("/api/agent/v2/manifest").raise_for_status().json()["skill"]
        response = client.get(skill["download_url"]).raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == skill["sha256"]
        with ZipFile(BytesIO(response.content)) as bundle:
            return skill["sha256"], bundle.read("lockbot/SKILL.md").decode()

    fallback_hash, fallback = read_skill()
    monkeypatch.setenv("LB_RELEASE_MANIFEST_URL", url)
    digest, markdown = read_skill()
    assert (url in markdown) is included
    assert (digest != fallback_hash) is included
    assert "`command -v lb`" in markdown
    assert "Ask the user for an approved internal lb release manifest URL" in fallback
    assert "{{LOCKBOT_LB_INSTALL_SOURCE}}" not in markdown


def command(client, case, text, headers=None):
    response = client.post(
        f"/api/bots/{case['bot_id']}/commands", headers=headers or case["headers"], json={"command": text}
    )
    assert response.status_code == 200, response.text


def snapshot(client, case, headers=None):
    response = client.get(f"/api/bots/{case['bot_id']}/resources", headers=headers or case["headers"])
    assert response.status_code == 200, response.text
    return response.json()["resources"][0]


def test_purpose_changes_are_visible_on_cached_hold_and_not_after_reacquire(client, api, seed_legacy_task_details):
    command(client, api, "lock n1 1h")
    assert snapshot(client, api)["holders"][0]["usage"] is None
    seed_legacy_task_details(api, "<script>ignored()</script> train model A")
    usage = snapshot(client, api)["holders"][0]["usage"]
    assert usage["purpose"] == "<script>ignored()</script> train model A"
    assert usage["untrusted_text"] is True and usage["actor"]["external_user_id"] == "alice"
    # Bypass the HTTP invalidation deliberately: a cached old hold must not revive its purpose.
    api["bot"].unlock("alice", "unlock n1")
    api["bot"].lock("alice", "lock n1 1h")
    assert snapshot(client, api)["holders"][0]["usage"] is None
    seed_legacy_task_details(api, "new task")
    assert snapshot(client, api)["holders"][0]["usage"]["purpose"] == "new task"


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_holder_and_waiting_user_get_separate_purposes(client, api, seed_legacy_task_details):
    command(client, api, "lock n1 1h")
    command(client, api, "book n1", api["other_headers"])
    seed_legacy_task_details(api, "training")
    seed_legacy_task_details(api, "benchmark after training", user="bob")
    resource = snapshot(client, api)
    assert resource["holders"][0]["usage"]["purpose"] == "training"
    booking = resource["booking_list"][0]
    assert booking["external_user_id"] == "bob" and booking["position"] == 0
    assert "purpose" not in booking["usage"]
    own_booking = snapshot(client, api, api["other_headers"])["booking_list"][0]
    assert own_booking["usage"]["purpose"] == "benchmark after training"
    assert booking["usage"]["actor"]["external_user_id"] == "bob"


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_device_purpose_is_attached_only_to_its_owner(client, api, seed_legacy_task_details):
    command(client, api, "lock n1 dev0 1h")
    command(client, api, "lock n1 dev1 1h", api["other_headers"])
    seed_legacy_task_details(api, "GPU experiment")
    resource = snapshot(client, api)
    assert resource["devices"][0]["holders"][0]["usage"]["purpose"] == "GPU experiment"
    assert resource["devices"][1]["holders"][0]["usage"] is None


def test_expired_revoked_and_wrong_bot_declarations_are_hidden(client, api, seed_legacy_task_details, db_session):
    command(client, api, "lock n1 1h")
    seed_legacy_task_details(api, "private until authorized")
    assert snapshot(client, api)["holders"][0]["usage"]
    row = db_session.query(UsageDeclaration).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert snapshot(client, api)["holders"][0]["usage"] is None
    seed_legacy_task_details(api, "new declaration")
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
