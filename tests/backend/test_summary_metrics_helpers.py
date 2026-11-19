"""Bounded optional summary telemetry using synthetic data and the served package."""

# ruff: noqa: F811 -- imported fixture
import importlib.util
import json
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import (
    KEY,
    case,  # noqa: F401
    helper,
)


@pytest.fixture
def packaged_metrics(case):
    spec = importlib.util.spec_from_file_location("summary_metrics_package", case.root / helper.SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.Client(case.path, root=case.root, sleep=case.delays.append)
    return module, client


def test_packaged_reuse_avoids_context_get_and_keeps_default_metrics_behavior(case, packaged_metrics):
    _, client = packaged_metrics
    context = client.context()
    client.metrics(["node-a"], context=context)
    assert case.state.context_count == 1
    client.metrics(["node-a"])
    assert case.state.context_count == 2
    assert all(KEY not in str(row) for row in case.state.requests if row[0].startswith("/metrics/"))


def test_packaged_reuse_refreshes_short_grant_once_and_reuses_it_for_next_batch(case, packaged_metrics):
    _, client = packaged_metrics
    case.state.grant_fail = True
    context = client.context()
    client.metrics(["node-a"], context=context)
    client.metrics(["node-a"], context=context)
    assert case.state.context_count == 2
    requests = [row for row in case.state.requests if row[0].startswith("/metrics/")]
    assert [row[2]["Authorization"] for row in requests] == [
        "Bearer metrics-grant-1",
        "Bearer metrics-grant-2",
        "Bearer metrics-grant-2",
    ]


@pytest.mark.parametrize("change", ["copy", "mutate", "actor", "bot", "instance"])
def test_packaged_reuse_requires_unchanged_client_verified_context(case, packaged_metrics, change):
    module, client = packaged_metrics
    context = client.context()
    if change == "copy":
        context = json.loads(json.dumps(context))
    elif change == "mutate":
        context["utilization"]["configuration"]["endpoint"] += "/changed"
    elif change == "actor":
        client.config["actor"] = {**client.config["actor"], "external_user_id": "different"}
    elif change == "bot":
        client.config["bot_uuid"] = "different"
    else:
        client.config["instance_id"] = "different"
    with pytest.raises(module.ClientError):
        client.metrics(["node-a"], context=context)
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)
    assert case.state.context_count == 1


@pytest.mark.parametrize("change", ["origin", "mapping", "budget", "key"])
def test_packaged_reuse_retains_source_scope_credentials_and_request_budget(case, packaged_metrics, change):
    module, client = packaged_metrics
    if change == "key":
        # A service-issued context still cannot send a resource-shaped metrics credential.
        request = client._request

        def bad_grant(*args, **kwargs):
            result = request(*args, **kwargs)
            if "utilization" in result:
                result["utilization"]["access"]["token"] = KEY
            return result

        client._request = bad_grant
    context = client.context()
    nodes = ["node-a"]
    if change == "origin":
        client.config["approved_metrics_origin"] = "https://unapproved.example.invalid"
    elif change == "mapping":
        nodes = ["unknown"]
    elif change == "budget":
        client.calls = module.MAX_CALLS
    with pytest.raises(module.ClientError):
        client.metrics(nodes, context=context)
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)


@pytest.mark.parametrize(
    "credential",
    [
        KEY,
        "lbp_" + "P" * 43,
        "lba1.synthetic.private",
        "Bearer " + KEY,
        "prefix" + KEY,
        KEY + "suffix",
        "prefixlbp_" + "P" * 43,
        "prefixlbk_" + "K" * 43,
    ],
)
def test_metrics_never_forwards_resource_credentials_or_enrollment_proofs(case, packaged_metrics, credential):
    module, client = packaged_metrics
    request = client._request

    def wrong_grant(*args, **kwargs):
        result = request(*args, **kwargs)
        if "utilization" in result:
            result["utilization"]["access"]["token"] = credential
        return result

    client._request = wrong_grant
    context = client.context()
    with pytest.raises(module.ClientError, match="resource_key_for_metrics_refused"):
        client.metrics(["node-a"], context=context)
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)


@pytest.mark.parametrize("field", ["nodeMapping", "deviceMapping"])
def test_mapping_text_cannot_smuggle_another_resource_credential_into_source_query(case, packaged_metrics, field):
    module, client = packaged_metrics
    request = client._request

    def wrong_mapping(*args, **kwargs):
        result = request(*args, **kwargs)
        if "utilization" in result:
            key = "node-a" if field == "nodeMapping" else "node-a:dev0"
            result["utilization"]["configuration"][field] = {key: "prefixlbk_" + "X" * 43}
        return result

    client._request = wrong_mapping
    context = client.context()
    with pytest.raises(module.ClientError, match="invalid_metrics_(device_)?mapping"):
        client.metrics(["node-a"], context=context)
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)


def test_packaged_optional_outage_keeps_snapshot_and_preserves_error_metadata(case, packaged_metrics):
    module, client = packaged_metrics
    context = client.context()
    case.state.metrics_status = 429
    snapshot = {"resources": [{"id": "node-a"}], "snapshot_fresh_for_write": True}
    result = module.attach_metrics(client, snapshot, context=context)
    assert result["resources"] == snapshot["resources"] and result["snapshot_fresh_for_write"] is True
    assert result["metrics"]["unread"] == [
        {"nodes": ["node-a"], "error": "http_429", "http_status": 429, "retry_after_seconds": 40}
    ]
    assert case.state.context_count == 1
    assert sum(row[0].startswith("/metrics/") for row in case.state.requests) == 1


