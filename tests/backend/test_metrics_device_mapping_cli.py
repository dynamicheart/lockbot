"""Explicit resource-to-collector joins; local fake services and observed metrics only."""

# ruff: noqa: F811 -- imported pytest fixture
import json
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import KEY, case, helper  # noqa: F401


def vector(readings, *, health="ok"):
    now = helper.time.time()
    result = []

    def sample(name, value, node, **labels):
        return {"metric": {"__name__": "lockbot_" + name, "node": node, **labels}, "value": [now, str(value)]}

    for source_node, cards in readings.items():
        result.extend(
            [
                sample("collector_success", 1, source_node, collector="host"),
                sample("sample_timestamp_seconds", now, source_node, collector="host"),
                sample("host_cpu_utilization_percent", 0, source_node),
            ]
        )
        if health != "missing":
            result.extend(
                [
                    sample("collector_success", 0 if health == "failed" else 1, source_node, collector="accelerator"),
                    sample(
                        "sample_timestamp_seconds",
                        now - (1000 if health == "stale" else 0),
                        source_node,
                        collector="accelerator",
                    ),
                ]
            )
        for source_device, percent in cards.items():
            result.append(sample("accelerator_utilization_percent", percent, source_node, device=source_device))
    return result


def configured(case, monkeypatch, mapping, *, nodes=None, bot_type="DEVICE", readings=None, health="ok"):
    """Supply source config before context verification, rather than mutating a verified context."""
    case.state.bot_type = bot_type
    nodes = nodes or {"node-a": "host-a"}
    if set(nodes.values()) != {"host-a"}:
        from lockbot.metrics import gateway

        validate = gateway.validate_query
        monkeypatch.setattr(gateway, "validate_query", lambda query, _allowed: validate(query, set(nodes.values())))
    case.state.metrics_rows = vector(readings or {"host-a": {"0": 11, "1": 77}}, health=health)
    original = case.client._request

    def request(url, **options):
        response = original(url, **options)
        if url.endswith("/api/agent/context"):
            source = response["utilization"]["configuration"]
            source["nodeMapping"] = nodes
            if mapping is None:
                source.pop("deviceMapping", None)
            else:
                source["deviceMapping"] = mapping
        return response

    monkeypatch.setattr(case.client, "_request", request)
    context = case.client.context()
    return case.client, context


def summary(metrics, resource_ids, *, node="node-a", unit="card"):
    rows = [
        {
            "ref": "synthetic-ref/" + node,
            "node": node,
            "unit": unit,
            "matched_device_ids": resource_ids,
            "states": {"idle": 1},
            "snapshot_fresh": True,
        }
    ]
    result = helper.merge_summary_metrics(rows, metrics)[0]
    encoded = json.dumps(result)
    assert KEY not in encoded and "metrics-grant-" not in encoded
    assert "deviceMapping" not in encoded and "nodeMapping" not in encoded and '"data"' not in encoded
    assert result["states"] == {"idle": 1} and result["snapshot_fresh"] is True
    assert result["metrics"]["does_not_authorize_resources"] is True
    return result["metrics"]


def assert_source_credentials(case):
    requests = [entry for entry in case.state.requests if entry[0].startswith("/metrics/")]
    assert requests and all(entry[2].get("Authorization", "").startswith("Bearer metrics-grant-") for entry in requests)
    assert all(KEY not in str(entry) for entry in requests)
    assert not any("commands" in entry[0] for entry in case.state.requests)


def test_swapped_collector_indices_are_joined_to_explicit_resource_ids(case, monkeypatch):
    client, context = configured(case, monkeypatch, {"node-a:dev0": "1", "node-a:dev1": "0"})
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [0, 1])
    cards = {card["device"]: card for card in projected["cards"]}
    assert set(cards) == {"0", "1"}
    assert cards["0"]["percent"] == 77 and cards["1"]["percent"] == 11
    raw = {card["resource_device"]: card for card in metrics["utilization"][0]["cards"]}
    assert raw["0"]["device"] == "1" and raw["1"]["device"] == "0"
    assert all(card["mapping_status"] == "mapped" for card in raw.values())
    assert all("resource_device" not in card and "source_device" not in card for card in cards.values())
    assert_source_credentials(case)


def test_uuid_source_label_is_not_mistaken_for_resource_device_id(case, monkeypatch):
    label = "GPU-4ca37cae-d1f6-47bb-95b5-733c7bc09aef"
    client, context = configured(case, monkeypatch, {"node-a:dev7": label}, readings={"host-a": {label: 35}})
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [7])
    assert len(projected["cards"]) == 1
    assert projected["cards"][0]["device"] == "7"
    assert label not in json.dumps(projected)
    raw = metrics["utilization"][0]["cards"][0]
    assert (raw["device"], raw["resource_device"], raw["mapping_status"]) == (label, "7", "mapped")
    assert projected["cards"][0]["percent"] == 35


