# ruff: noqa: F811
"""Registered hardware stays separate from allocator state and addresses."""

from urllib.parse import urlsplit

import pytest
from lockbot.backend.app import main
from lockbot.backend.app.resource_auth import service
from sqlalchemy import create_engine, inspect, text

from backend.test_resource_api_resources import ACTOR, PLATFORM, _get, _hold, api  # noqa: F401

HARDWARE = [{"model": "P900", "count": 2}, {"model": "P800", "count": 6}]


def mapping(**nodes):
    return {"version": 1, "nodes": nodes}


def test_startup_migrations_preserve_existing_node_access(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(main, "engine", engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bots (id INTEGER PRIMARY KEY, node_access_config TEXT)"))
        conn.execute(text("INSERT INTO bots VALUES (1, :config)"), {"config": '{"version":1}'})
        conn.execute(text("CREATE TABLE configuration_credentials (id INTEGER PRIMARY KEY)"))

    for _ in range(2):
        main._migrate_bots_node_metadata()
        main._migrate_configuration_credential_deletion()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT node_access_config, node_hardware_config FROM bots")).one() == (
            '{"version":1}',
            None,
        )
    assert "deleted_at" in {column["name"] for column in inspect(engine).get_columns("configuration_credentials")}


@pytest.mark.parametrize("api", ["NODE", "QUEUE"], indirect=True)
def test_scoped_whole_machine_inventory(client, api, admin_header):
    bot = f"/api/bots/{api['bot_id']}"
    path = bot + "/node-hardware"
    owner = api["auth_header"]
    assert client.get(path, headers=api["headers"]).status_code == 401
    assert client.patch(path, headers=admin_header, json=mapping(n1=HARDWARE)).status_code == 403
    assert client.get(path, headers=owner).json()["nodes"] == {}
    before = _get(client, api).json()["resources"][0]
    assert "accelerators" not in before

    preview = client.post(path + "/preview", headers=owner, json=mapping(n1=HARDWARE))
    assert preview.json() == {"changes": {"n1": HARDWARE}}
    assert client.get(path, headers=owner).json()["nodes"] == {}

    update = client.patch(path, headers=owner, json=mapping(n1=HARDWARE))
    assert update.status_code == 200
    assert update.json()["nodes"] == {"n1": HARDWARE}
    response = _get(client, api).json()["resources"][0]
    assert response["accelerators"] == HARDWARE
    assert response["lock_status"] == before["lock_status"]
    assert response["holders"] == before["holders"]

    view_url = client.get("/api/agent/context", headers=api["headers"]).json()["view_url"]
    code = urlsplit(view_url).fragment.rsplit("/", 1)[1]
    shared = client.get(f"/api/bot-views/{api['bot_id']}/state", headers={"Authorization": f"Bearer {code}"})
    assert shared.status_code == 200 and "accelerators" not in shared.text and "P900" not in shared.text

    assert client.patch(path, headers=owner, json=mapping(n2=[{"model": "P800", "count": 4}])).status_code == 200
    assert client.patch(path, headers=owner, json=mapping(n1=[])).json()["nodes"] == {
        "n2": [{"model": "P800", "count": 4}]
    }


def test_matching_node_ids_keep_inventory_and_availability_separate_across_bots(client, api, tmp_path):
    owner = api["auth_header"]
    first = f"/api/bots/{api['bot_id']}"
    cluster = {"n1": "same-node"}
    created = client.post(
        "/api/bots",
        headers=owner,
        json={
            "name": "second-resource-group",
            "bot_type": "QUEUE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": cluster,
        },
    ).raise_for_status()
    second_id = created.json()["id"]
    second = f"/api/bots/{second_id}"
    client.put(second + "/resource-auth", headers=owner, json={"enabled": True}).raise_for_status()
    issued = client.post("/api/resource-auth/bindings", json={"label": "second"}).raise_for_status().json()
    service.handle_chat_binding(
        platform=PLATFORM, external_user_id=ACTOR, bot_id=second_id, action="bind", argument=issued["bind_code"]
    )
    second_headers = {"Authorization": f"Bearer {issued['credential']}"}
    api["manager"].start_bot(
        second_id,
        {
            "BOT_TYPE": "QUEUE",
            "BOT_ID": second_id,
            "BOT_NAME": "second-resource-group",
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": cluster,
        },
    )
    p800 = [{"model": "P800", "count": 8}]
    p900 = [{"model": "P900", "count": 4}]
    client.patch(first + "/node-hardware", headers=owner, json=mapping(n1=p800)).raise_for_status()
    client.patch(second + "/node-hardware", headers=owner, json=mapping(n1=p900)).raise_for_status()
    _hold(api, "n1", ACTOR)

    first_snapshot = _get(client, api).raise_for_status().json()
    second_snapshot = client.get(second + "/resources", headers=second_headers).raise_for_status().json()
    first_node = next(node for node in first_snapshot["resources"] if node["id"] == "n1")
    (second_node,) = second_snapshot["resources"]
    assert (first_node["accelerators"], first_node["lock_status"]) == (p800, "exclusive")
    assert (second_node["id"], second_node["accelerators"], second_node["lock_status"]) == ("n1", p900, "idle")
    assert client.get(second + "/resources", headers=api["headers"]).status_code == 401
    assert client.get(first + "/resources", headers=second_headers).status_code == 401

    client.patch(first + "/node-hardware", headers=owner, json=mapping(n1=[])).raise_for_status()
    first_after = _get(client, api).raise_for_status().json()
    first_node_after = next(node for node in first_after["resources"] if node["id"] == "n1")
    assert "accelerators" not in first_node_after
    assert first_node_after["lock_status"] == first_node["lock_status"]
    (holder_before,) = first_node["holders"]
    (holder_after,) = first_node_after["holders"]
    assert {k: v for k, v in holder_after.items() if k != "remaining_seconds"} == {
        k: v for k, v in holder_before.items() if k != "remaining_seconds"
    }
    assert 0 < holder_after["remaining_seconds"] <= holder_before["remaining_seconds"]
    (second_node_after,) = (
        client.get(second + "/resources", headers=second_headers).raise_for_status().json()["resources"]
    )
    assert second_node_after["accelerators"] == p900


@pytest.mark.parametrize(
    "invalid",
    [
        mapping(unknown=HARDWARE, n1=[]),
        mapping(n1=[{"model": "P800", "count": True}]),
        mapping(n1=[{"model": "P800", "count": 0}]),
        mapping(n1=[{"model": "P800", "count": 6}, {"model": "p800", "count": 2}]),
        mapping(n1=[{"model": "P900", "count": 2, "ip": "192.0.2.11"}]),
        {"version": True, "nodes": {"n1": HARDWARE}},
    ],
)
def test_invalid_update_is_atomic(client, api, invalid):
    path = f"/api/bots/{api['bot_id']}/node-hardware"
    owner = api["auth_header"]
    assert client.patch(path, headers=owner, json=mapping(n1=HARDWARE)).status_code == 200
    assert client.patch(path, headers=owner, json=invalid).status_code == 422
    assert client.get(path, headers=owner).json()["nodes"] == {"n1": HARDWARE}


def test_removed_node_does_not_inherit_prior_inventory(client, api):
    bot = f"/api/bots/{api['bot_id']}"
    path = bot + "/node-hardware"
    owner = api["auth_header"]
    assert client.patch(path, headers=owner, json=mapping(n1=HARDWARE)).status_code == 200
    assert client.put(bot, headers=owner, json={"cluster_configs": ["n2"]}).status_code == 200
    assert client.put(bot, headers=owner, json={"cluster_configs": ["n1", "n2"]}).status_code == 200
    assert client.get(path, headers=owner).json()["nodes"] == {}


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_card_groups_use_their_existing_device_inventory(client, api):
    path = f"/api/bots/{api['bot_id']}/node-hardware"
    assert client.patch(path, headers=api["auth_header"], json=mapping(n1=HARDWARE)).status_code == 409


def test_duplicate_node_json_is_rejected_without_saving(client, api):
    path = f"/api/bots/{api['bot_id']}/node-hardware"
    owner = api["auth_header"]
    raw = '{"version":1,"nodes":{"n1":[{"model":"P900","count":2}],"n1":[]}}'
    headers = {**owner, "Content-Type": "application/json"}
    assert client.post(path + "/preview", headers=headers, content=raw).status_code == 422
    assert client.patch(path, headers=headers, content=raw).status_code == 422
    assert client.get(path, headers=owner).json()["nodes"] == {}
