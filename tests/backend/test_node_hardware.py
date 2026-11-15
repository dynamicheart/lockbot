# ruff: noqa: F811
"""Registered hardware stays separate from allocator state and addresses."""

from urllib.parse import urlsplit

import pytest

from backend.test_resource_api_resources import _get, api  # noqa: F401

HARDWARE = [{"model": "P900", "count": 2}, {"model": "P800", "count": 6}]


def mapping(**nodes):
    return {"version": 1, "nodes": nodes}


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
