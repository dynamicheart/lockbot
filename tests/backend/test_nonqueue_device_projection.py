"""Match real DEVICE wire semantics without guessing missing queue evidence."""

# ruff: noqa: F811 -- imported fixture
import copy

import pytest
from lockbot.backend.app.agent_skill.scripts import lockbot_cli as cli

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["DEVICE", "QUEUE_DEVICE"], indirect=True)
@pytest.mark.parametrize("owned", [False, True])
def test_actual_api_device_state_and_own_filter(client, api, owned):
    if owned:
        response = client.post("/api/agent/commands", headers=api["headers"], json={"command": "lock n1 dev0 1h"})
        assert response.status_code == 200, response.text
    response = client.get("/api/agent/resources", headers=api["headers"])
    assert response.status_code == 200
    snapshot = response.json()
    context = snapshot["context"]
    assert snapshot["health"] == "running" and snapshot["age_seconds"] < 5
    queued = context["bot"]["type"] == "QUEUE_DEVICE"
    assert ("booking_list" in snapshot["resources"][0]["devices"][0]) == queued
    snapshot["snapshot_fresh_for_read"] = True
    projection = cli.device_summary_rows(snapshot, context, node="n1")
    expected_states = ["busy" if owned else "idle", "idle"]
    assert [row["state"] for row in projection["rows"]] == expected_states
    assert [row["held_by_you"] for row in projection["rows"]] == [owned, False]
    assert all(row["booked_by_you"] is False for row in projection["rows"])
    assert all(row["ownership_match_unknown"] is False for row in projection["rows"])
    assert projection["projection_complete"]
    mine = cli.device_summary_rows(snapshot, context, node="n1", mine=True)
    assert [row["id"] for row in mine["rows"]] == ([0] if owned else [])
    assert mine["unknown_ownership_devices"] == 0 and mine["projection_complete"]

    page = client.get(
        "/api/agent/resources/query",
        headers=api["headers"],
        params={"view": "devices", "node_id": "n1", "fields": "id,state,held_by_you,booked_by_you"},
    )
    assert page.status_code == 200, page.text
    assert [row["state"] for row in page.json()["rows"]] == expected_states
    assert page.json()["coverage"]["unknown_ownership_devices"] == 0


@pytest.mark.parametrize("kind", ["DEVICE", "QUEUE_DEVICE"])
@pytest.mark.parametrize("invalid", ["queue_missing", "queue_null", "queue_object", "holders_missing", "holders_null"])
def test_only_absent_queue_in_nonqueued_mode_has_structural_empty_semantics(kind, invalid):
    context = {"bot": {"type": kind}, "actor": {"platform": "infoflow", "external_user_id": "alice"}}
    card = {"id": 0, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
    if invalid.endswith("missing"):
        card.pop("booking_list" if invalid.startswith("queue") else "holders")
    else:
        card["booking_list" if invalid.startswith("queue") else "holders"] = {} if invalid == "queue_object" else None
    snapshot = {
        "health": "running",
        "snapshot_fresh_for_read": True,
        "resources": [{"id": "node-a", "devices": [card]}],
    }
    original = copy.deepcopy(snapshot)
    result = cli.device_summary_rows(snapshot, context, node="node-a")
    valid_absence = kind == "DEVICE" and invalid == "queue_missing"
    row = result["rows"][0]
    assert row["state"] == ("idle" if valid_absence else "unknown")
    assert row["held_by_you"] is (False if valid_absence else None)
    assert row["booked_by_you"] is (False if valid_absence else None)
    assert row["ownership_match_unknown"] is not valid_absence
    assert result["projection_complete"] is valid_absence
    assert snapshot == original
