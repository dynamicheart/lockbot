# ruff: noqa: F811
"""Private node access settings and projections use the stable resource ID."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from lockbot.backend.app.audit.models import AuditLog

from backend.test_resource_api_resources import _get, _hold, _persist, api  # noqa: F401

HOST = "ssh-host.example.invalid"


def _routes(api):
    bot = f"/api/bots/{api['bot_id']}"
    return bot, bot + "/node-access"


def _mapping(**nodes):
    return {"version": 1, "nodes": nodes}


def test_preview_patch_merge_and_remove_are_private(client, api, admin_header, db_session, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.views.bot_manager", api["manager"])
    bot, path = _routes(api)
    owner = api["auth_header"]
    assert client.get(path, headers=api["headers"]).status_code == 401
    assert client.get(path, headers=admin_header).status_code == 403
    assert client.put(path, headers=admin_header, json=_mapping(n1={"host": HOST})).status_code == 403
    assert client.get(path, headers=owner).json() == {"version": 1, "node_ids": ["n1", "n2"], "nodes": {}}
    data = _mapping(n1={"host": HOST.upper()}, n2={"host": "2001:0db8::0001", "port": 2222})
    preview = client.post(path + "/preview", headers=owner, json=data)
    assert preview.headers["Cache-Control"] == "no-store"
    assert preview.json() == {
        "version": 1,
        "added": ["n1", "n2"],
        "changed": [],
        "unchanged": [],
        "nodes": {"n1": {"host": HOST, "port": 22}, "n2": {"host": "2001:db8::1", "port": 2222}},
    }
    assert client.get(path, headers=owner).json()["nodes"] == {}
    assert client.put(path, headers=owner, json=data).json()["nodes"] == preview.json()["nodes"]
    update = _mapping(n2={"host": "192.0.2.20"})
    assert client.post(path + "/preview", headers=owner, json=update).json()["changed"] == ["n2"]
    assert client.put(path, headers=owner, json=update).json()["nodes"] == {
        "n1": {"host": HOST, "port": 22},
        "n2": {"host": "192.0.2.20", "port": 22},
    }
    assert HOST not in client.get(bot, headers=owner).text
    view = client.get("/api/agent/context", headers=api["headers"]).json()["view_url"]
    view_code = urlsplit(view).fragment.rsplit("/", 1)[1]
    share = client.get(f"/api/bot-views/{api['bot_id']}/state", headers={"Authorization": "Bearer " + view_code})
    assert share.status_code == 200
    assert {node["id"]: node["node_access"] for node in share.json()["resources"]} == {
        "n1": {"host": HOST, "port": 22},
        "n2": {"host": "192.0.2.20", "port": 22},
    }
    assert client.delete(path + "/n1", headers=owner).status_code == 204
    assert client.get(path, headers=owner).json()["nodes"] == {"n2": {"host": "192.0.2.20", "port": 22}}
    assert all(HOST not in (row.detail or "") for row in db_session.query(AuditLog))


def test_shared_view_exposes_only_registered_addresses_with_a_valid_link(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.views.bot_manager", api["manager"])
    _, path = _routes(api)
    address = {"host": HOST, "ip": "192.0.2.41", "port": 2222}
    assert (
        client.put(
            path,
            headers=api["auth_header"],
            json={"version": 2, "nodes": {"n1": {**address, "method": "internal-method", "note": "internal-note"}}},
        ).status_code
        == 200
    )
    share_path = f"/api/bot-views/{api['bot_id']}/state"
    assert client.get(share_path).status_code == 403
    share_url = client.get("/api/agent/context", headers=api["headers"]).json()["view_url"]
    code = urlsplit(share_url).fragment.rsplit("/", 1)[1]
    response = client.get(share_path, headers={"Authorization": f"Bearer {code}"}).raise_for_status()
    resources = {node["id"]: node for node in response.json()["resources"]}
    assert resources["n1"]["node_access"] == address
    assert "node_access" not in resources["n2"]
    assert "internal-method" not in response.text and "internal-note" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":1,"nodes":{"n1":{"host":"a"}},"nodes":{}}',
        '{"version":1,"nodes":{"n1":{"host":"a"},"n1":{"host":"b"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","port":22,"port":2222}}}',
        '{"version":true,"nodes":{"n1":{"host":"a"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","port":true}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","port":0}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","port":65536}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","port":"22"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","password":"ignored"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a","ip":"192.0.2.42"}}}',
        '{"version":2,"nodes":{"n1":{"host":"192.0.2.41","ip":"192.0.2.42"}}}',
        '{"version":2,"nodes":{"n1":{"host":"a","ip":"2001:db8::1"}}}',
        '{"version":2,"nodes":{"n1":{"host":"a","ip":42}}}',
        '{"version":2,"nodes":{"n1":{"host":"a","ipv4":"192.0.2.42"}}}',
        '{"version":2,"nodes":{"n1":{"host":"a","ip":"192.0.2.42","ip":"192.0.2.43"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a; id"}}}',
        '{"version":1,"nodes":{"n1":{"host":"https://compute.example.invalid/"}}}',
        '{"version":1,"nodes":{"n1":{"host":"admin@compute.example.invalid"}}}',
        '{"version":1,"nodes":{"n1":{"host":"999.001.0.0"}}}',
        '{"version":1,"nodes":{"n1":{"host":"2001:db8::1%eth0"}}}',
        '{"version":1,"nodes":{"n1":{"host":"a"},"unknown":{"host":"b"}}}',
    ],
)
def test_invalid_import_is_atomic(client, api, raw):
    _, path = _routes(api)
    owner = api["auth_header"]
    before = _mapping(n1={"host": HOST, "port": 22})
    assert client.put(path, headers=owner, json=before).status_code == 200
    headers = {**owner, "Content-Type": "application/json"}
    assert client.post(path + "/preview", headers=headers, content=raw).status_code == 422
    assert client.put(path, headers=headers, content=raw).status_code == 422
    assert client.get(path, headers=owner).json()["nodes"] == before["nodes"]


def test_removed_inventory_cannot_reattach_saved_address(client, api):
    bot, path = _routes(api)
    owner = api["auth_header"]
    data = _mapping(n1={"host": HOST})
    assert client.post(path + "/preview", headers=owner, json=data).json()["added"] == ["n1"]
    assert client.put(path, headers=owner, json=data).status_code == 200
    assert client.put(bot, headers=owner, json={"cluster_configs": ["n2"]}).status_code == 200
    assert client.put(path, headers=owner, json=data).status_code == 422
    assert client.put(bot, headers=owner, json={"cluster_configs": ["n1", "n2"]}).status_code == 200
    assert client.get(path, headers=owner).json()["nodes"] == {}


def test_holder_access_checks_the_latest_committed_snapshot(client, api):
    _, path = _routes(api)
    assert client.put(path, headers=api["auth_header"], json=_mapping(n1={"host": HOST})).status_code == 200
    assert HOST not in _get(client, api).text
    _hold(api, "n1", "alice")
    assert HOST not in _get(client, api, api["other_headers"]).text
    active = _get(client, api)
    assert active.headers["Cache-Control"] == "no-store"
    assert active.json()["resources"][0]["node_access"] == {"host": HOST, "port": 22}
    api["bot"].state.bot_state["n1"]["current_users"].clear()
    _persist(api)
    assert HOST not in _get(client, api).text
    _hold(api, "n1", "alice", duration=1, started=int(datetime.now(timezone.utc).timestamp()) - 10)
    assert HOST not in _get(client, api).text


def test_changed_snapshot_does_not_disclose_address_from_old_holding(client, api, monkeypatch):
    from lockbot.backend.app.resource_api import resources

    _, path = _routes(api)
    assert client.put(path, headers=api["auth_header"], json=_mapping(n1={"host": HOST})).status_code == 200
    api["bot"].lock("alice", "lock n1 10m")
    project = resources.build_resources

    def project_then_release(**kwargs):
        result = project(**kwargs)
        api["bot"].unlock("alice", "free n1")
        return result

    monkeypatch.setattr(resources, "build_resources", project_then_release)
    response = _get(client, api)
    assert response.status_code == 200
    assert response.json()["resources"][0]["holders"][0]["external_user_id"] == "alice"
    assert HOST not in response.text


def test_dual_address_document_preserves_omitted_legacy_nodes(client, api):
    _, path = _routes(api)
    owner = api["auth_header"]
    legacy = {"host": "2001:db8::1", "port": 22}
    assert client.put(path, headers=owner, json=_mapping(n2=legacy)).status_code == 200
    dual = {"host": HOST, "ip": "192.0.2.42", "port": 2222}
    data = {"version": 2, "nodes": {"n1": {**dual, "host": HOST.upper()}}}
    preview = client.post(path + "/preview", headers=owner, json=data).json()
    assert preview["version"] == 2 and preview["nodes"] == {"n1": dual}
    assert client.get(path, headers=owner).json()["nodes"] == {"n2": legacy}
    document = client.put(path, headers=owner, json=data).json()
    assert document == client.get(path, headers=owner).json()
    assert document["version"] == 2 and document["nodes"] == {"n1": dual, "n2": legacy}
    assert client.post(path + "/preview", headers=owner, json=data).json()["unchanged"] == ["n1"]
    assert client.delete(path + "/n1", headers=owner).status_code == 204
    assert client.get(path, headers=owner).json()["version"] == 1


@pytest.mark.parametrize("api", ["DEVICE", "QUEUE_DEVICE"], indirect=True)
def test_agent_projects_only_held_cards(client, api):
    _, path = _routes(api)
    address = {"host": HOST, "ip": "192.0.2.42", "port": 22}
    mapping = {"version": 2, "nodes": {"n1": address}}
    assert client.put(path, headers=api["auth_header"], json=mapping).status_code == 200
    cards = api["bot"].state.bot_state["n1"]
    cards[0]["current_users"].append(
        {"user_id": "alice", "start_time": int(datetime.now(timezone.utc).timestamp()), "duration": 600}
    )
    if "booking_list" in cards[1]:
        cards[1]["booking_list"].append({"user_id": "alice", "start_time": 1, "duration": 600})
    _persist(api)
    snapshot = _get(client, api).json()["resources"][0]
    assert "node_access" not in snapshot
    assert snapshot["devices"][0]["node_access"] == address
    assert "node_access" not in snapshot["devices"][1]
    other = _get(client, api, api["other_headers"]).text
    assert HOST not in other and address["ip"] not in other
