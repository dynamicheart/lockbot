"""Bounded personal records preserve coverage, zero IDs and queue semantics."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_summary_cli import execute


def actor_rows(state, count=1):
    now = datetime.now(timezone.utc)
    return [
        {
            "external_user_id": state.actor["external_user_id"],
            "platform": state.actor["platform"],
            "remaining_seconds": 7200,
            "expires_at": (now + timedelta(hours=2)).isoformat(),
        }
        for _ in range(count)
    ]


def test_many_owned_and_booked_cards_are_bounded_without_disclosing_peer_records(aggregate):
    state = aggregate.server(1)
    state.bot_type = "QUEUE_DEVICE"
    aggregate.record(state, "scope")
    mine = actor_rows(state)[0]
    private_text = "peer-task-information-must-not-be-returned"
    devices = [
        {
            "id": index * 2,
            "dev_model": "P800",
            "lock_status": "shared",
            "holders": [mine, {"external_user_id": "peer-user", "purpose": private_text}],
            "booking_list": [{"external_user_id": "peer-user"}, {**mine, "position": 1}],
            "claimable_by_you": False,
        }
        for index in range(80)
    ]
    state.extra["resources"] = [{"id": "node07", "alias": "19", "devices": devices}]
    output, process = execute(aggregate, "resources-all", "--mine", "--summary")
    result = output["result"]
    assert result["view"] == "own" and result["scope_complete"]
    row = result["rows"][0]
    assert row["holding_units"] == row["booking_units"] == 80
    assert len(row["holdings"]) == len(row["bookings"]) == 20
    assert row["holdings_has_more"] and row["bookings_has_more"]
    assert any(child["device"] == 0 for child in row["holdings"])
    assert all(child["mode"] == "shared" and child["remaining_time"] == "2h" for child in row["holdings"])
    assert all(child["queue_rank"] == 2 and child["claimable_by_you"] is False for child in row["bookings"])
    assert all("mode" not in child and "remaining_time" not in child for child in row["bookings"])
    assert "peer-user" not in process.stdout and private_text not in process.stdout
    assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT and len(state.events) == 2


def test_whole_node_dev0_and_same_name_other_scope_keep_distinct_references(aggregate):
    states = [aggregate.server(index) for index in (1, 2)]
    for index, state in enumerate(states):
        aggregate.record(state, str(index))
        state.extra["resources"] = [
            {"id": "dev0", "lock_status": "exclusive", "holders": actor_rows(state), "booking_list": []}
        ]
    result = execute(aggregate, "resources-all", "--mine", "--json", "node,unit,holding_units,holdings")[0]["result"]
    assert result["scope_complete"] and len(result["rows"]) == 2
    assert len({row["ref"] for row in result["rows"]}) == 2
    assert all(row["unit"] == "whole_node" and row["holdings"][0]["device"] is None for row in result["rows"])


@pytest.mark.parametrize("missing", ["queue", "owner", "platform", "duplicate_owner"])
def test_unknown_owner_or_queue_cannot_prove_no_personal_bookings(aggregate, missing):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    node = {"id": "node07", "lock_status": "exclusive", "holders": [{"external_user_id": "peer"}], "booking_list": []}
    if missing == "queue":
        node.pop("booking_list")
    elif missing == "owner":
        node["holders"] = [{}]
    elif missing == "platform":
        node["holders"][0]["platform"] = None
    else:
        node["lock_status"] = "shared"
        node["holders"] *= 2
    state.extra["resources"] = [node]
    result = execute(aggregate, "resources-all", "--mine", "--summary")[0]["result"]
    assert result["rows"] == [] and not result["scope_complete"]
    assert result["unknown_ownership_units"] == 1 and result["matched_nodes"] is None


def test_missing_booking_details_remain_unknown_in_own_booking(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [
        {"id": "node07", "lock_status": "idle", "holders": [], "booking_list": actor_rows(state)}
    ]
    result = execute(aggregate, "resources-all", "--mine", "--summary")[0]["result"]
    row = result["rows"][0]
    assert row["holding_units"] == 0 and row["booking_units"] == 1
    assert row["bookings"][0]["queue_rank"] is None and row["bookings"][0]["claimable_by_you"] is None
    assert "mode" not in row["bookings"][0] and "expires_at" not in row["bookings"][0]


def test_model_filter_is_applied_to_owned_cards_and_exact_times_remain_opt_in(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    path = aggregate.record(state, "scope")
    state.extra["resources"] = [
        {
            "id": "node07",
            "devices": [
                {"id": index, "dev_model": model, "lock_status": "exclusive", "holders": actor_rows(state)}
                for index, model in enumerate(("P800", "H20", "P800"))
            ],
        }
    ]
    result = execute(
        aggregate, "--connection", path, "resources", "--mine", "--model", "P800", "--summary", "--exact-lease-times"
    )[0]["result"]
    row = result["rows"][0]
    assert row["holding_units"] == 2 and {child["device"] for child in row["holdings"]} == {0, 2}
    assert all(child["remaining_seconds"] == 7200 and "remaining_time" not in child for child in row["holdings"])
    assert result["view"] == "own" and result["scope_complete"]


def test_raw_duplicate_node_identity_cannot_be_hidden_by_own_model_filter(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [
        {
            "id": "node07",
            "devices": [{"id": 0, "dev_model": model, "lock_status": "exclusive", "holders": actor_rows(state)}],
        }
        for model in ("P800", "H20")
    ]
    result = execute(aggregate, "resources-all", "--mine", "--model", "P800", "--summary")[0]["result"]
    assert result["rows"] == [] and not result["scope_complete"] and result["unidentified_nodes"] == 2


def test_empty_stale_ownership_observation_does_not_confirm_zero_resources():
    context = {
        "server": {"instance_id": "synthetic-instance"},
        "bot": {"uuid": "synthetic-bot", "type": "QUEUE"},
        "actor": {"platform": "infoflow", "external_user_id": "synthetic-user"},
    }
    snapshot = {
        "context": context,
        "scope": "own",
        "health": "running",
        "age_seconds": 30,
        "snapshot_fresh_for_write": False,
        "holdings": [],
        "bookings": [],
        "ownership_coverage": {"complete": True, "unknown_units": 0, "unidentified_nodes": 0},
    }
    result = json.loads(helper.query_summary_output({"result": snapshot, "_approved_origin": "http://127.0.0.1:8155"}))[
        "result"
    ]
    assert result["rows"] == [] and result["scope_complete"] is False


def test_duplicate_owned_device_ids_are_not_made_unique_by_filter():
    snapshot = {
        "scope": "own",
        "health": "running",
        "snapshot_fresh_for_write": True,
        "context": {"bot": {"type": "DEVICE"}},
        "ownership_coverage": {"complete": True},
        "holdings": [
            {"node": "node07", "allocation": "card", "device": device, "model": model, "mode": "exclusive"}
            for device, model in ((0, "P800"), ("0", "H20"))
        ],
        "bookings": [],
    }
    result = helper.own_summary_rows(snapshot, reference_factory=lambda node: "ref/" + node, model="P800")
    assert result["rows"] == [] and result["projection_complete"] is False
    assert result["unknown_ownership_units"] == 2


def test_owned_vs_empty_same_scope_observations_cannot_confirm_an_owned_set(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "second" * 8
    aggregate.record(state, "second")
    second_key = state.key
    state.accepted_keys, state.key = [second_key], original
    node = {"id": "node07", "lock_status": "exclusive", "holders": actor_rows(state), "booking_list": []}
    state.extra["resources"] = [node]
    state.extra_per_key = {"Bearer " + second_key: {"resources": [{**node, "lock_status": "idle", "holders": []}]}}
    result = execute(aggregate, "resources-all", "--mine", "--summary")[0]["result"]
    assert not result["scope_complete"] and result["conflicting_scopes"] == 1
    assert set(result["rows"][0]) == {"ref", "node", "observation_conflict"}


def test_empty_unknown_allocation_type_cannot_confirm_zero_ownership(aggregate):
    state = aggregate.server(1)
    state.bot_type = "NEW_KIND"
    aggregate.record(state, "scope")
    state.extra["resources"] = []
    result = execute(aggregate, "resources-all", "--mine", "--summary")[0]["result"]
    assert result["rows"] == [] and not result["scope_complete"]
    assert result["unknown_ownership_units"] == 1


@pytest.mark.parametrize("matching_scope", [True, False])
def test_own_model_filter_uses_only_a_scope_matched_local_hardware_note(aggregate, matching_scope):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    state.extra["resources"] = [
        {"id": "dev0", "lock_status": "exclusive", "holders": actor_rows(state), "booking_list": []}
    ]
    notes = {
        "version": 1,
        "scope": {
            "approved_origin": state.origin,
            "instance_id": state.instance,
            "bot_uuid": state.bot_uuid if matching_scope else "another-bot",
        },
        "nodes": {"dev0": {"source": "synthetic registration", "accelerators": [{"model": "P800", "count": 8}]}},
    }
    sidecar = path.with_name(path.name + ".node-hardware.json")
    sidecar.write_text(json.dumps(notes))
    sidecar.chmod(0o600)
    result = execute(aggregate, "resources-all", "--mine", "--model", "P800", "--summary")[0]["result"]
    if matching_scope:
        assert result["rows"][0]["node"] == "dev0" and result["rows"][0]["holding_units"] == 1
    else:
        assert result["rows"] == [] and result["unknown_hardware_nodes"] == 1
    assert len(state.events) == 2