@pytest.mark.parametrize("mapping", [None, {}, {"node-a:dev1": "1"}])
def test_missing_mapping_never_falls_back_to_equal_source_resource_numbers(case, monkeypatch, mapping):
    client, context = configured(case, monkeypatch, mapping)
    metrics = client.metrics(["node-a"], context=context)
    assert summary(metrics, [0])["cards"] == []


def test_duplicate_source_targets_within_node_do_not_pick_a_resource_even_if_only_one_selected(case, monkeypatch):
    client, context = configured(
        case,
        monkeypatch,
        {"node-a:dev0": "shared-label", "node-a:dev1": "shared-label"},
        readings={"host-a": {"shared-label": 19}},
    )
    metrics = client.metrics(["node-a"], context=context)
    assert summary(metrics, [0])["cards"] == []
    assert summary(metrics, [0, 1])["cards"] == []


def test_same_device_source_label_is_safe_when_reused_on_distinct_source_nodes(case, monkeypatch):
    client, context = configured(
        case,
        monkeypatch,
        {"node-a:dev0": "0", "node-b:dev0": "0"},
        nodes={"node-a": "host-a", "node-b": "host-b"},
        readings={"host-a": {"0": 10}, "host-b": {"0": 80}},
    )
    metrics = client.metrics(["node-a", "node-b"], context=context)
    first, second = summary(metrics, [0]), summary(metrics, [0], node="node-b")
    assert first["cards"][0]["percent"] == 10 and second["cards"][0]["percent"] == 80
    assert first["cards"][0]["device"] == second["cards"][0]["device"] == "0"


@pytest.mark.parametrize("value", [None, "", False, 0, ["0"], {"target": "0"}])
def test_invalid_mapping_values_cannot_produce_a_claimed_mapped_card(case, monkeypatch, value):
    try:
        client, context = configured(case, monkeypatch, {"node-a:dev0": value})
        metrics = client.metrics(["node-a"], context=context)
    except helper.ClientError:
        return  # Rejecting the configuration is also safe; no identity fallback.
    assert summary(metrics, [0])["cards"] == []


@pytest.mark.parametrize("identifier", ["00", "dev0", "slotA"])
def test_opaque_resource_ids_are_not_canonicalized_into_numeric_resource_zero(case, monkeypatch, identifier):
    client, context = configured(case, monkeypatch, {"node-a:dev0": "0"})
    metrics = client.metrics(["node-a"], context=context)
    assert summary(metrics, [identifier])["cards"] == []
    assert summary(metrics, [0])["cards"][0]["device"] == "0"


@pytest.mark.parametrize("health", ["ok", "stale", "failed", "missing"])
def test_zero_and_collection_health_survive_mapping_without_claiming_current_authority(case, monkeypatch, health):
    client, context = configured(
        case, monkeypatch, {"node-a:dev0": "UUID-zero"}, readings={"host-a": {"UUID-zero": 0}}, health=health
    )
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [0])
    assert len(projected["cards"]) == 1
    card = projected["cards"][0]
    assert card["status"] == health
    assert card["percent"] == (0 if health == "ok" else None)
    assert projected["best_effort"] and projected["does_not_authorize_resources"]


def test_whole_node_devices_keep_source_identity_without_invented_resource_ids(case, monkeypatch):
    label = "GPU-unallocated-source-label"
    client, context = configured(
        case, monkeypatch, {"node-a:dev0": label}, bot_type="NODE", readings={"host-a": {label: 29}}
    )
    metrics = client.metrics(["node-a"], context=context)
    raw = metrics["utilization"][0]
    assert raw["device_identity"] == "source"
    assert raw["cards"][0]["device"] == label
    assert raw["cards"][0]["mapping_status"] == "source_only"
    assert "resource_device" not in raw["cards"][0]
    projected = summary(metrics, [], unit="whole_node")
    assert projected["device_identity"] == "source"
    assert projected["cards"][0]["device"] == label
    assert "resource_device" not in json.dumps(projected)


@pytest.mark.parametrize("suffix", ["00", "dev0", "０", "slotA"])
def test_opaque_mapping_keys_do_not_alias_canonical_resource_zero(case, monkeypatch, suffix):
    client, context = configured(case, monkeypatch, {"node-a:dev" + suffix: "0"})
    metrics = client.metrics(["node-a"], context=context)
    assert summary(metrics, [0])["cards"] == []
    assert not any(card.get("resource_device") == "0" for card in metrics["utilization"][0]["cards"])


