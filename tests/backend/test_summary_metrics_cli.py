"""Optional scoped summary telemetry is bounded and preserves resource evidence."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import case, helper  # noqa: F401


def invoke(case, *options):
    process = subprocess.run(
        [
            sys.executable,
            "-S",
            str(case.root / helper.SCRIPT_PATH),
            "--connection",
            str(case.path),
            "resources",
            "--summary",
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert "lbr_" not in process.stdout and "metrics-grant-" not in process.stdout
    assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    return json.loads(process.stdout)["result"]


def samples():
    now = helper.time.time()

    def sample(metric, value, **labels):
        return {"metric": {"__name__": "lockbot_" + metric, "node": "host-a", **labels}, "value": [now, str(value)]}

    return [
        sample("collector_success", 1, collector="host"),
        sample("sample_timestamp_seconds", now, collector="host"),
        sample("host_cpu_utilization_percent", 0),
        sample("collector_success", 1, collector="accelerator"),
        sample("sample_timestamp_seconds", now, collector="accelerator"),
        sample("accelerator_utilization_percent", 35, device="0"),
        sample("accelerator_utilization_percent", 99, device="1"),
    ]


def test_scoped_summary_metrics_reuse_verified_context_without_redundant_get(case):
    case.state.metrics_rows = samples()
    result = invoke(case, "--with-metrics")
    assert case.state.context_count == 1
    assert sum(path == "/api/bots/7/resources" for path, *_ in case.state.requests) == 1
    assert sum(path == "/metrics/api/v1/query" for path, *_ in case.state.requests) == 1
    assert result["metrics_requested"] and result["metrics_read_complete"]
    row = result["rows"][0]
    assert row["metrics"]["cpu"]["percent"] == 0
    assert row["metrics"]["does_not_authorize_resources"] is True
    assert row["states"]["idle"] == 1 and "source_node" not in row["metrics"]
    assert not any(path.endswith("commands") for path, *_ in case.state.requests)


def test_personal_card_telemetry_does_not_expand_to_peer_cards(case):
    case.state.bot_type = "DEVICE"
    case.state.device_mapping = {"node-a:dev0": "0", "node-a:dev1": "1"}
    case.state.metrics_rows = samples()
    case.state.resource_devices = [
        {
            "id": 0,
            "dev_model": "P800",
            "lock_status": "exclusive",
            "holders": [{"external_user_id": "alice", "remaining_seconds": 7200}],
        },
        {"id": 1, "dev_model": "H20", "lock_status": "exclusive", "holders": [{"external_user_id": "peer"}]},
    ]
    result = invoke(case, "--mine", "--with-metrics", "--json", "node,holdings,metrics")
    row = result["rows"][0]
    assert {child["device"] for child in row["holdings"]} == {0}
    assert [reading["device"] for reading in row["metrics"]["cards"]] == ["0"]
    assert row["metrics"]["cards"][0]["percent"] == 35
    assert case.state.context_count == 1 and result["view"] == "own"


@pytest.mark.parametrize("status", [429, 503])
def test_optional_telemetry_failure_is_not_a_resource_query_failure(case, status):
    case.state.metrics_status = status
    result = invoke(case, "--with-metrics")
    row = result["rows"][0]
    assert result["scope_complete"] and not result["metrics_read_complete"]
    assert row["states"]["idle"] == 1 and row["metrics"]["status"] == "missing"
    assert row["metrics"]["errors"][0]["http_status"] == status
    assert case.state.context_count == 1


def test_summary_telemetry_queries_only_a_bounded_prefix_and_ages_the_original_observation():
    context = {
        "server": {"instance_id": "synthetic-instance"},
        "bot": {"uuid": "synthetic-bot", "type": "NODE"},
        "actor": {"platform": "infoflow", "external_user_id": "synthetic-user"},
    }
    snapshot = {
        "context": context,
        "health": "running",
        "age_seconds": 0,
        "snapshot_fresh_for_write": True,
        "resources": [{"id": f"node-{index:04}", "lock_status": "idle", "holders": []} for index in range(1000)],
    }
    calls, clock = [], [0]

    def metrics(nodes):
        calls.append(nodes)
        clock[0] = 7
        return {
            "utilization": [
                {"node": node, "cpu": {"status": "ok", "percent": 0, "sample_age_seconds": 1}} for node in nodes
            ]
        }

    client = SimpleNamespace(
        base="http://127.0.0.1:8155", secrets=[], metrics=metrics, clock=lambda: clock[0], _resource_request_started=0
    )
    combined = helper.attach_summary_metrics(client, snapshot)
    assert len(calls) == 1 and len(calls[0]) == 20
    final = helper.observation_at_completion(client, combined)
    output = helper.query_summary_output({"result": final, "_approved_origin": client.base})
    result = json.loads(output)["result"]
    assert result["has_more"] and len(output.encode()) + 1 <= helper.SUMMARY_BYTE_LIMIT
    assert all(row["snapshot_fresh"] is False and row["states"]["unknown"] == 1 for row in result["rows"])
    assert snapshot["age_seconds"] == 0 and "metrics" not in snapshot
