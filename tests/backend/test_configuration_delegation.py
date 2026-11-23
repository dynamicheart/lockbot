"""Service-level coverage of user-issued, operation-scoped configuration credentials."""

from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app import main
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.delegation import ConfigurationCredential, _now
from lockbot.backend.app.bots.models import Bot
from sqlalchemy import create_engine, text

from backend.test_bots import _sample_bot

ISSUE = "/api/configuration-credentials"
NODE = "node_access:write"
SOURCE = "datasource:write"
INVENTORY = "node_inventory:write"


def bot(client, headers, name):
    return client.post("/api/bots", headers=headers, json=_sample_bot(name)).raise_for_status().json()["id"]


def credentials(client, headers, ids, scopes):
    return client.post(ISSUE, headers=headers, json={"label": "inventory-agent", "bot_ids": ids, "scopes": scopes})


def bearer(value):
    return {"Authorization": f"Bearer {value}"}


def access(bot_id):
    return f"/api/bots/{bot_id}/configuration-access"


def preview(client, bot_id, headers, data):
    return client.post(access(bot_id) + "/preview", headers=headers, json=data).raise_for_status().json()


def test_inventory_upload_uses_bot_scope_and_keeps_other_observations(client, auth_header, admin_header):
    target = bot(client, auth_header, "inventory target")
    other = bot(client, auth_header, "other")
    key = bearer(credentials(client, auth_header, [target], [INVENTORY]).raise_for_status().json()["credential"])
    snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "host",
        "accelerators": [{"model": "Example Device", "count": 4, "memory_bytes_per_card": 96 * 2**30}],
        "memory_bytes": 512 * 2**30,
        "cpu_arch": "x86_64",
    }

    def proposal(node, record):
        return {"scope": INVENTORY, "configuration": {"version": 1, "nodes": {node: record}}}

    def upload(node, record):
        body = proposal(node, record)
        planned = preview(client, target, key, body)
        assert client.put(access(target), json={**body, "receipt": planned["receipt"]}, headers=key).status_code == 200

    assert client.get(access(other), headers=key).status_code == 403
    assert (
        client.post(access(target) + "/preview", headers=key, json={"scope": SOURCE, "configuration": None}).status_code
        == 403
    )
    assert client.post(access(target) + "/preview", headers=key, json=proposal("unknown", snapshot)).status_code == 422
    assert (
        client.post(
            access(target) + "/preview", headers=key, json=proposal("node1", {**snapshot, "private": 1})
        ).status_code
        == 422
    )
    upload("node1", snapshot)
    upload("node2", snapshot)
    upload("node1", {**snapshot, "cpu_arch": "ignored"})
    route = f"/api/bots/{target}/node-inventory"
    assert client.get(route, headers=admin_header).status_code == 403
    doc = client.get(route, headers=auth_header).raise_for_status().json()
    assert doc["node_ids"] == ["node1", "node2"]
    assert set(doc["nodes"]) == {"node1", "node2"}
    assert doc["nodes"]["node1"]["cpu_arch"] == "x86_64"
    assert client.get("/api/public-docs/configuration/AGENTS.md").raise_for_status().text.count(INVENTORY) == 2


