"""Personal keys select the bot; canonical routes retain the legacy authorization contract."""

# ruff: noqa: F811

import pytest

from backend.test_resource_api_resources import api  # noqa: F401


def test_discovery_and_public_schema_use_key_scoped_routes(client, api):
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    for name, suffix in (
        ("resources", "resources"),
        ("commands", "commands"),
    ):
        assert context["links"][name] == "/api/agent/" + suffix
    public = client.get("/api/public-openapi.json").json()["paths"]
    complete = client.get("/openapi.json").json()["paths"]
    assert "usage_declarations" not in context["links"]
    assert not any("usage-declarations" in path for path in complete)
    for suffix, methods in (
        ("resources", ["get"]),
        ("commands", ["post"]),
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
    legacy_resources = old.json()["resources"]
    canonical_resources = snapshot.json()["resources"]
    for resources in (legacy_resources, canonical_resources):
        for resource in resources:
            for holder in resource["holders"]:
                holder.pop("remaining_seconds")
    assert legacy_resources == canonical_resources
    release = client.post(f"/api/bots/{api['bot_id']}/commands", headers=headers, json={"command": "free n1"})
    assert release.status_code == 200
    assert not client.get("/api/agent/resources", headers=headers).json()["resources"][0]["holders"]


@pytest.mark.parametrize(
    "suffix,method,payload",
    [
        ("resources", "get", None),
        ("commands", "post", {"command": "query"}),
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
    response = client.get("/api/agent/resources", headers=api["headers"])
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_access_disabled"


def test_validation_does_not_echo_private_input(client, api):
    secret = "private input must not be reflected"
    response = client.post("/api/agent/commands", headers=api["headers"], json={"command": "query", "bot_id": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("prefix", ["/api/agent", "/api/bots/{bot_id}"])
def test_usage_declaration_routes_have_been_retired(client, api, db_session, prefix):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    prefix = prefix.format(bot_id=api["bot_id"])
    headers = api["headers"]
    assert client.post(prefix + "/commands", headers=headers, json={"command": "lock n1 1h"}).status_code == 200
    for method, suffix in [("GET", ""), ("PUT", "/n1"), ("DELETE", "/n1")]:
        body = {"purpose": "private task", "agent_label": "private agent"} if method == "PUT" else None
        response = client.request(method, prefix + "/usage-declarations" + suffix, headers=headers, json=body)
        assert response.status_code == 404
        assert "private task" not in response.text
    assert db_session.query(UsageDeclaration).count() == 0
    assert client.get(prefix + "/resources", headers=headers).json()["resources"][0]["holders"]


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
