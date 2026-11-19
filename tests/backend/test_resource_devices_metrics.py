"""Paged card telemetry through the packaged SDK and synthetic localhost APIs."""

# ruff: noqa: F811 -- imported pytest fixture
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from backend.test_resource_agent_helper import KEY, case, helper  # noqa: F401


def cards(count):
    return [
        {"id": index, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
        for index in range(count)
    ]


def vector(values, *, memory=False):
    now = helper.time.time()

    def sample(name, value, **labels):
        return {"metric": {"__name__": "lockbot_" + name, "node": "host-a", **labels}, "value": [now, str(value)]}

    result = [
        sample("collector_success", 1, collector="accelerator"),
        sample("sample_timestamp_seconds", now, collector="accelerator"),
    ]
    for source_device, percent in values.items():
        result.append(sample("accelerator_utilization_percent", percent, device=source_device))
        if memory:
            result.extend(
                [
                    sample("accelerator_memory_used_bytes", 1024, device=source_device),
                    sample("accelerator_memory_total_bytes", 8192, device=source_device),
                ]
            )
    return result


def prepare(case, count=4, *, memory=False):
    case.state.bot_type = "QUEUE_DEVICE"
    case.state.resource_devices = cards(count)
    case.state.device_mapping = {f"node-a:dev{index}": f"GPU-source-{index:04}" for index in range(count)}
    case.state.metrics_rows = vector({f"GPU-source-{index:04}": index % 101 for index in range(count)}, memory=memory)
    return helper.resource_reference(case.connection, "node-a", width=64)


def source_requests(case):
    return [entry for entry in case.state.requests if entry[0] == "/metrics/api/v1/query"]


def invoke(case, *arguments, code=0):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(case.root / helper.SCRIPT_PATH),
            "--connection",
            str(case.path),
            *map(str, arguments),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == code, process.stderr
    assert KEY not in process.stdout + process.stderr
    assert "metrics-grant-" not in process.stdout + process.stderr
    assert len(process.stdout.encode()) <= 8192
    if code:
        assert not process.stdout
        return json.loads(process.stderr)
    assert not process.stderr
    value = json.loads(process.stdout)
    encoded = process.stdout
    assert all(
        token not in encoded
        for token in (
            '"source_node"',
            '"source_device"',
            '"resource_device"',
            '"data"',
            "GPU-source-",
            "GPU-peer-secret",
        )
    )
    return value


def page(case, reference, *args, fields="id,model,state,held_by_you,metrics", limit=20):
    return invoke(case, "resource", "devices", reference, "--limit", limit, "--json", fields, *args)["result"]


def assert_metrics_header(result):
    assert result["metrics_requested"] is True
    assert result["metrics_scope"] == "selected_node"
    assert result["metrics_does_not_authorize_resources"] is True
    assert result["http_requests"] <= 12


def packaged_module(case):
    spec = importlib.util.spec_from_file_location("paged_device_metrics_sdk", case.root / helper.SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compact_field_selection_does_not_fetch_without_explicit_flag(case):
    reference = prepare(case)
    result = page(case, reference, fields="id,metrics")
    assert result["rows"] == [{"id": index} for index in range(4)]
    assert "metrics_requested" not in result and not source_requests(case)
    assert result["http_requests"] == 3 and case.state.context_count == 1


def test_one_selected_node_query_preserves_zero_and_resource_ownership(case):
    reference = prepare(case, memory=True)
    case.state.resource_devices[0].update(lock_status="exclusive", holders=[{"external_user_id": "alice"}])
    result = page(case, reference, "--with-metrics")
    assert_metrics_header(result)
    assert result["scope_complete"] and result["metrics_read_complete"]
    assert result["http_requests"] == 4 and case.state.context_count == 1
    assert len(source_requests(case)) == 1
    first = result["rows"][0]
    assert first["state"] == "busy" and first["held_by_you"] is True
    assert first["metrics"]["percent"] == 0 and first["metrics"]["status"] == "ok"
    assert first["metrics"]["mapping_status"] == "mapped"
    assert first["metrics"]["memory"]["used"]["bytes"] == 1024
    assert first["metrics"]["sample_age_seconds"] >= 0
    assert all("device" not in row["metrics"] for row in result["rows"])
    assert not any(path.endswith("commands") for path, *_ in case.state.requests)


def test_explicit_flag_fetches_independently_of_selected_display_fields(case):
    reference = prepare(case)
    result = page(case, reference, "--with-metrics", fields="id")
    assert result["rows"] == [{"id": index} for index in range(4)]
    assert_metrics_header(result)
    assert result["metrics_read_complete"] and len(source_requests(case)) == 1


def test_paged_metrics_cover_ids_beyond_twenty_with_one_query_per_invocation(case):
    reference = prepare(case, count=96, memory=True)
    before = {str(path): path.read_bytes() for path in case.tmp.rglob("*") if path.is_file()}
    result = page(case, reference, "--with-metrics", fields="id,metrics", limit=256)
    found = []
    for invocations in range(1, 17):
        assert_metrics_header(result)
        assert result["http_requests"] == 4 and result["metrics_read_complete"]
        assert 0 < len(result["rows"]) <= 256
        assert len(source_requests(case)) == invocations
        for row in result["rows"]:
            assert row["metrics"]["percent"] == row["id"] % 101
            assert row["metrics"]["mapping_status"] == "mapped"
        found.extend(row["id"] for row in result["rows"])
        if not result["has_more"]:
            break
        if "next_command" in result:
            assert "--with-metrics" in result["next_command"]
            result = invoke(case, *result["next_command"])["result"]
        else:
            assert result["repeat_query_with_next_cursor"] is True
            result = page(
                case,
                reference,
                "--with-metrics",
                "--cursor",
                result["next_cursor"],
                fields="id,metrics",
                limit=256,
            )
    assert found == list(range(96)) and not result["has_more"]
    assert before == {str(path): path.read_bytes() for path in case.tmp.rglob("*") if path.is_file()}


def test_opaque_zero_zero_id_never_receives_numeric_zero_metrics(case):
    reference = prepare(case, count=2)
    case.state.resource_devices[1]["id"] = "00"
    case.state.device_mapping["node-a:dev00"] = "GPU-peer-secret"
    case.state.metrics_rows = vector({"GPU-source-0000": 0, "GPU-peer-secret": 99})
    result = page(case, reference, "--with-metrics", fields="id,metrics")
    rows = {str(row["id"]): row for row in result["rows"]}
    assert rows["0"]["metrics"]["percent"] == 0
    assert rows["00"]["metrics"]["percent"] is None
    assert rows["00"]["metrics"]["mapping_status"] == "unsupported_id"


def test_missing_explicit_mapping_is_unknown_not_equal_number_fallback(case):
    reference = prepare(case, count=2)
    case.state.device_mapping.pop("node-a:dev1")
    case.state.metrics_rows = vector({"GPU-source-0000": 0, "1": 88})
    result = page(case, reference, "--with-metrics", fields="id,metrics")
    assert result["rows"][0]["metrics"]["percent"] == 0
    assert result["rows"][1]["metrics"]["percent"] is None
    assert result["rows"][1]["metrics"]["mapping_status"] != "mapped"


@pytest.mark.parametrize("value", ["missing", None])
def test_missing_or_null_sample_stays_unknown_beside_real_zero(case, value):
    reference = prepare(case, count=2)
    case.state.metrics_rows = vector({"GPU-source-0000": 0})
    if value is None:
        sample = vector({"GPU-source-0001": 0})[-1]
        sample["value"][1] = None
        case.state.metrics_rows.append(sample)
    result = page(case, reference, "--with-metrics", fields="id,metrics")
    assert result["rows"][0]["metrics"]["percent"] == 0
    assert result["rows"][0]["metrics"]["status"] == "ok"
    assert result["rows"][1]["metrics"]["percent"] is None
    assert result["rows"][1]["metrics"]["status"] != "ok"
    assert len(source_requests(case)) == 1


def test_mine_returns_only_held_card_not_peer_metric_identity(case):
    reference = prepare(case, count=3)
    case.state.resource_devices[1].update(lock_status="exclusive", holders=[{"external_user_id": "alice"}])
    case.state.resource_devices[2].update(lock_status="exclusive", holders=[{"external_user_id": "peer-user"}])
    result = page(case, reference, "--mine", "--with-metrics", fields="id,held_by_you,metrics")
    assert [row["id"] for row in result["rows"]] == [1]
    assert result["rows"][0]["metrics"]["percent"] == 1
    assert "peer-user" not in json.dumps(result)
    assert len(source_requests(case)) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_source_failure_keeps_inventory_coverage_separate(case, status):
    reference = prepare(case)
    case.state.metrics_status = status
    result = page(case, reference, "--with-metrics")
    assert_metrics_header(result)
    assert result["scope_complete"] and result["snapshot_fresh"]
    assert result["metrics_read_complete"] is False
    assert result["metrics_unread_reasons"] == [f"http_{status}"]
    assert all(row["state"] == "idle" and row["metrics"]["percent"] is None for row in result["rows"])
    assert len(source_requests(case)) == 1
    if status == 429:
        assert result["metrics_retry_after_seconds"] == 40


def test_one_permitted_short_grant_refresh_reuses_same_inventory(case):
    reference = prepare(case)
    case.state.grant_fail = True
    result = page(case, reference, "--with-metrics")
    assert result["metrics_read_complete"] and result["http_requests"] == 6
    assert case.state.context_count == 2 and len(source_requests(case)) == 2
    assert sum(path == "/api/bots/7/resources" for path, *_ in case.state.requests) == 1
    assert [item[2]["Authorization"] for item in source_requests(case)] == [
        "Bearer metrics-grant-1",
        "Bearer metrics-grant-2",
    ]


@pytest.mark.parametrize("selection", ["model", "mine"])
def test_no_candidates_never_queries_metrics_source(case, selection):
    reference = prepare(case)
    args = ["--model", "P900"] if selection == "model" else ["--mine"]
    result = page(case, reference, "--with-metrics", *args)
    assert result["rows"] == [] and result["metrics_read_complete"]
    assert result["http_requests"] == 3 and not source_requests(case)


def test_field_discovery_with_flag_is_zero_io_even_without_key(case):
    prepare(case)
    case.path.with_name("resource.key").unlink()
    value = invoke(case, "resource", "devices", "--with-metrics", "--json")
    assert "metrics" in value["json_fields"] and value["required_row_fields"] == ["id"]
    assert not case.state.requests


def test_flag_is_omitted_from_legacy_query_and_bound_to_cursor_before_http(case):
    reference = prepare(case, count=4)
    old = {"model": None, "mine": False, "fields": ["id"], "limit": 1, "legacy_read": False}
    assert helper.resource_devices_query(fields=["id"], limit=1) == old
    assert helper.resource_devices_query(fields=["id"], limit=1, with_metrics=False) == old
    first = page(case, reference, fields="id", limit=1)
    case.state.requests.clear()
    error = invoke(
        case,
        "resource",
        "devices",
        reference,
        "--with-metrics",
        "--limit",
        1,
        "--json",
        "id",
        "--cursor",
        first["next_cursor"],
        code=2,
    )
    assert error["error"] == "resource_devices_cursor_query_changed" and not case.state.requests


def test_metrics_share_remaining_twelve_request_budget_with_inventory(case):
    reference = prepare(case)
    module = packaged_module(case)
    client = module.Client(case.path, root=case.root)
    client.calls = 9
    encoded = module.resource_devices(
        client, reference, query=module.resource_devices_query(fields=["id", "metrics"], with_metrics=True)
    )
    result = json.loads(encoded)["result"]
    assert result["http_requests"] == 12 and len(case.state.requests) == 3
    assert result["scope_complete"] and result["snapshot_fresh"]
    assert result["metrics_read_complete"] is False
    assert result["metrics_unread_reasons"] == ["request_budget_exhausted"]
    assert all(row["metrics"]["percent"] is None for row in result["rows"])
    assert not source_requests(case)


def test_metrics_share_global_deadline_and_aged_resources_stay_unknown(case, monkeypatch):
    reference = prepare(case)
    case.state.resource_devices[0].update(lock_status="exclusive", holders=[{"external_user_id": "alice"}])
    module = packaged_module(case)
    current = [0]
    client = module.Client(case.path, root=case.root, clock=lambda: current[0])
    original = client.metrics

    def expired_before_source(*args, **kwargs):
        current[0] = 61
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "metrics", expired_before_source)
    encoded = module.resource_devices(
        client,
        reference,
        query=module.resource_devices_query(fields=["id", "held_by_you", "metrics"], with_metrics=True),
    )
    result = json.loads(encoded)["result"]
    assert result["http_requests"] == 3 and not source_requests(case)
    assert result["metrics_read_complete"] is False
    assert result["metrics_unread_reasons"] == ["request_budget_exhausted"]
    assert result["snapshot_fresh"] is False and result["scope_complete"] is False
    assert result["rows"][0]["held_by_you"] is None
    assert result["rows"][0]["metrics"]["percent"] is None


def test_metrics_packing_retry_reages_ownership_without_extra_source_http(case, monkeypatch):
    reference = prepare(case, count=3)
    case.state.resource_devices[2].update(lock_status="exclusive", holders=[{"external_user_id": "alice"}])
    module = packaged_module(case)
    current, projections = [0], [0]
    original = module.observation_at_completion

    def age_before_final_projection(client, snapshot):
        projections[0] += 1
        if projections[0] == 3:
            current[0] = 6
        return original(client, snapshot)

    monkeypatch.setattr(module, "observation_at_completion", age_before_final_projection)
    client = module.Client(case.path, root=case.root, clock=lambda: current[0])
    query = module.resource_devices_query(
        mine=True, fields=["id", "held_by_you", "metrics"], limit=1, with_metrics=True
    )
    encoded = module.resource_devices(client, reference, query=query)
    result = json.loads(encoded)["result"]
    assert projections[0] >= 4 and len(source_requests(case)) == 1
    assert result["http_requests"] == 4 and result["metrics_read_complete"]
    assert not result["snapshot_fresh"] and not result["scope_complete"]
    assert result["rows"][0]["id"] == 0 and result["rows"][0]["held_by_you"] is None
    assert result["rows"][0]["ownership_match_unknown"]
    assert result["rows"][0]["metrics"]["percent"] == 0
    assert len(encoded.encode()) + 1 <= 8192


@pytest.mark.parametrize("elapsed", [0, 0.2])
def test_byte_packed_metrics_use_final_sample_age_without_refetch(case, monkeypatch, elapsed):
    reference = prepare(case, count=200, memory=True)
    module = packaged_module(case)
    current, projections = [0], [0]
    client = module.Client(case.path, root=case.root, clock=lambda: current[0])
    original_metrics = client.metrics
    original_observation = module.observation_at_completion

    def near_expiry(*args, **kwargs):
        reading = original_metrics(*args, **kwargs)

        def set_age(value):
            if isinstance(value, dict):
                if "sample_age_seconds" in value:
                    value["sample_age_seconds"] = 89.9
                for nested in value.values():
                    set_age(nested)
            elif isinstance(value, list):
                for nested in value:
                    set_age(nested)

        set_age(reading)
        return reading

    def packing_elapsed(client, snapshot):
        projections[0] += 1
        if projections[0] == 3:
            current[0] = elapsed
        return original_observation(client, snapshot)

    monkeypatch.setattr(client, "metrics", near_expiry)
    monkeypatch.setattr(module, "observation_at_completion", packing_elapsed)
    encoded = module.resource_devices(
        client,
        reference,
        query=module.resource_devices_query(fields=["id", "metrics"], limit=200, with_metrics=True),
    )
    result = json.loads(encoded)["result"]
    assert result["snapshot_fresh"] and result["age_seconds"] == elapsed
    assert result["metrics_read_complete"] and result["http_requests"] == 4
    assert len(source_requests(case)) == 1
    assert result["has_more"] and len(encoded.encode()) + 1 <= 8192
    assert [row["id"] for row in result["rows"]] == list(range(len(result["rows"])))
    for row in result["rows"]:
        reading = row["metrics"]
        assert reading["status"] == ("stale" if elapsed else "ok")
        assert reading["sample_age_seconds"] == pytest.approx(89.9 + elapsed)
        assert reading["percent"] == (None if elapsed else row["id"] % 101)
        for key, value in (("used", 1024), ("total", 8192)):
            memory = reading["memory"][key]
            assert memory["status"] == ("stale" if elapsed else "ok")
            assert memory["bytes"] == (None if elapsed else value)
            assert memory["sample_age_seconds"] == pytest.approx(89.9 + elapsed)
