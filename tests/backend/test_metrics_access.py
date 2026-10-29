# ruff: noqa: F811
import time
from datetime import datetime, timedelta, timezone

import jwt
from lockbot.backend.app.bots.metrics_access import issue_metrics_access
from lockbot.backend.app.bots.models import Bot

from backend.test_resource_api_resources import api  # noqa: F401


def source():
    return {
        "endpoint": "https://metrics.example.invalid",
        "profile": "lockbot-v1",
        "accessMode": "short_lived",
        "nodeMapping": {"n1": "host-a"},
    }


def signing_key(tmp_path, monkeypatch):
    path = tmp_path / "metrics.key"
    path.write_bytes(b"x" * 64)
    path.chmod(0o600)
    monkeypatch.setenv("METRICS_GRANT_SECRET_FILE", str(path))
    return path


def test_owner_can_authorize_only_current_bot_mappings_and_no_secrets_are_returned(client, api, tmp_path, monkeypatch):
    signing_key(tmp_path, monkeypatch)
    path = f"/api/bots/{api['bot_id']}/utilization-access"
    assert client.post(path, json=source()).status_code == 401
    assert client.post(path, headers=api["headers"], json=source()).status_code == 401
    response = client.post(path, headers=api["auth_header"], json=source())
    assert response.status_code == 200, response.text
    decoded = jwt.decode(response.json()["token"], b"x" * 64, algorithms=["HS256"], audience=source()["endpoint"])
    assert decoded["nodes"] == ["host-a"] and decoded["sub"]
    assert decoded["exp"] - decoded["iat"] <= 300
    assert decoded["exp"] > time.time()
    assert "x" * 64 not in response.text
    invalid = {**source(), "nodeMapping": {"unconfigured-resource": "host-b"}}
    assert client.post(path, headers=api["auth_header"], json=invalid).status_code == 503


def test_agent_discovers_short_grant_but_disabling_access_stops_new_grants(client, api, tmp_path, monkeypatch):
    signing_key(tmp_path, monkeypatch)
    path = f"/api/bots/{api['bot_id']}"
    assert client.put(path, headers=api["auth_header"], json={"utilization_config": source()}).status_code == 200
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    context = response.json()
    token = context["utilization"]["access"]["token"]
    decoded = jwt.decode(token, b"x" * 64, algorithms=["HS256"], audience=source()["endpoint"])
    assert decoded["iss"] == context["server"]["instance_id"]
    assert decoded["sub"] == context["bot"]["uuid"]
    assert client.put(path + "/resource-auth", headers=api["auth_header"], json={"enabled": False}).status_code == 200
    suspended = client.get("/api/agent/context", headers=api["headers"]).json()
    assert suspended["utilization"]["access"] is None


def test_expiry_caps_view_access_and_key_permissions_are_enforced(api, db_session, tmp_path, monkeypatch):
    key = signing_key(tmp_path, monkeypatch)
    bot = db_session.get(Bot, api["bot_id"])
    expiry = datetime.fromtimestamp(time.time() + 45, timezone.utc)
    grant = issue_metrics_access(db_session, bot, source(), expires_at=expiry)
    assert grant["expires_at"] <= int(expiry.timestamp())
    with_expired = datetime.fromtimestamp(time.time() - 1, timezone.utc)
    assert issue_metrics_access(db_session, bot, source(), expires_at=with_expired) is None
    key.chmod(0o644)
    assert issue_metrics_access(db_session, bot, source()) is None
    monkeypatch.delenv("METRICS_GRANT_SECRET_FILE")
    assert issue_metrics_access(db_session, bot, source()) is None


def test_readonly_view_receives_only_short_metrics_grant_after_its_normal_authorization(
    client, api, tmp_path, monkeypatch
):
    from urllib.parse import urlsplit

    monkeypatch.setattr("lockbot.backend.app.resource_api.views.bot_manager", api["manager"])
    signing_key(tmp_path, monkeypatch)
    path = f"/api/bots/{api['bot_id']}"
    assert client.put(path, headers=api["auth_header"], json={"utilization_config": source()}).status_code == 200
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    view_code = urlsplit(context["view_url"]).fragment.rsplit("/", 1)[1]
    view_path = f"/api/bot-views/{api['bot_id']}/state"
    assert client.get(view_path).status_code != 200
    response = client.get(view_path, headers={"Authorization": "Bearer " + view_code})
    assert response.status_code == 200, response.text
    grant = response.json()["utilization_access"]
    decoded = jwt.decode(grant["token"], b"x" * 64, algorithms=["HS256"], audience=source()["endpoint"])
    assert decoded["nodes"] == ["host-a"] and decoded["sub"] == context["bot"]["uuid"]
    assert "x" * 64 not in response.text and view_code not in grant["token"]


def test_agent_grant_does_not_outlive_its_expiring_personal_credential(client, api, db_session, tmp_path, monkeypatch):
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    signing_key(tmp_path, monkeypatch)
    path = f"/api/bots/{api['bot_id']}"
    assert client.put(path, headers=api["auth_header"], json={"utilization_config": source()}).status_code == 200
    row = db_session.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"], external_user_id="alice").one()
    deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=45)
    row.credential_expires_at = deadline
    db_session.commit()
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    grant = response.json()["utilization"]["access"]
    assert grant["expires_at"] <= int(deadline.replace(tzinfo=timezone.utc).timestamp())
