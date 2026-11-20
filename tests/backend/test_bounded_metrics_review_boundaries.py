"""Pure counterexamples from the bounded telemetry review; no transports or Keys."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import helper


def reading(device="0", percent=0):
    return {
        "device": "collector-" + device,
        "resource_device": device,
        "mapping_status": "mapped",
        "status": "ok",
        "percent": percent,
        "sample_age_seconds": 0,
    }


def own_snapshot(records):
    return {
        "scope": "own",
        "ownership_coverage": {"complete": True},
        "holdings": records,
        "bookings": [],
    }


def own_card(device, *, node="node-a", model="P800", alias=None):
    return {"node": node, "device": device, "allocation": "card", "model": model, "alias": alias}


def inventory(ids, *, extra_node=False):
    def node(name, devices):
        return {
            "id": name,
            "devices": [
                {"id": device, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
                for device in devices
            ],
        }

    resources = [node("node-a", ids)]
    if extra_node:
        resources.append(node("healthy", [0]))
    return {
        "context": {
            "server": {"instance_id": "review-instance"},
            "bot": {"uuid": "review-scope", "type": "DEVICE"},
            "actor": {"platform": "review", "external_user_id": "review-actor"},
            "utilization": {"configuration": {"nodeMapping": {"node-a": "source-a", "healthy": "source-b"}}},
        },
        "resources": resources,
        "snapshot_fresh_for_write": True,
        "health": "running",
        "age_seconds": 0,
    }


@pytest.mark.parametrize("receipt,now,status", [((100, 30), 102, "stale"), (None, 102, "invalid")])
def test_card_memory_ages_even_when_parent_card_is_also_a_sample(receipt, now, status):
    original = {
        "utilization": [
            {
                "node": "node-a",
                "cards": [
                    {
                        "status": "ok",
                        "percent": 25,
                        "sample_age_seconds": 29,
                        "memory": {
                            "used": {"status": "ok", "bytes": 1024, "sample_age_seconds": 29},
                            "total": {"status": "ok", "bytes": 8192, "sample_age_seconds": 29},
                        },
                    }
                ],
            }
        ],
    }
    untouched = deepcopy(original)
    result = helper.metrics_at_completion(original, {"node-a": receipt} if receipt else {}, now)
    card = result["utilization"][0]["cards"][0]
    assert card["status"] == status and card["percent"] is None
    for sample in card["memory"].values():
        assert sample["status"] == status and sample["bytes"] is None
        assert sample["sample_age_seconds"] == (31 if receipt else None)
    assert original == untouched


def test_unknown_model_own_card_prevents_complete_filtered_metrics_denominator():
    snapshot = own_snapshot([own_card(0), own_card(1, model=None)])
    metrics = {"utilization": [{"node": "node-a", "cards": [reading()], "cards_status": "reported"}]}
    row = {"ref": "selected-node", "node": "node-a", "unit": "card", "matched_device_ids": [0]}
    selected, complete = helper.metric_device_selection(snapshot, node="node-a", model="p800")
    assert selected == ["0"] and complete is False
    result = helper.compact_resource_metrics(row, snapshot, metrics, model="p800")["accelerator"]
    assert result["mean_percent"] == 0 and result["ok_devices"] == 1
    assert result["selected_devices"] is None and result["selection_complete"] is False
    assert result["status"] == "partial"


def test_own_selection_matches_exact_node_even_when_another_node_has_that_alias():
    snapshot = own_snapshot([own_card(0), own_card(9, node="different-node", alias="node-a")])
    assert helper.metric_device_selection(snapshot, node="node-a", model="p800") == (["0"], True)


def test_own_duplicate_identity_cannot_be_hidden_by_the_model_filter():
    snapshot = own_snapshot([own_card(0), own_card(0, model="P900"), own_card(1)])
    selected, complete = helper.metric_device_selection(snapshot, node="node-a", model="p800")
    assert selected == ["1"] and complete is False


def test_peer_id_disagreement_beyond_display_prefix_blocks_metrics_without_a_read(monkeypatch):
    first, second = inventory(range(21)), inventory([*range(20), 21])
    groups = [
        {"snapshot": snapshot, "_approved_origin": "http://review.invalid", "_client": SimpleNamespace()}
        for snapshot in (first, second)
    ]
    rows, notes, complete = helper.resource_summary_projection({"result": {"groups": groups}})
    # These old, bounded display observations are equal: metrics need a full-ID check.
    assert complete and notes["conflicting_observations"] == 0 and len(rows) == 1

    def no_source_read(*args, **kwargs):
        pytest.fail("ambiguous peer telemetry must not start a source read")

    monkeypatch.setattr(helper, "attach_metrics", no_source_read)
    reader = helper._ResourceListReader(root=None, opener=None, clock=lambda: 100, legacy_read=False)
    reader.telemetry(groups, ["node-a"])
    assert reader.calls == 0
    assert groups[0]["_metrics"]["complete"] is False
    assert groups[0]["_metrics"]["unread"] == [{"nodes": ["node-a"], "error": "ambiguous_metrics_observation"}]


def test_peer_configuration_disagreement_blocks_metrics_even_with_equal_inventory(monkeypatch):
    first, second = inventory([0]), inventory([0])
    second["context"]["utilization"]["configuration"]["nodeMapping"]["node-a"] = "different-source"
    groups = [{"snapshot": snapshot, "_client": SimpleNamespace()} for snapshot in (first, second)]
    monkeypatch.setattr(helper, "attach_metrics", lambda *args, **kwargs: pytest.fail("changed source queried"))
    reader = helper._ResourceListReader(root=None, opener=None, clock=lambda: 100, legacy_read=False)
    reader.telemetry(groups, ["node-a"])
    assert groups[0]["_metrics"]["unread"][0]["error"] == "ambiguous_metrics_observation"


def test_healthy_peer_selection_still_reads_once_and_synchronizes_shared_budget(monkeypatch):
    first, second = inventory(range(21), extra_node=True), inventory([*range(20), 21], extra_node=True)
    client = SimpleNamespace()
    groups = [
        {"snapshot": snapshot, "_client": client if index == 0 else SimpleNamespace()}
        for index, snapshot in enumerate((first, second))
    ]
    observed = []

    def attach(selected_client, snapshot, *, context, _sample_clocks):
        assert selected_client is client and selected_client.calls == 4 and selected_client.call_limit == 12
        assert context is first["context"]
        observed.append([node["id"] for node in snapshot["resources"]])
        selected_client.calls += 1
        return {"metrics": {"complete": True, "utilization": [], "unread": []}}

    monkeypatch.setattr(helper, "attach_metrics", attach)
    reader = helper._ResourceListReader(root=None, opener=None, clock=lambda: 100, legacy_read=False)
    reader.calls = 4
    reader.telemetry(groups, ["node-a", "healthy"])
    assert observed == [["healthy"]] and reader.calls == 5
    assert groups[0]["_metrics"]["complete"] is False
    assert groups[0]["_metrics"]["unread"] == [{"nodes": ["node-a"], "error": "ambiguous_metrics_observation"}]