def test_duplicate_node_target_is_detected_even_when_other_node_not_selected(case, monkeypatch):
    client, context = configured(
        case, monkeypatch, {"node-a:dev0": "0"}, nodes={"node-a": "host-a", "hidden-node": "host-a"}
    )
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [0])
    assert projected["cards"] == []
    assert projected["cpu"]["percent"] is None


@pytest.mark.parametrize("change", ["device", "node", "type"])
def test_grant_refresh_cannot_change_mapping_before_second_source_query(case, monkeypatch, change):
    client, context = configured(case, monkeypatch, {"node-a:dev0": "0"})
    case.state.grant_fail = True
    original = client._request

    def request(url, **options):
        response = original(url, **options)
        if url.endswith("/api/agent/context") and case.state.context_count > 1:
            if change == "device":
                response["utilization"]["configuration"]["deviceMapping"] = {"node-a:dev0": "1"}
            elif change == "node":
                response["utilization"]["configuration"]["nodeMapping"] = {"node-a": "host-changed"}
            else:
                response["bot"]["type"] = "NODE"
        return response

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(helper.ClientError, match="metrics_mapping_changed"):
        client.metrics(["node-a"], context=context)
    assert len([entry for entry in case.state.requests if entry[0].startswith("/metrics/")]) == 1
    assert_source_credentials(case)


def test_grant_only_refresh_preserves_same_device_mapping(case, monkeypatch):
    client, context = configured(case, monkeypatch, {"node-a:dev0": "1", "node-a:dev1": "0"})
    case.state.grant_fail = True
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [0])
    assert projected["cards"][0]["device"] == "0" and projected["cards"][0]["percent"] == 77
    assert len([entry for entry in case.state.requests if entry[0].startswith("/metrics/")]) == 2
    assert_source_credentials(case)


def test_summary_device_limit_keeps_explicit_coverage_without_renumbering(case, monkeypatch):
    mapping = {"node-a:dev" + str(index): "UUID-" + str(24 - index) for index in range(25)}
    readings = {"host-a": {"UUID-" + str(index): index for index in range(25)}}
    client, context = configured(case, monkeypatch, mapping, readings=readings)
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, list(range(25)))
    assert len(projected["cards"]) == 20
    assert projected["cards_has_more"] and projected["reported_cards"] == 25
    for card in projected["cards"]:
        assert card["percent"] == 24 - int(card["device"])
    assert "UUID-" not in json.dumps(projected)


def test_ambiguous_node_does_not_discard_an_unrelated_healthy_node(case, monkeypatch):
    client, context = configured(
        case,
        monkeypatch,
        {"node-a:dev0": "0", "node-b:dev0": "0"},
        nodes={"node-a": "host-a", "duplicate-a": "host-a", "node-b": "host-b"},
        readings={"host-b": {"0": 80}},
    )
    metrics = client.metrics(["node-a", "node-b"], context=context)
    assert summary(metrics, [0])["cards"] == []
    healthy = summary(metrics, [0], node="node-b")
    assert healthy["cards"][0]["device"] == "0" and healthy["cards"][0]["percent"] == 80


def test_duplicate_target_hidden_behind_opaque_key_cannot_authorize_numeric_mapping(case, monkeypatch):
    client, context = configured(case, monkeypatch, {"node-a:dev0": "0", "node-a:dev00": "0"})
    metrics = client.metrics(["node-a"], context=context)
    assert summary(metrics, [0])["cards"] == []
    raw = next(card for card in metrics["utilization"][0]["cards"] if card["device"] == "0")
    assert raw["mapping_status"] == "ambiguous" and "resource_device" not in raw


def test_missing_mapped_sample_remains_unknown_instead_of_zero(case, monkeypatch):
    client, context = configured(
        case, monkeypatch, {"node-a:dev0": "UUID-absent"}, readings={"host-a": {"different-source": 42}}
    )
    metrics = client.metrics(["node-a"], context=context)
    projected = summary(metrics, [0])
    assert len(projected["cards"]) == 1
    assert projected["cards"][0]["device"] == "0"
    assert projected["cards"][0]["status"] == "missing"
    assert projected["cards"][0]["percent"] is None


