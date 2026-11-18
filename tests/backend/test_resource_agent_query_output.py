"""Actual packaged queries keep useful state while reducing model-facing output."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import case, helper  # noqa: F401
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def invoke(case, *arguments):
    result = subprocess.run(
        [sys.executable, "-S", str(case.root / helper.SCRIPT_PATH), "--connection", str(case.path), *arguments],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert "metrics-grant-" not in result.stdout
    assert not (case.tmp / "cache").exists()
    return json.loads(result.stdout)["result"]


def test_actual_default_output_shrinks_without_new_requests_or_losing_scope(aggregate):
    states = [aggregate.server(number) for number in (1, 2)]
    for index, state in enumerate(states):
        aggregate.record(state, str(index))
    concise = aggregate.run("resources-all")["result"]
    for state in states:
        assert len(state.events) == 2
    full = aggregate.run("resources-all", "--full")["result"]
    assert concise["http_requests"] == full["http_requests"] == 4
    assert concise["complete"] == full["complete"] is True
    for short, detailed in zip(concise["groups"], full["groups"], strict=True):
        assert short["identity"] == detailed["identity"]
        context = short["snapshot"]["context"]
        assert "skill" not in context and "links" not in context
        assert set(context["server"]) <= {"service_id", "instance_id", "api_version", "server_version"}
        assert context["actor"] == detailed["snapshot"]["context"]["actor"]
        assert context["bot_rules"] == detailed["snapshot"]["context"]["bot_rules"]
        assert short["snapshot"]["health"] == detailed["snapshot"]["health"]
        assert short["snapshot"]["resources"][0]["holders"]
        assert short["snapshot"]["snapshot_fresh_for_write"] is True
        assert "skill" in detailed["snapshot"]["context"]
    assert len(json.dumps(concise)) < len(json.dumps(full)) * 0.6


@pytest.mark.parametrize("model", [None, "P800"])
def test_alias_matches_remain_separate_and_model_unknown_is_not_a_nonmatch(aggregate, model):
    first, second = aggregate.server(1), aggregate.server(2)
    for index, state in enumerate((first, second)):
        aggregate.record(state, str(index))
        state.extra["resources"] = [
            {
                "id": "shared-id",
                "alias": "19",
                "lock_status": "idle",
                "holders": [],
                "booking_list": [],
                "accelerators": [{"model": "P800", "count": 8}] if index == 0 else [],
            },
            {
                "id": "unrelated",
                "alias": "119",
                "lock_status": "idle",
                "holders": [],
                "booking_list": [],
                "accelerators": [{"model": "P900", "count": 8}],
            },
        ]
    arguments = ["resources-all", "--node", "19", *(["--model", model] if model else [])]
    result = aggregate.run(*arguments)["result"]
    assert result["complete"] and result["http_requests"] == 4
    assert len(result["groups"]) == 2
    for group in result["groups"]:
        assert group["snapshot"]["selection"]["full_inventory"] is False
        assert all(row["id"] != "unrelated" for row in group["snapshot"]["resources"])
        assert all(row["node"] != "unrelated" for row in group["snapshot"]["inventory_overview"]["nodes"])
    assert len(result["groups"][0]["snapshot"]["resources"]) == 1
    if model:
        assert result["groups"][1]["snapshot"]["resources"] == []
        assert result["groups"][1]["snapshot"]["selection"]["unknown_hardware_nodes"] == ["shared-id"]
    else:
        assert len(result["groups"][1]["snapshot"]["resources"]) == 1


@pytest.mark.parametrize("status", [200, 429, 503])
def test_resources_and_metrics_share_one_invocation_and_preserve_optional_errors(case, status):
    case.state.metrics_status = status
    result = invoke(case, "resources", "--node", "node-a", "--with-metrics")
    assert result["resources"][0]["id"] == "node-a"
    assert result["context"]["actor"] == case.state.actor
    metrics = result["metrics"]
    assert metrics["complete"] is (status == 200)
    if status != 200:
        assert metrics["unread"][0]["error"] == f"http_{status}"
        assert metrics["utilization"] == []
    else:
        assert metrics["utilization"][0]["node"] == "node-a"
    assert sum(path == "/metrics/api/v1/query" for path, *_ in case.state.requests) == 1
    assert not any(path.endswith("/commands") for path, *_ in case.state.requests)


def test_default_metrics_keep_memory_and_filesystem_readings_without_raw_vector(case):
    now = helper.time.time()

    def sample(name, value, **labels):
        return {"metric": {"__name__": "lockbot_" + name, "node": "host-a", **labels}, "value": [now, str(value)]}

    case.state.metrics_rows = [
        sample("collector_success", 1, collector="host"),
        sample("sample_timestamp_seconds", now - 2, collector="host"),
        sample("collector_success", 1, collector="accelerator"),
        sample("sample_timestamp_seconds", now - 120, collector="accelerator"),
        sample("host_cpu_utilization_percent", 0),
        sample("host_memory_used_bytes", 2048),
        sample("host_memory_total_bytes", 8192),
        sample("host_memory_available_bytes", 4096),
        sample("host_memory_cache_bytes", 1024),
        sample("host_filesystem_used_bytes", 16384, filesystem="data"),
        sample("host_filesystem_total_bytes", 65536, filesystem="data"),
        sample("accelerator_utilization_percent", 3, device="0"),
        sample("accelerator_memory_used_bytes", 2048, device="0"),
        sample("accelerator_memory_total_bytes", 4096, device="0"),
    ]
    result = invoke(case, "metrics", "--node", "node-a")
    assert "data" not in result
    reading = result["utilization"][0]
    assert reading["cpu"]["percent"] == 0
    assert reading["memory"]["used"]["bytes"] == 2048
    assert reading["memory"]["available"]["bytes"] == 4096
    assert reading["filesystems"][0]["total"]["bytes"] == 65536
    card = reading["cards"][0]
    assert card["status"] == card["memory"]["used"]["status"] == "stale"
    assert card["memory"]["used"]["bytes"] is None
    detailed = invoke(case, "metrics", "--node", "node-a", "--full")
    assert detailed["data"]["data"]["result"] == case.state.metrics_rows


def test_output_projection_does_not_change_the_verified_snapshot():
    snapshot = {
        "resources": [{"id": "19", "holders": []}],
        "snapshot_fresh_for_write": True,
        "context": {
            "server": {"instance_id": "scope", "download_url": "/bundle"},
            "skill": {"package_version": "version"},
            "bot_rules": {"default_duration_seconds": 7200},
        },
    }
    original = json.dumps(snapshot, sort_keys=True)
    projected = helper.inventory_output(helper.select_inventory(snapshot, nodes=["19"]))
    assert projected["context"]["bot_rules"]["default_duration_seconds"] == 7200
    assert projected["context"]["server"] == {"instance_id": "scope"}
    assert json.dumps(snapshot, sort_keys=True) == original


@pytest.mark.parametrize("hardware", [{"devices": [{"dev_model": "P900"}, None]}, {"accelerators": [None]}])
def test_malformed_hardware_stays_unknown_when_filtering(hardware):
    snapshot = {"resources": [{"id": "19", **hardware}], "context": {}}
    result = helper.select_inventory(snapshot, model="P800")
    assert result["resources"] == []
    assert result["selection"]["unknown_hardware_nodes"] == ["19"]


@pytest.mark.parametrize("error", [AttributeError, TypeError, ValueError, KeyError])
def test_malformed_optional_metrics_cannot_erase_verified_resources(error):
    snapshot = {"resources": [{"id": "19"}], "snapshot_fresh_for_write": True}

    def malformed(_nodes):
        raise error("untrusted source contents must not appear in the error")

    result = helper.attach_metrics(SimpleNamespace(metrics=malformed), snapshot)
    assert result["resources"] == snapshot["resources"]
    assert result["metrics"]["complete"] is False
    assert result["metrics"]["unread"] == [{"nodes": ["19"], "error": "invalid_metrics_response"}]
    assert "untrusted" not in json.dumps(result)


def test_slow_optional_telemetry_ages_resource_freshness_before_display():
    snapshot = {
        "resources": [{"id": "19", "lock_status": "idle", "holders": []}],
        "age_seconds": 1,
        "snapshot_fresh_for_write": True,
        "inventory_overview": {"snapshot_fresh": True},
        "context": {"bot": {"type": "NODE"}},
    }
    clock = [0]

    def metrics(_nodes):
        clock[0] = 6
        return {"utilization": []}

    client = SimpleNamespace(metrics=metrics, clock=lambda: clock[0], _resource_request_started=0)
    combined = helper.attach_metrics(client, snapshot)
    result = helper.observation_at_completion(client, combined)
    assert result["age_seconds"] == 7
    assert result["snapshot_fresh_for_write"] is False
    assert result["inventory_overview"]["snapshot_fresh"] is False
    assert result["inventory_overview"]["nodes"][0]["groups"][0]["unknown"] == ["19"]
    assert snapshot["age_seconds"] == 1


def test_metrics_batch_uses_one_call_for_many_selected_nodes():
    calls = []

    def metrics(nodes):
        calls.append(nodes)
        return {"utilization": [{"node": node} for node in nodes]}

    snapshot = {"resources": [{"id": str(index)} for index in range(10)]}
    result = helper.attach_metrics(SimpleNamespace(metrics=metrics), snapshot)
    assert len(calls) == 1 and len(calls[0]) == 10
    assert len(result["metrics"]["utilization"]) == 10
