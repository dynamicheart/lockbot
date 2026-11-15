"""Service-level coverage of user-issued, operation-scoped configuration credentials."""

from datetime import timedelta

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.delegation import ConfigurationCredential, _now
from lockbot.backend.app.bots.models import Bot

from backend.test_bots import _sample_bot

ISSUE = "/api/configuration-credentials"
NODE = "node_access:write"
SOURCE = "datasource:write"


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
    assert issued["credential"] not in client.get(ISSUE, headers=auth_header).text
    assert client.get(ISSUE, headers=admin_header).json()["items"] == []
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
                "node1": {"host": "compute.example.test", "ipv4": "192.0.2.42", "port": 2222},
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
