"""Serve the real packaged read CLI; preserve IDs and expose only observed states."""

# ruff: noqa: F811 -- imported pytest fixtures
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from backend.test_packaged_cli_traffic import packaged_cli  # noqa: F401
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def read(aggregate, path, *flags):
    result = subprocess.run(
        [sys.executable, "-S", str(aggregate.script), "--connection", str(path), "resources", *flags],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["result"]


def card(identifier, model=None, *, status="idle", holders=None, queue=None):
    return {
        "id": identifier,
        "dev_model": model,
        "lock_status": status,
        "holders": [] if holders is None else holders,
        "booking_list": [] if queue is None else queue,
        "notes": "Preserve the complete raw entry",
    }


def groups(overview):
    return {row["model"]: row for row in overview["nodes"][0]["groups"]}


def test_real_packaged_read_groups_exact_card_zero_and_models_without_rewriting_raw(aggregate):
    server = aggregate.server(1)
    server.bot_type = "DEVICE"
    connection = aggregate.record(server, "cards")
    peer = [
        {
            "external_user_id": "peer",
            "remaining_seconds": 7200,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        }
    ]
    cards = [card(0, "P800", status="exclusive", holders=peer), card(1, "H20"), card(2, "P800"), card(3, "P800")]
    server.extra["resources"] = [{"id": "opaque-node", "alias": "19", "devices": cards}]
    before = copy.deepcopy(server.extra["resources"])
    snapshot = read(aggregate, connection, "--exact-lease-times")
    overview = snapshot["inventory_overview"]
    assert overview["source"] == "verified_snapshot" and overview["snapshot_fresh"]
    assert "not proof" in overview["meaning"] and "queue authorization" in overview["meaning"]
    assert overview["nodes"][0]["node"] == "opaque-node" and overview["nodes"][0]["alias"] == "19"
    model = groups(overview)
    assert model["P800"] == {"model": "P800", "idle": [2, 3], "busy": [0], "queued": [], "unknown": []}
    assert model["H20"]["idle"] == [1]
    assert snapshot["resources"] == before == server.extra["resources"]
    assert snapshot["context"]["actor"] == server.actor and snapshot["next_event_kind"] == "own_early_warning"
    assert [row[:2] for row in server.events] == [("/api/agent-skill", "GET"), ("/api/agent/resources", "GET")]
    snapshot = read(aggregate, connection)
    assert snapshot["inventory_overview"] == overview
    assert snapshot["resources"][0]["devices"][0]["holders"][0]["remaining_time"] == "2h"


def test_queue_and_malformed_state_never_become_idle_lock_candidates(aggregate):
    server = aggregate.server(1)
    server.bot_type = "QUEUE_DEVICE"
    connection = aggregate.record(server, "cards")
    peer, queued = [{"external_user_id": "peer"}], [{"external_user_id": server.actor["external_user_id"]}]
    cards = [
        card(0, None),
        card(1, "P800", queue=queued),
        card(2, "P800", status="exclusive", holders=peer, queue=queued),
        card(3, "P800", holders=peer, queue=queued),  # Idle plus holder is contradictory; unknown wins.
        card(4, "P800", status={"state": "idle"}),
        card(5, "P800", status="exclusive"),
        card(6, "P800", holders=[{}]),
    ]
    server.extra["resources"] = [{"id": "unknown-model-is-not-P800", "alias": "P800", "devices": cards}]
    snapshot = read(aggregate, connection, "--exact-lease-times")
    model = groups(snapshot["inventory_overview"])
    assert model[None]["idle"] == [0] and model[None]["model"] is None
    assert model["P800"]["queued"] == [1, 2]
    assert model["P800"]["idle"] == [] and model["P800"]["busy"] == []
    assert model["P800"]["unknown"] == [3, 4, 5, 6]
    assert snapshot["resources"] == server.extra["resources"]


def test_duplicate_zero_ids_keep_original_types_and_are_marked_unknown(aggregate):
    server = aggregate.server(1)
    server.bot_type = "DEVICE"
    connection = aggregate.record(server, "cards")
    server.extra["resources"] = [{"id": "node", "devices": [card(0, "P800"), card("0", "P800")]}]
    overview = read(aggregate, connection, "--exact-lease-times")["inventory_overview"]
    assert groups(overview)["P800"]["unknown"] == [0, "0"]
    assert groups(overview)["P800"]["idle"] == []


def test_unverified_actor_cannot_produce_an_inventory_overview(aggregate):
    server = aggregate.server(1)
    server.actor["external_user_id"] = ""
    aggregate.record(server, "unverified")
    result = aggregate.run("resources-all")["result"]
    assert result["complete"] is False and result["groups"] == []
    assert result["unread"][0]["error"].startswith("context_actor_mismatch")
    assert len(server.events) == 2


def test_overview_is_bounded_without_dropping_raw_nodes(aggregate):
    server = aggregate.server(1)
    server.bot_type = "DEVICE"
    connection = aggregate.record(server, "cards")
    server.extra["resources"] = [{"id": f"node-{index}", "devices": [card(0, "P800")]} for index in range(200)]
    snapshot = read(aggregate, connection, "--exact-lease-times")
    overview = snapshot["inventory_overview"]
    assert overview["truncated"] and overview["omitted_entries"] > 0
    assert len(json.dumps(overview, ensure_ascii=False).encode()) <= 16 * 1024
    assert snapshot["resources"] == server.extra["resources"] and len(snapshot["resources"]) == 200


def test_whole_node_models_use_only_observed_hardware_and_bound_oversized_aliases(aggregate):
    server = aggregate.server(1)
    connection = aggregate.record(server, "whole-nodes")
    server.extra["resources"] = [
        {**card("opaque", status="idle"), "alias": "P800"},
        {**card("mixed", status="idle"), "accelerators": [{"model": "P800", "count": 2}, {"model": "H20", "count": 2}]},
        {**card("oversized", status="idle"), "alias": "x" * 20000},
    ]
    snapshot = read(aggregate, connection, "--exact-lease-times")
    overview = snapshot["inventory_overview"]
    nodes = {row["node"]: row for row in overview["nodes"]}
    assert nodes["opaque"]["groups"] == [{"model": None, "idle": ["opaque"], "busy": [], "queued": [], "unknown": []}]
    assert nodes["mixed"]["groups"][0]["model"] is None
    assert nodes["mixed"]["accelerators"] == [{"model": "P800", "count": 2}, {"model": "H20", "count": 2}]
    assert "oversized" not in nodes and overview["truncated"] and overview["omitted_entries"] == 2
    assert len(json.dumps(overview, ensure_ascii=False).encode()) <= 16 * 1024
    assert snapshot["resources"] == server.extra["resources"]


def test_aggregate_overview_is_derived_after_completion_freshness_and_mine_stays_unchanged(aggregate):
    import importlib.util

    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    spec = importlib.util.spec_from_file_location("overview_aggregate_timing", aggregate.script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def clock():
        return 6 if second.events else 0

    result = module.resources_all(aggregate.catalog, root=aggregate.script.parents[1], clock=clock)["result"]
    old, fresh = [row["snapshot"] for row in result["groups"]]
    assert result["complete"] is False and result["http_requests"] == 4
    assert groups(old["inventory_overview"])[None]["unknown"] == ["same-node"]
    assert not old["inventory_overview"]["snapshot_fresh"]
    assert groups(fresh["inventory_overview"])[None]["busy"] == ["same-node"]
    mine = aggregate.run("resources-all", "--mine")["result"]
    assert all("inventory_overview" not in row["snapshot"] for row in mine["groups"])


def test_overview_does_not_change_mutation_result_or_request_count(packaged_cli):
    run, connection, _ = packaged_cli
    result, events = run(
        "--connection",
        str(connection),
        "command",
        "--operation",
        "lock",
        "--node",
        "n1",
        "--whole-node",
        "--duration",
        "1h",
    )
    assert result["result"]["verified"]
    assert "inventory_overview" not in result["result"]["verified_snapshot"]
    assert len(events) == 3 and sum(event["method"] == "POST" for event in events) == 1


@pytest.mark.parametrize(
    "fields,missing,classification",
    [
        ({}, ["accelerators", "lock_status", "booking_list"], "unknown"),
        (
            {"accelerators": None, "lock_status": None, "booking_list": None},
            ["accelerators", "lock_status", "booking_list"],
            "unknown",
        ),
        ({"accelerators": [], "lock_status": "idle", "booking_list": []}, [], "idle"),
        (
            {"accelerators": [{"model": "P800", "count": 8}], "lock_status": "idle"},
            ["booking_list"],
            "unknown",
        ),
    ],
)
def test_sparse_whole_node_observations_distinguish_unreported_from_explicit_empty(
    aggregate, fields, missing, classification
):
    server = aggregate.server(1)
    connection = aggregate.record(server, "sparse-observations")
    node = {"id": "node-a", "holders": [], **fields}
    server.extra["resources"] = [copy.deepcopy(node)]
    snapshot = read(aggregate, connection, "--exact-lease-times")
    observed = snapshot["inventory_overview"]["nodes"][0]
    assert observed.get("not_reported_fields", []) == missing
    assert observed["groups"][0][classification] == ["node-a"]
    assert snapshot["resources"] == [node] == server.extra["resources"]
    if "accelerators" in missing:
        assert observed["accelerators"] is None
    else:
        assert observed["accelerators"] == fields["accelerators"]
    assert [event[1] for event in server.events] == ["GET", "GET"]
