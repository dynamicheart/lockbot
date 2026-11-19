"""Opt-in node summaries and exact reference reads through the actual SDK process."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import os
import subprocess
import sys

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def cards(count=8):
    return [
        {"id": number * 2, "dev_model": "P800" if number % 2 else "H20", "lock_status": "idle", "holders": []}
        for number in range(count)
    ]


def execute(aggregate, *args, code=0):
    process = subprocess.run(
        [sys.executable, "-S", str(aggregate.script), *map(str, args)],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == code, process.stderr
    assert all(state.key not in process.stdout + process.stderr for state in aggregate.states)
    return json.loads(process.stdout or process.stderr), process


def test_field_discovery_and_invalid_fields_do_not_contact_any_service(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    output, _ = execute(aggregate, "resources-all", "--json")
    assert {"ref", "node", "matched_device_ids"} <= set(output["json_fields"])
    output, _ = execute(aggregate, "resources-all", "--json", "not-a-field", code=2)
    assert output["error"] == "invalid_summary_fields"
    assert state.events == []


def test_summary_bounds_bytes_and_devices_while_retaining_mixed_model_ids(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [{"id": "node" + str(i), "alias": "label", "devices": cards(64)} for i in range(30)]
    output, process = execute(aggregate, "resources-all", "--model", "P800", "--summary")
    result = output["result"]
    assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    assert 1 <= result["displayed_nodes"] <= 20 and result["has_more"]
    assert result["scope_complete"] and result["page_kind"] == "local_observation_projection"
    for row in result["rows"]:
        assert row["total_cards"] == 64 and row["matched_cards"] == 32
        assert row["matched_device_ids"] == list(range(2, 80, 4))
        assert row["devices_has_more"] and "holders" not in row
    assert len(state.events) == 2


def test_selected_fields_keep_reference_without_exposing_raw_metadata(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    output, process = execute(aggregate, "resources-all", "--json", "node,unit")
    row = output["result"]["rows"][0]
    assert set(row) == {"ref", "node", "unit"}
    assert "connection.json" not in process.stdout
    assert len(state.events) == 2


def test_reference_routes_only_the_selected_scope_and_matches_exact_spelling(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    path = aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"] = [
        {"id": node, "alias": "node07", "lock_status": "idle", "holders": [], "booking_list": []}
        for node in ("node07", "NODE07", "other")
    ]
    reference = helper.resource_reference(json.loads(path.read_text()), "node07", width=64)
    output, _ = execute(aggregate, "resources", "--ref", reference, "--summary")
    assert [row["node"] for row in output["result"]["rows"]] == ["node07"]
    assert len(first.events) == 2 and second.events == []
    assert output["result"]["rows"][0]["ref"] == reference


def test_reference_cannot_follow_a_connection_changed_to_another_actor(aggregate):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    reference = helper.resource_reference(json.loads(path.read_text()), "same-node", width=64)
    config = json.loads(path.read_text())
    config["actor"]["external_user_id"] = "another-user"
    path.write_text(json.dumps(config))
    output, _ = execute(aggregate, "resources", "--ref", reference, code=2)
    assert output["error"] == "resource_reference_scope_unavailable"
    assert state.events == []


def test_partial_scope_summary_keeps_failures_separate_from_local_more_rows(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    for index, state in enumerate((first, second)):
        aggregate.record(state, str(index))
    second.status = 503
    output, _ = execute(aggregate, "resources-all", "--summary")
    result = output["result"]
    assert result["scope_complete"] is False and result["has_more"] is False
    assert result["matched_nodes"] is None and result["unread_scopes"] == 1
    assert "http_503" in result["unread_reasons"]


def test_distinct_credentials_for_same_scope_do_not_break_summary(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "second" * 8
    aggregate.record(state, "second")
    state.accepted_keys = [state.key]
    state.key = original
    output, _ = execute(aggregate, "resources-all", "--summary")
    result = output["result"]
    assert result["displayed_nodes"] == 1 and result["duplicate_observations"] == 1
    assert result["conflicting_observations"] == 0 and result["scope_complete"]
    assert result["unknown_hardware_nodes"] == 1
    assert len(state.events) == 4


def test_conflicting_same_reference_observations_withhold_states_and_device_ids():
    context = {
        "server": {"instance_id": "instance"},
        "bot": {"uuid": "bot", "type": "DEVICE"},
        "actor": {"platform": "infoflow", "external_user_id": "synthetic-user"},
    }
    groups = []
    for model in ("P800", "H20"):
        groups.append(
            {
                "identity": {"origin": "http://127.0.0.1:8155"},
                "snapshot": {
                    "context": context,
                    "health": "running",
                    "snapshot_fresh_for_write": True,
                    "resources": [{"id": "node07", "devices": [{**cards(1)[0], "dev_model": model}]}],
                },
            }
        )
    output = json.loads(helper.query_summary_output({"result": {"groups": groups}}))["result"]
    assert output["scope_complete"] is False and output["conflicting_observations"] == 1
    assert set(output["rows"][0]) == {"ref", "node", "observation_conflict"}


def test_model_filter_cannot_hide_duplicate_node_identity(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [
        {"id": "node07", "devices": [{**cards(1)[0], "dev_model": model}]} for model in ("P800", "H20")
    ]
    output, _ = execute(aggregate, "resources-all", "--model", "P800", "--summary")
    result = output["result"]
    assert result["rows"] == [] and not result["scope_complete"]
    assert result["unidentified_nodes"] == 2


def test_model_filter_preserves_conflict_evidence_from_a_nonmatching_credential(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "second" * 8
    aggregate.record(state, "second")
    second_key = state.key
    state.accepted_keys, state.key = [second_key], original
    state.extra["resources"] = [{"id": "node07", "devices": [{**cards(1)[0], "dev_model": "P800"}]}]
    state.extra_per_key = {
        "Bearer " + second_key: {"resources": [{"id": "node07", "devices": [{**cards(1)[0], "dev_model": "H20"}]}]}
    }
    result = execute(aggregate, "resources-all", "--summary", "--model", "P800")[0]["result"]
    assert result["scope_complete"] is False and result["conflicting_observations"] == 1
    assert set(result["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert len(state.events) == 4


def test_alias_filter_cannot_hide_a_conflicting_same_node_observation(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "second" * 8
    aggregate.record(state, "second")
    second_key = state.key
    state.accepted_keys, state.key = [second_key], original
    node = {"id": "node07", "alias": "19", "lock_status": "idle", "holders": [], "booking_list": []}
    state.extra["resources"] = [node]
    state.extra_per_key = {"Bearer " + second_key: {"resources": [{**node, "alias": "another-label"}]}}
    result = execute(aggregate, "resources-all", "--summary", "--node", "19")[0]["result"]
    assert result["scope_complete"] is False and result["conflicting_observations"] == 1
    assert set(result["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert len(state.events) == 4


def test_unknown_hardware_is_counted_once_after_model_filter(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [
        {"id": "unknown", "accelerators": [{"model": "P800", "count": 0}, {"model": None, "count": 2}]}
    ]
    output, _ = execute(aggregate, "resources-all", "--model", "P800", "--summary")
    assert output["result"]["rows"] == []
    assert output["result"]["unknown_hardware_nodes"] == 1


def test_windows_unicode_details_defer_without_losing_a_valid_long_reference(monkeypatch):
    context = {
        "server": {"instance_id": "instance"},
        "bot": {"uuid": "bot", "type": "DEVICE"},
        "actor": {"platform": "infoflow", "external_user_id": "synthetic-user"},
    }
    node = "节" * 256
    snapshot = {
        "context": context,
        "health": "running",
        "snapshot_fresh_for_write": True,
        "resources": [
            {
                "id": node,
                "devices": [
                    {**cards(1)[0], "id": number, "dev_model": "模" * 79 + str(number)} for number in range(10)
                ],
            }
        ],
    }
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "name", "nt")
        encoded = helper.query_summary_output({"result": snapshot, "_approved_origin": "http://127.0.0.1:8155"})
    result = json.loads(encoded)["result"]
    assert len(encoded.encode()) + 1 <= helper.SUMMARY_BYTE_LIMIT
    assert len(result["rows"]) == 1 and result["rows"][0]["detail_deferred"] is True
    assert helper.parse_resource_reference(result["rows"][0]["ref"])[1] == node


@pytest.mark.parametrize("options", [["--limit", "0"], ["--offset", "-1"], ["--summary", "--full"]])
def test_invalid_summary_options_fail_before_requests(aggregate, options):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    execute(aggregate, "resources-all", *options, code=2)
    assert state.events == []