@pytest.mark.parametrize("more_field", ["devices_has_more", "holdings_has_more", "bookings_has_more"])
def test_truncated_resource_selection_cannot_claim_exhaustive_metric_coverage(more_field):
    rows = [{"ref": "synthetic-ref", "node": "node-a", "unit": "card", "matched_device_ids": [0], more_field: True}]
    metrics = {
        "utilization": [
            {
                "node": "node-a",
                "cards_status": "reported",
                "cards": [
                    {
                        "device": "source-0",
                        "resource_device": "0",
                        "mapping_status": "mapped",
                        "status": "ok",
                        "percent": 0,
                        "sample_age_seconds": 1,
                    },
                    {
                        "device": "source-1",
                        "resource_device": "1",
                        "mapping_status": "mapped",
                        "status": "ok",
                        "percent": 51,
                        "sample_age_seconds": 1,
                    },
                ],
            }
        ]
    }
    projected = helper.merge_summary_metrics(rows, metrics)[0]["metrics"]
    assert [card["device"] for card in projected["cards"]] == ["0"]
    assert projected["cards"][0]["percent"] == 0
    assert projected["selection_incomplete"] and projected["cards_has_more"]
    assert projected["mapping_status"] == "partial"
    assert "source-" not in json.dumps(projected)


def test_node_name_containing_device_separator_does_not_cross_mapping_owner_boundary(case, monkeypatch):
    client, context = configured(
        case,
        monkeypatch,
        {"a:dev0": "gpu0", "a:dev0:dev0": "gpu0"},
        nodes={"a": "host-a", "a:dev0": "host-b"},
        readings={"host-a": {"gpu0": 14}, "host-b": {"gpu0": 83}},
    )
    metrics = client.metrics(["a", "a:dev0"], context=context)
    readings = {reading["node"]: reading for reading in metrics["utilization"]}
    for node in ("a", "a:dev0"):
        raw = readings[node]
        assert raw["mapping_status"] == "mapped"
        assert len(raw["cards"]) == 1
        card = raw["cards"][0]
        assert (card["device"], card["resource_device"], card["mapping_status"]) == ("gpu0", "0", "mapped")
    first = summary(metrics, [0], node="a")
    second = summary(metrics, [0], node="a:dev0")
    assert first["cards"][0]["percent"] == 14 and second["cards"][0]["percent"] == 83
    assert first["mapping_status"] == second["mapping_status"] == "mapped"


def empty_card_projection(**changes):
    row = {
        "ref": "synthetic-ref",
        "node": "node-a",
        "unit": "card",
        "matched_device_ids": [],
        "device_inventory_complete": True,
        "total_cards": 0,
        "matched_cards": 0,
    }
    row.update(changes)
    metrics = {"utilization": [{"node": "node-a", "mapping_status": "missing", "cards_status": "missing", "cards": []}]}
    return helper.merge_summary_metrics([row], metrics)[0]["metrics"]


def test_known_empty_complete_card_inventory_does_not_invent_more_devices():
    projected = empty_card_projection()
    assert projected["cards"] == [] and projected["reported_cards"] == 0
    assert projected["mapping_status"] == "not_required"
    assert not projected["cards_has_more"] and not projected.get("selection_incomplete", False)
    assert projected["best_effort"] and projected["does_not_authorize_resources"]


@pytest.mark.parametrize(
    "changes",
    [
        {"device_inventory_complete": False},
        {"device_inventory_complete": None},
        {"device_inventory_complete": 1},
        {"total_cards": False},
        {"matched_cards": False},
        {"total_cards": "0"},
        {"matched_cards": "0"},
        {"total_cards": 1},
        {"matched_device_ids": None},
        {"devices_has_more": True},
        {"holdings_has_more": True},
        {"bookings_has_more": True},
    ],
)
def test_unknown_or_truncated_empty_card_inventory_preserves_partial_coverage(changes):
    projected = empty_card_projection(**changes)
    assert projected["cards"] == []
    assert projected["mapping_status"] == "missing"
    assert projected["selection_incomplete"] and projected["cards_has_more"]


def test_whole_node_ambiguous_mapping_remains_invalid_without_source_identity_success():
    rows = [{"ref": "synthetic-ref", "node": "node-a", "unit": "whole_node"}]
    metrics = {
        "utilization": [
            {
                "node": "node-a",
                "device_identity": "source",
                "mapping_status": "ambiguous_node",
                "cards_status": "invalid",
                "cards": [],
                "cpu": {"status": "invalid", "percent": None},
            }
        ]
    }
    projected = helper.merge_summary_metrics(rows, metrics)[0]["metrics"]
    assert projected["status"] == "invalid" and projected["mapping_status"] == "ambiguous_node"
    assert projected["cards_status"] == "invalid" and projected["cards"] == []
    assert projected["cpu"]["status"] == "invalid" and projected["cpu"]["percent"] is None
    assert projected["does_not_authorize_resources"] and projected["best_effort"]