def test_attach_optional_context_batches_only_selected_nodes_and_supports_legacy_clients():
    context, calls = {}, []
    client = object.__new__(helper.Client)

    def metrics(nodes, *, context):
        calls.append((nodes, context))
        return {"utilization": [{"node": node} for node in nodes]}

    client.metrics = metrics
    snapshot = {"resources": [{"id": str(number)} for number in range(65)]}
    result = helper.attach_metrics(client, snapshot, context=context)
    assert [len(nodes) for nodes, _ in calls] == [64, 1]
    assert all(observed is context for _, observed in calls)
    assert len(result["metrics"]["utilization"]) == 65
    legacy = SimpleNamespace(metrics=lambda nodes: {"utilization": [{"node": node} for node in nodes]})
    assert helper.attach_metrics(legacy, snapshot, context=context)["metrics"]["complete"] is True
    calls.clear()
    helper.attach_metrics(client, {"resources": []}, context=context)
    assert calls == []


def reading(status="ok", value=0, age=3):
    return {"status": status, "percent": value, "sample_age_seconds": age}


def test_summary_metrics_bound_long_arrays_strip_raw_fields_and_never_change_resource_states():
    rows = [{"ref": "exact", "node": "node-a", "unit": "whole_node", "states": {"busy": 1}, "snapshot_fresh": False}]
    sample = reading()
    metrics = {
        "utilization": [
            {
                "node": "node-a",
                "source_node": "private-host",
                "endpoint": "http://private.invalid",
                "cpu": sample,
                "cards_status": "reported",
                "cards": [{"device": str(index), **sample} for index in range(400)],
                "filesystems": [
                    {"filesystem": "/mount/" + str(index), "used": {**sample, "bytes": 0}} for index in range(400)
                ],
            }
        ],
        "data": {"raw": "never expose"},
    }
    original = json.dumps((rows, metrics), sort_keys=True)
    (result,) = helper.merge_summary_metrics(rows, metrics)
    assert result["states"] == {"busy": 1} and result["snapshot_fresh"] is False
    projected = result["metrics"]
    assert len(projected["cards"]) == len(projected["filesystems"]) == 20
    assert projected["cards_has_more"] and projected["filesystems_has_more"]
    assert projected["reported_cards"] == projected["reported_filesystems"] == 400
    assert projected["does_not_authorize_resources"] is True
    assert projected["cpu"] == sample
    assert not any(
        value in json.dumps(result) for value in ("private-host", "private.invalid", "never expose", "source_node")
    )
    assert json.dumps((rows, metrics), sort_keys=True) == original


def test_summary_metrics_match_exact_nodes_and_device_ids_including_own_children():
    rows = [
        {"node": "node-a", "unit": "card", "matched_device_ids": [0, "00", 7]},
        {"node": "NODE-A", "unit": "card", "matched_device_ids": [0]},
        {"node": "node-a", "unit": "card", "holdings": [{"device": 7}], "bookings": [{"device": "00"}]},
        {"node": "node-a", "unit": "card"},
    ]
    metrics = {
        "utilization": [
            {
                "node": "node-a",
                "cards_status": "reported",
                "cards": [
                    {
                        "device": "collector-" + device,
                        "resource_device": device,
                        "mapping_status": "mapped",
                        **reading(),
                    }
                    for device in ("0", "00", "7", "1")
                ],
            }
        ]
    }
    result = helper.merge_summary_metrics(rows, metrics)
    # The current mapping schema cannot prove an opaque/padded resource ID.
    assert [card["device"] for card in result[0]["metrics"]["cards"]] == ["0", "7"]
    assert result[1]["metrics"]["status"] == "missing"
    assert [card["device"] for card in result[2]["metrics"]["cards"]] == ["7"]
    assert result[3]["metrics"]["cards"] == []


@pytest.mark.parametrize("status", ["stale", "missing", "failed", "invalid", "ok"])
def test_summary_metrics_preserve_sample_health_age_and_zero_without_authorizing_resources(status):
    rows = [{"node": "node-a", "unit": "whole_node", "states": {"unknown": 1}}]
    metrics = {"utilization": [{"node": "node-a", "cpu": reading(status, 0, 91), "cards_status": status}]}
    result = helper.merge_summary_metrics(rows, metrics)[0]
    assert result["metrics"]["cpu"] == reading(status, 0.0 if status == "ok" else None, 91.0)
    assert result["states"] == {"unknown": 1}
    assert result["metrics"]["does_not_authorize_resources"] is True


def test_summary_metrics_preserve_only_matching_bounded_errors_and_duplicate_readings_are_unknown():
    rows = [{"node": "node-a", "unit": "whole_node"}, {"node": "NODE-A", "unit": "whole_node"}]
    metrics = {
        "utilization": [{"node": "node-a"}, {"node": "node-a"}],
        "unread": [
            {"nodes": ["node-a"], "error": "http_503", "http_status": 503, "retry_after_seconds": 5},
            {"nodes": ["NODE-A"], "error": "metrics_not_read"},
        ],
    }
    result = helper.merge_summary_metrics(rows, metrics)
    assert result[0]["metrics"]["status"] == "invalid"
    assert result[0]["metrics"]["errors"] == [
        {"error": "ambiguous_node_metrics"},
        {"error": "http_503", "http_status": 503, "retry_after_seconds": 5},
    ]
    assert result[1]["metrics"]["errors"] == [{"error": "metrics_not_read"}]


@pytest.mark.parametrize("status", [401, 403])
def test_packaged_reuse_never_refreshes_failed_short_grant_more_than_once(case, packaged_metrics, status):
    module, client = packaged_metrics
    context = client.context()
    case.state.metrics_status = status
    with pytest.raises(module.ClientError) as error:
        client.metrics(["node-a"], context=context)
    assert error.value.status == status
    assert case.state.context_count == 2
    assert sum(row[0].startswith("/metrics/") for row in case.state.requests) == 2
