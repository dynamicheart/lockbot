# ruff: noqa: F811
"""Per-card ownership, FIFO workflows, tasks and OP scope through the real API routes."""

import copy
import json

import pytest
from lockbot.backend.app.resource_api.usage import UsageDeclaration

from backend.test_operator_api import operator  # noqa: F401
from backend.test_resource_api_resources import _clear_cache, api  # noqa: F401

pytestmark = pytest.mark.parametrize("api", ["QUEUE_DEVICE"], indirect=True)


@pytest.fixture
def cards(api, monkeypatch):
    for module in ("usage", "views", "operator"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", api["manager"])
    return api


def command(client, cards, text, other=False, **metadata):
    response = client.post(
        f"/api/bots/{cards['bot_id']}/commands",
        headers=cards["other_headers"] if other else cards["headers"],
        json={"command": text, **metadata},
    )
    assert response.status_code == 200, response.text
    return response.json()


def state(client, cards, other=False):
    response = client.get(
        f"/api/bots/{cards['bot_id']}/resources", headers=cards["other_headers"] if other else cards["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_card_discovery_and_same_person_independent_agent_tasks(client, cards):
    response = state(client, cards)
    assert response["bot_type"] == "QUEUE_DEVICE"
    assert "book" in response["supported_operations"] and "slock" not in response["supported_operations"]
    assert "dev0" in response["command_examples"]["book"]
    command(client, cards, "lock n1 dev0 1h", agent_label="dev-agent", purpose="Feature development")
    command(client, cards, "lock n1 dev1 30m", agent_label="test-agent", purpose="Regression")
    devices = state(client, cards)["resources"][0]["devices"]
    assert [d["resource_id"] for d in devices] == ["n1:dev0", "n1:dev1"]
    assert [d["holders"][0]["usage"]["purpose"] for d in devices] == ["Feature development", "Regression"]
    assert [d["holders"][0]["usage"]["agent_label"] for d in devices] == ["dev-agent", "test-agent"]
    command(client, cards, "free n1 dev0")
    devices = state(client, cards)["resources"][0]["devices"]
    assert not devices[0]["holders"] and devices[1]["holders"][0]["usage"]["purpose"] == "Regression"


def test_card_booking_notify_claim_carries_purpose_and_uses_claim_deadline(client, cards):
    command(client, cards, "lock n1 dev0 1h")
    command(client, cards, "book n1 dev0 30m", other=True, agent_label="queue-agent", purpose="After development")
    response = state(client, cards, other=True)
    assert response["next_event_kind"] == "node_frees"
    assert not response["resources"][0]["devices"][0]["claimable_by_you"]
    command(client, cards, "free n1 dev0")
    cards["bot"]._check_and_notify()
    response = state(client, cards, other=True)
    device = response["resources"][0]["devices"][0]
    assert response["next_event_kind"] == "claim_window_end"
    assert device["claimable_by_you"] and device["claim_command"] == "lock n1 dev0"
    assert device["booking_list"][0]["usage"]["purpose"] == "After development"
    assert not any(key.startswith("_") for key in device)
    command(client, cards, device["claim_command"], other=True)
    device = state(client, cards, other=True)["resources"][0]["devices"][0]
    assert device["holders"][0]["duration_seconds"] == 1800
    assert device["holders"][0]["usage"]["agent_label"] == "queue-agent"


def test_op_card_release_keeps_sibling_holding_and_waiting_task(client, cards, operator):
    command(client, cards, "lock n1 dev0,1 1h", purpose="Development")
    command(client, cards, "book n1 dev0 30m", other=True, purpose="Regression")
    base = f"/api/bots/{cards['bot_id']}"
    before = copy.deepcopy(cards["bot"].state.bot_state)
    bad = client.post(
        base + "/kicklock",
        headers=operator["op_headers"],
        json={"node_keys": ["n1:dev0", "n1:dev9"], "reason": "Maintenance"},
    )
    assert bad.status_code == 404 and cards["bot"].state.bot_state == before
    released = client.post(
        base + "/kicklock", headers=operator["op_headers"], json={"node_keys": ["n1:dev0"], "reason": "Maintenance"}
    )
    assert released.status_code == 200 and released.json()["freed"] == ["n1:dev0"]
    payload = client.get(base + "/op/resources", headers=operator["op_headers"]).json()
    devices = payload["resources"][0]["devices"]
    assert not devices[0]["holders"] and devices[0]["booking_list"][0]["usage"]["purpose"] == "Regression"
    assert devices[1]["holders"][0]["usage"]["purpose"] == "Development"
    assert all("claimable_by_you" not in device for device in devices)


def test_manual_declarations_require_exact_canonical_card_ownership(client, cards, db_session):
    command(client, cards, "lock n1 dev0 1h", purpose="Original")
    path = f"/api/bots/{cards['bot_id']}/usage-declarations/"
    for resource in ["n1:dev1", "n1:dev00", "n1:dev０"]:
        assert client.put(path + resource, headers=cards["headers"], json={"purpose": "Wrong"}).status_code == 409
    assert (
        client.put(path + "n1:dev0", headers=cards["other_headers"], json={"purpose": "Other user"}).status_code == 409
    )
    assert client.put(path + "n1:dev0", headers=cards["headers"], json={"purpose": "Updated"}).status_code == 200
    db_session.expire_all()
    assert db_session.query(UsageDeclaration).one().purpose == "Updated"


def test_card_readonly_view_includes_full_queue_and_omits_caller_fields(client, cards):
    from urllib.parse import parse_qs, urlsplit

    command(client, cards, "lock n1 dev0 1h", purpose="Develop")
    command(client, cards, "book n1 dev0 30m", other=True, purpose="Regression")
    context = client.get("/api/agent/context", headers=cards["headers"]).json()
    token = parse_qs(urlsplit(context["view_url"]).fragment.split("?", 1)[1])["view"][0]
    payload = client.get(f"/api/bot-views/{cards['bot_id']}/state", headers={"Authorization": "Bearer " + token})
    assert payload.status_code == 200, payload.text
    device = payload.json()["resources"][0]["devices"][0]
    assert device["booking_list"][0]["duration_seconds"] == 1800
    assert device["booking_list"][0]["usage"]["purpose"] == "Regression"
    assert "claimable_by_you" not in device and "binding_id" not in json.dumps(device)


def test_native_card_release_cleanup_keeps_sibling_task(client, cards, db_session, session_factory, monkeypatch):
    from lockbot.backend.app.resource_api import usage_cleanup

    monkeypatch.setattr(usage_cleanup, "SessionLocal", session_factory)
    monkeypatch.setattr(usage_cleanup, "bot_manager", cards["manager"])
    command(client, cards, "lock n1 dev0 1h", purpose="First")
    command(client, cards, "lock n1 dev1 1h", purpose="Second")
    cards["bot"].unlock("alice", "free n1 dev0")
    assert usage_cleanup.cleanup_usage_once()["removed"] == 1
    db_session.expire_all()
    row = db_session.query(UsageDeclaration).one()
    assert row.resource_id == "n1:dev1" and row.purpose == "Second"


def test_state_edit_normalizes_card_queues_without_changing_legacy_device_shape(cards):
    from lockbot.backend.app.bots.router import _default_state_for, _validate_and_align_state

    config = {"n1": ["GPU", "GPU"]}
    initial = _default_state_for(config, "QUEUE_DEVICE")
    assert all(card["booking_list"] == [] for card in initial["n1"])
    initial["n1"][0]["booking_list"] = [{"user_id": "bob", "start_time": 10, "duration": 600}]
    initial["n1"][1]["booking_list"] = "invalid"
    normalized, warnings = _validate_and_align_state(initial, "QUEUE_DEVICE", config)
    assert normalized["n1"][0]["booking_list"][0]["user_id"] == "bob"
    assert normalized["n1"][1]["booking_list"] == [] and warnings
    assert all("booking_list" not in card for card in _default_state_for(config, "DEVICE")["n1"])