def test_missing_device_ids_list_cannot_be_treated_as_known_empty_inventory():
    row = {
        "ref": "synthetic-ref",
        "node": "node-a",
        "unit": "card",
        "device_inventory_complete": True,
        "total_cards": 0,
        "matched_cards": 0,
    }
    metrics = {"utilization": [{"node": "node-a", "mapping_status": "missing", "cards_status": "missing", "cards": []}]}
    projected = helper.merge_summary_metrics([row], metrics)[0]["metrics"]
    assert projected["mapping_status"] == "missing"
    assert projected["cards_has_more"] and projected["selection_incomplete"]


def test_actual_ambiguous_mapping_marks_metrics_incomplete_without_changing_known_idle_resource(case, monkeypatch):
    client, context = configured(
        case, monkeypatch, {}, bot_type="NODE", nodes={"node-a": "host-a", "hidden-node": "host-a"}
    )
    # The fixture serves separate legacy identity/resources, matching inventory() assembly.
    snapshot = {**client.resources(context), "context": context, "context_source": "separate_verified_read"}
    combined = helper.attach_metrics(client, snapshot, context=context)
    assert combined["resources"] == snapshot["resources"]
    assert combined["snapshot_fresh_for_write"] == snapshot["snapshot_fresh_for_write"]
    assert combined["metrics"]["complete"] is False
    assert combined["metrics"]["unread"] == [{"nodes": ["node-a"], "error": "ambiguous_metrics_node_mapping"}]
    result = json.loads(helper.query_summary_output({"result": combined, "_approved_origin": client.base}))["result"]
    assert result["metrics_requested"] and result["metrics_read_complete"] is False
    assert result["rows"][0]["states"]["idle"] == 1
    assert result["rows"][0]["metrics"]["mapping_status"] == "ambiguous_node"
    assert not any(entry[0].startswith("/metrics/") for entry in case.state.requests)


def test_mapping_gap_keeps_unrelated_healthy_cpu_reading_and_explicit_unread_reason():
    readings = [
        {
            "node": "node-a",
            "mapping_status": "ambiguous_node",
            "cards_status": "invalid",
            "cards": [],
            "cpu": {"status": "invalid", "percent": None},
        },
        {
            "node": "node-b",
            "mapping_status": "source_only",
            "cards_status": "missing",
            "cards": [],
            "cpu": {"status": "ok", "percent": 27, "sample_age_seconds": 1},
        },
    ]
    client = SimpleNamespace(metrics=lambda nodes: {"utilization": readings})
    resources = [{"id": node, "lock_status": "idle", "holders": []} for node in ("node-a", "node-b")]
    snapshot = {"resources": resources, "snapshot_fresh_for_write": True}
    combined = helper.attach_metrics(client, snapshot)
    assert combined["resources"] == resources and combined["snapshot_fresh_for_write"] is True
    assert combined["metrics"]["complete"] is False
    assert combined["metrics"]["utilization"] == readings
    assert combined["metrics"]["unread"] == [{"nodes": ["node-a"], "error": "ambiguous_metrics_node_mapping"}]
    rows = [{"ref": "synthetic/" + node, "node": node, "unit": "whole_node"} for node in ("node-a", "node-b")]
    first, second = helper.merge_summary_metrics(rows, combined["metrics"])
    assert first["metrics"]["status"] == "invalid"
    assert first["metrics"]["errors"] == [{"error": "ambiguous_metrics_node_mapping"}]
    assert second["metrics"]["cpu"]["percent"] == 27
    assert "errors" not in second["metrics"]


def test_local_mapping_gap_does_not_prevent_reading_later_bounded_batch():
    calls = []

    def metrics(nodes):
        calls.append(list(nodes))
        return {
            "utilization": [
                {
                    "node": node,
                    "mapping_status": "ambiguous_node" if node == "node-0" else "source_only",
                    "cpu": {
                        "status": "invalid" if node == "node-0" else "ok",
                        "percent": None if node == "node-0" else 0,
                        "sample_age_seconds": 1,
                    },
                }
                for node in nodes
            ]
        }

    resources = [{"id": "node-" + str(index)} for index in range(65)]
    combined = helper.attach_metrics(SimpleNamespace(metrics=metrics), {"resources": resources})
    assert [len(batch) for batch in calls] == [64, 1]
    assert calls[1] == ["node-64"]
    assert len(combined["metrics"]["utilization"]) == 65
    assert combined["metrics"]["utilization"][-1]["cpu"]["percent"] == 0
    assert combined["metrics"]["complete"] is False
    assert combined["metrics"]["unread"] == [{"nodes": ["node-0"], "error": "ambiguous_metrics_node_mapping"}]