def test_manual_node_json_saves_login_and_hardware_together(client, auth_header, admin_header):
    target = bot(client, auth_header, "combined settings")
    path = f"/api/bots/{target}/node-inventory"
    login = {
        "host": "node-01.example.test",
        "ip": "192.0.2.41",
        "port": 22,
        "method": "SSH via bastion",
        "note": "Use the assigned account",
    }
    hardware = [{"model": "Example Device", "count": 4}]
    mapping = {"version": 1, "nodes": {"node1": login | {"hardware": hardware}}}

    expected = {
        "changes": {
            "node1": {
                "before": {"login": None, "hardware": []},
                "after": {"login": login, "hardware": hardware},
            }
        }
    }
    assert client.post(path + "/preview", headers=auth_header, json=mapping).raise_for_status().json() == expected
    assert client.patch(path, headers=admin_header, json=mapping).status_code == 403
    invalid = {"version": 1, "nodes": {"node1": {"login": login, "hardware": [{"model": "bad", "count": 0}]}}}
    assert client.patch(path, headers=auth_header, json=invalid).status_code == 422
    mixed = {"version": 1, "nodes": {"node1": login | {"login": login}}}
    assert client.patch(path, headers=auth_header, json=mixed).status_code == 422
    assert client.get(path, headers=auth_header).raise_for_status().json()["login_addresses"] == {}
    assert client.patch(path, headers=auth_header, json=mapping).raise_for_status().json() == {"changed_nodes": 1}
    document = client.get(path, headers=auth_header).raise_for_status().json()
    assert document["login_addresses"] == {"node1": login}
    assert document["registered_hardware"] == {"node1": hardware}

    # Older address-only clients can update the address without dropping guidance.
    address = {"host": "node-01.example.test", "port": 2222}
    login_path = f"/api/bots/{target}/node-access"
    client.put(login_path, headers=auth_header, json={"version": 1, "nodes": {"node1": address}}).raise_for_status()
    assert client.get(login_path, headers=auth_header).json()["nodes"]["node1"] == address | {
        "method": login["method"],
        "note": login["note"],
    }

    # Clearing a note is explicit, and the remaining guidance is still preserved.
    clear_note = {"version": 1, "nodes": {"node1": {"login": address | {"note": None}}}}
    client.patch(path, headers=auth_header, json=clear_note).raise_for_status()
    assert client.get(login_path, headers=auth_header).json()["nodes"]["node1"] == address | {"method": login["method"]}

    removal = {"version": 1, "nodes": {"node1": {"host": None}}}
    client.patch(path, headers=auth_header, json=removal).raise_for_status()
    document = client.get(path, headers=auth_header).raise_for_status().json()
    assert document["login_addresses"] == {}
    assert document["registered_hardware"] == {"node1": hardware}


def test_retired_task_note_credentials_have_no_routes(client, admin_header):
    assert client.get("/api/admin/audit-credentials", headers=admin_header).status_code == 404
    assert client.post("/api/admin/audit-credentials", headers=admin_header, json={}).status_code in {404, 405}
    assert client.get("/api/audit/bots/1/task-notes", headers=admin_header).status_code == 404
    assert client.get("/api/audit/bots/1/agent-history", headers=admin_header).status_code == 404
    assert client.get("/api/audit/logs", headers=admin_header).status_code == 200


