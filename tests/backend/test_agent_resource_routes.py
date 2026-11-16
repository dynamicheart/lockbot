"""Personal keys select the bot; canonical routes retain the legacy authorization contract."""

# ruff: noqa: F811

import pytest

from backend.test_resource_api_resources import api  # noqa: F401


def test_discovery_and_public_schema_use_key_scoped_routes(client, api):
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    for name, suffix in (
        ("resources", "resources"),
        ("commands", "commands"),
        ("usage_declarations", "usage-declarations"),
    ):
        assert context["links"][name] == "/api/agent/" + suffix
    public = client.get("/api/public-openapi.json").json()["paths"]
    complete = client.get("/openapi.json").json()["paths"]
    for suffix, methods in (
        ("resources", ["get"]),
        ("commands", ["post"]),
        ("usage-declarations", ["get"]),
        ("usage-declarations/{resource_id}", ["put", "delete"]),
    ):
        assert "/api/bots/{bot_id}/" + suffix not in public
        for method in methods:
            operation = public["/api/agent/" + suffix][method]
            assert operation["security"] == [{"AgentKey": []}]
            assert not any(p["name"] == "bot_id" for p in operation.get("parameters", []))
            assert complete["/api/bots/{bot_id}/" + suffix][method]["deprecated"] is True


def test_canonical_acquisition_and_legacy_readback_share_state(client, api):
    headers = api["headers"]
    response = client.post("/api/agent/commands?bot_id=999999", headers=headers, json={"command": "lock n1 10m"})
    assert response.status_code == 200, response.text
    snapshot = client.get("/api/agent/resources?bot_id=999999", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json()["bot_id"] == api["bot_id"]
    old = client.get(f"/api/bots/{api['bot_id']}/resources", headers=headers)
    assert old.json()["resources"] == snapshot.json()["resources"]
    release = client.post(f"/api/bots/{api['bot_id']}/commands", headers=headers, json={"command": "free n1"})
    assert release.status_code == 200
    assert not client.get("/api/agent/resources", headers=headers).json()["resources"][0]["holders"]


@pytest.mark.parametrize(
    "suffix,method,payload",
    [
        ("resources", "get", None),
        ("commands", "post", {"command": "query"}),
        ("usage-declarations", "get", None),
        ("usage-declarations/n1", "put", {"purpose": "test"}),
        ("usage-declarations/n1", "delete", None),
    ],
)
def test_canonical_routes_reject_missing_wrong_and_revoked_credentials(client, api, suffix, method, payload):
    kwargs = {"json": payload} if payload is not None else {}
    for headers in ({}, api["auth_header"], {"Authorization": "Bearer lbk_not-a-personal-key"}):
        result = client.request(method, "/api/agent/" + suffix, headers=headers, **kwargs)
        assert result.status_code == 401, result.text
        if suffix == "resources":
            assert result.headers["cache-control"] == "no-store"
    client.delete(f"/api/bots/{api['bot_id']}/resource-auth/bindings", headers=api["auth_header"])
    assert client.request(method, "/api/agent/" + suffix, headers=api["headers"], **kwargs).status_code == 401


def test_access_switch_and_command_allowlist_still_apply(client, api):
    response = client.post("/api/agent/commands", headers=api["headers"], json={"command": "kicklock n1"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "command_not_allowed"
    client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=api["auth_header"], json={"enabled": False})
    for path in ("resources", "usage-declarations"):
        response = client.get("/api/agent/" + path, headers=api["headers"])
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "agent_access_disabled"


def test_validation_does_not_echo_private_input(client, api):
    secret = "private input must not be reflected"
    for method, path, body in (
        ("post", "commands", {"command": "query", "bot_id": secret}),
        ("put", "usage-declarations/n1", {"purpose": secret * 100}),
    ):
        response = client.request(method, "/api/agent/" + path, headers=api["headers"], json=body)
        assert response.status_code == 422
        assert secret not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_usage_privacy_and_conditional_cleanup_share_legacy_state(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    headers = api["headers"]
    assert client.post("/api/agent/commands", headers=headers, json={"command": "lock n1 10m"}).status_code == 200
    saved = client.put(
        "/api/agent/usage-declarations/n1",
        headers=headers,
        json={"purpose": "private task", "task_category": "code_review"},
    )
    assert saved.status_code == 200, saved.text
    revision = saved.json()["revision"]
    old = client.get(f"/api/bots/{api['bot_id']}/usage-declarations", headers=headers)
    own = client.get("/api/agent/usage-declarations", headers=headers)
    assert own.json() == old.json()
    assert own.json()["items"][0]["purpose"] == "private task"
    others = client.get("/api/agent/usage-declarations", headers=api["other_headers"])
    assert "private task" not in others.text
    rejected = client.delete("/api/agent/usage-declarations/n1", headers={**headers, "If-Match": "stale"})
    assert rejected.status_code == 412
    deleted = client.delete("/api/agent/usage-declarations/n1", headers={**headers, "If-Match": revision})
    assert deleted.status_code == 200, deleted.text
    assert not client.get("/api/agent/usage-declarations", headers=headers).json()["items"]
    assert client.get("/api/agent/resources", headers=headers).json()["resources"][0]["holders"]


def test_canonical_reads_share_credential_budget_and_no_store(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.resources.credential_budget.check", lambda *args: (False, 7))
    response = client.get("/api/agent/resources", headers=api["headers"])
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["cache-control"] == "no-store"


def test_canonical_traffic_keeps_verified_bot_and_actor_attribution(client, api, monkeypatch, db_session):
    from lockbot.backend.app.resource_api.activity import AgentActivity
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    records = []
    monkeypatch.setattr(client.app.state.service_traffic, "record", lambda **record: records.append(record))
    assert client.get("/api/agent/resources", headers=api["headers"]).status_code == 200
    assert client.post("/api/agent/commands", headers=api["headers"], json={"command": "query"}).status_code == 200
    assert len(records) == 2
    for record in records:
        assert record["source"] == "agent"
        assert record["bot_id"] == api["bot_id"]
        assert record["credential"]["kind"] == "agent"
        assert record["credential"]["user"] == "alice"
    assert {record["route"] for record in records} == {"GET /api/agent/resources", "POST /api/agent/commands"}
    binding = db_session.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"], external_user_id="alice").one()
    activity = db_session.get(AgentActivity, binding.id)
    assert activity.read_requests == 2
    assert activity.write_requests == 0


def test_canonical_command_rechecks_credential_after_admission(client, api, monkeypatch):
    from contextlib import contextmanager

    from lockbot.backend.app.resource_auth.models import ResourceBinding

    original_admit = api["manager"].admit

    @contextmanager
    def revoke_before_admitting(*args, **kwargs):
        # Use a separate session, as a concurrent revoke would, after dependency authentication.
        from lockbot.backend.app.database import SessionLocal

        with SessionLocal() as db:
            row = db.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"], external_user_id="alice").one()
            from datetime import datetime

            row.revoked_at = datetime.utcnow()
            db.commit()
        with original_admit(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(api["manager"], "admit", revoke_before_admitting)
    result = client.post("/api/agent/commands", headers=api["headers"], json={"command": "lock n1 10m"})
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "credential_invalid"
    assert not api["bot"].state.bot_state["n1"]["current_users"]
