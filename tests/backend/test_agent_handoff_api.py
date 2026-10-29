"""Run the reference client gate at an actual in-process HTTP serialization boundary."""

# ruff: noqa: F811
import copy
import json

import pytest
from lockbot.agent_client.handoff import GateError, final_command_payload, make_handoff

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "QUEUE_DEVICE"], indirect=True)
@pytest.mark.parametrize("mode", ["command_only", "public_category", "private_details"])
def test_authenticated_handoff_first_http_operation(client, api, monkeypatch, mode):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    origin, reference = str(client.base_url).rstrip("/"), "keyring:fixture-resource-group"
    context_response = client.get("/api/agent/context", headers=api["headers"])
    assert context_response.status_code == 200
    context = context_response.json()
    trusted = make_handoff(origin, context, reference, {"version": 1, "mode": mode})
    child_copy = json.loads(json.dumps(trusted))
    target = "n1 dev0" if context["bot"]["type"] == "QUEUE_DEVICE" else "n1"
    candidate = {
        "command": f"lock {target} 5m",
        "purpose": "fictional-private-purpose",
        "agent_label": "fictional-private-agent",
        "task_category": "unit_testing",
        "arbitrary_child_field": "must-not-be-sent",
    }
    fresh = client.get("/api/agent/context", headers=api["headers"]).json()
    payload = final_command_payload(
        trusted,
        child_copy,
        origin=origin,
        authenticated_context=fresh,
        credential_ref=reference,
        candidate=candidate,
    )
    # This exact encoded result is the HTTP body: no merging of child fields afterward.
    encoded = json.dumps(payload).encode()
    assert b"arbitrary_child_field" not in encoded
    assert (b"fictional-private-purpose" in encoded) == (mode == "private_details")
    assert (b"task_category" in encoded) == (mode == "public_category")
    response = client.post(
        fresh["links"]["commands"],
        content=encoded,
        headers={**api["headers"], "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    node = api["bot"].state.bot_state["n1"]
    if isinstance(node, list):
        node = node[0]
    assert len(node["current_users"]) == 1
    assert node["current_users"][0]["user_id"] == "alice"
    for headers in [api["headers"], api["other_headers"]]:
        own = headers is api["headers"]
        snapshot = client.get(fresh["links"]["resources"], headers=headers)
        assert snapshot.status_code == 200
        assert ("fictional-private-purpose" in snapshot.text) == (own and mode == "private_details")
        assert ("fictional-private-agent" in snapshot.text) == (own and mode == "private_details")
        assert ("unit_testing" in snapshot.text) == (mode == "public_category")


def test_changed_authenticated_actor_stops_before_http_mutation(client, api):
    origin = str(client.base_url).rstrip("/")
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    trusted = make_handoff(origin, context, "keyring:alice")
    other_context = client.get("/api/agent/context", headers=api["other_headers"]).json()
    with pytest.raises(GateError, match="scope changed"):
        final_command_payload(
            trusted,
            copy.deepcopy(trusted),
            origin=origin,
            authenticated_context=other_context,
            credential_ref="keyring:alice",
            candidate={"command": "lock n1 5m"},
        )
    assert not api["bot"].state.bot_state["n1"]["current_users"]