def test_existing_credentials_survive_the_additive_deletion_migration(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE configuration_credentials (id INTEGER PRIMARY KEY, public_id TEXT)"))
            conn.execute(text("INSERT INTO configuration_credentials VALUES (1, 'existing')"))
        monkeypatch.setattr(main, "engine", engine)
        main._migrate_configuration_credential_deletion()
        main._migrate_configuration_credential_deletion()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT public_id, deleted_at FROM configuration_credentials")).one() == (
                "existing",
                None,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_only_issuer_can_delete_inactive_record_without_erasing_audit(
    client, auth_header, admin_header, db_session, state
):
    first = bot(client, auth_header, "cleanup target")
    issued = credentials(client, auth_header, [first], [NODE]).raise_for_status().json()
    url = f"{ISSUE}/{issued['id']}"
    assert client.delete(url + "/record", headers=auth_header).status_code == 409
    if state == "revoked":
        assert client.delete(url, headers=auth_header).status_code == 204
    else:
        row = db_session.query(ConfigurationCredential).filter_by(public_id=issued["id"]).one()
        row.expires_at = _now() - timedelta(seconds=1)
        db_session.commit()
    assert client.delete(url + "/record", headers=admin_header).status_code == 404
    for _ in range(2):
        assert client.delete(url + "/record", headers=auth_header).status_code == 204
    assert client.get(ISSUE, headers=auth_header).raise_for_status().json()["items"] == []
    assert client.get(access(first), headers=bearer(issued["credential"])).status_code == 401
    db_session.expire_all()
    row = db_session.query(ConfigurationCredential).filter_by(public_id=issued["id"]).one()
    assert row.deleted_at is not None
    events = db_session.query(AuditLog).filter_by(target_name=issued["id"]).order_by(AuditLog.id).all()
    assert [event.action for event in events] == [
        "configuration_credential.create",
        *(["configuration_credential.revoke"] if state == "revoked" else []),
        "configuration_credential.delete",
    ]
    assert all(issued["credential"] not in (event.detail or "") for event in events)


def test_user_key_scopes_two_bots_and_rejects_stale_or_changed_proposals(client, auth_header, admin_header):
    first = bot(client, auth_header, "first")
    second = bot(client, auth_header, "second")
    outside = bot(client, auth_header, "outside")
    other_owner = bot(client, admin_header, "other-owner")
    assert credentials(client, auth_header, [other_owner], [NODE]).status_code == 403
    assert credentials(client, admin_header, [first], [NODE]).status_code == 403

    issued = credentials(client, auth_header, [first, second], [NODE]).raise_for_status().json()
    key = bearer(issued["credential"])
    assert issued["shown_once"] is True
    listing = client.get(ISSUE, headers=auth_header).raise_for_status()
    assert issued["credential"] not in listing.text
    assert listing.json()["bots"] == [
        {"id": first, "name": "first"},
        {"id": outside, "name": "outside"},
        {"id": second, "name": "second"},
    ]
    assert client.get(ISSUE, headers=admin_header).json()["items"] == []
    assert client.get(ISSUE, headers=key).status_code == 401
    assert client.get(ISSUE).status_code == 401
    assert client.get(access(first), headers=auth_header).status_code == 401
    assert client.get(access(first), headers=bearer("lbk_" + "a" * 48)).status_code == 401
    assert client.get(access(outside), headers=key).status_code == 403
    assert client.get(access(other_owner), headers=key).status_code == 403
    assert client.get(access(second), headers=key).raise_for_status().json()["node_ids"] == ["node1", "node2"]
    assert (
        client.post(access(first) + "/preview", headers=key, json={"scope": SOURCE, "configuration": None}).status_code
        == 403
    )

    change = {
        "scope": NODE,
        "configuration": {
            "version": 2,
            "nodes": {
                "node1": {"host": "compute.example.test", "ip": "192.0.2.42", "port": 2222},
            },
        },
    }
    proposal = preview(client, first, key, change)
    assert proposal["before"]["nodes"] == {} and proposal["changed"]
    assert client.get(f"/api/bots/{first}/node-access", headers=auth_header).json()["nodes"] == {}
    assert client.put(access(second), headers=key, json={**change, "receipt": proposal["receipt"]}).status_code == 409
    different = {"scope": NODE, "configuration": {"version": 1, "nodes": {"node2": {"host": "other.example.test"}}}}
    assert client.put(access(first), headers=key, json={**different, "receipt": proposal["receipt"]}).status_code == 409
    result = client.put(access(first), headers=key, json={**change, "receipt": proposal["receipt"]}).raise_for_status()
    assert result.json()["configuration"] == proposal["after"]
    assert client.put(access(first), headers=key, json={**change, "receipt": proposal["receipt"]}).status_code == 409

    second_change = preview(client, second, key, change)
    client.put(access(second), headers=key, json={**change, "receipt": second_change["receipt"]}).raise_for_status()
    assert client.get(f"/api/bots/{second}/node-access", headers=auth_header).json()["nodes"] == {
        "node1": {"host": "compute.example.test", "ip": "192.0.2.42", "port": 2222}
    }

    old_proposal = preview(client, first, key, different)
    client.put(
        f"/api/bots/{first}/node-access", headers=auth_header, json=different["configuration"]
    ).raise_for_status()
    assert (
        client.put(access(first), headers=key, json={**different, "receipt": old_proposal["receipt"]}).status_code
        == 409
    )
    assert client.get(ISSUE, headers=auth_header).json()["items"][0]["last_used_at"]


def test_datasource_validation_and_rotation_cannot_broaden_authority(client, auth_header, admin_header, db_session):
    target = bot(client, auth_header, "datasource")
    issued = credentials(client, auth_header, [target], [SOURCE]).raise_for_status().json()
    key = bearer(issued["credential"])
    path = access(target)
    config = {
        "endpoint": "https://metrics.example.test",
        "profile": "lockbot-v1",
        "nodeMapping": {"node1": "collector-label"},
        "accessMode": "short_lived",
    }
    change = {"scope": SOURCE, "configuration": config}
    for invalid in (
        {**config, "endpoint": "https://literal-secret@metrics.example.test"},
        {**config, "nodeMapping": {"unknown": "collector-label"}},
        {**config, "deviceMapping": {"node1:dev0": "wrong-type"}},
    ):
        response = client.post(path + "/preview", headers=key, json={**change, "configuration": invalid})
        assert response.status_code == 422 and "literal-secret" not in response.text
    proposal = preview(client, target, key, change)
    assert proposal["before"] is None and proposal["after"]["nodeMapping"] == config["nodeMapping"]
    rotated = (
        client.post(ISSUE + "/" + issued["id"] + "/rotate", headers=auth_header, json={}).raise_for_status().json()
    )
    assert client.get(path, headers=key).status_code == 401
    key = bearer(rotated["credential"])
    assert client.put(path, headers=key, json={**change, "receipt": proposal["receipt"]}).status_code == 409
    next_proposal = preview(client, target, key, change)
    assert (
        client.put(path, headers=key, json={**change, "receipt": next_proposal["receipt"]})
        .raise_for_status()
        .json()["changed"]
    )
    db_session.expire_all()
    assert db_session.get(Bot, target).utilization_config
    assert client.delete(ISSUE + "/" + issued["id"], headers=admin_header).status_code == 404
    assert client.delete(ISSUE + "/" + issued["id"], headers=auth_header).status_code == 204
    assert client.get(path, headers=key).status_code == 401


def test_configuration_key_reevaluates_issuer_role_ownership_and_expiration(
    client, auth_header, admin_header, db_session
):
    target = bot(client, auth_header, "transfer")
    issuer = db_session.query(User).filter_by(username="adminuser").one()
    issuer.role = "super_admin"
    db_session.commit()
    assert {"id": target, "name": "transfer"} in client.get(ISSUE, headers=admin_header).json()["bots"]
    issued = credentials(client, admin_header, [target], [NODE, SOURCE]).raise_for_status().json()
    key = bearer(issued["credential"])
    path = access(target)
    assert client.get(path, headers=key).status_code == 200
    issuer.role = "admin"
    db_session.commit()
    assert client.get(path, headers=key).status_code == 403

    issuer.role = "super_admin"
    db_session.get(Bot, target).user_id = issuer.id
    db_session.commit()
    assert client.get(path, headers=key).status_code == 403

    renewed = credentials(client, admin_header, [target], [NODE]).raise_for_status().json()
    renewed_key = bearer(renewed["credential"])
    assert client.get(path, headers=renewed_key).status_code == 200
    row = db_session.query(ConfigurationCredential).filter_by(public_id=renewed["id"]).one()
    row.expires_at = _now() - timedelta(seconds=1)
    db_session.commit()
    assert client.get(path, headers=renewed_key).status_code == 401

    fresh = credentials(client, admin_header, [target], [NODE]).raise_for_status().json()
    issuer.token_version += 1
    db_session.commit()
    assert client.get(path, headers=bearer(fresh["credential"])).status_code == 401


def test_public_schema_uses_dedicated_configuration_bearer(client):
    doc = client.get("/api/public-openapi.json").raise_for_status().json()
    assert doc["paths"]["/api/bots/{bot_id}/configuration-access"]["get"]["security"] == [{"ConfigurationKey": []}]
    assert doc["paths"]["/api/bots/{bot_id}/configuration-access/preview"]["post"]["security"] == [
        {"ConfigurationKey": []}
    ]
    assert "/api/configuration-credentials" not in doc["paths"]
