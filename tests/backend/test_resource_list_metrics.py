"""Bounded opt-in aggregate telemetry against synthetic localhost services only."""

# ruff: noqa: F811 -- imported pytest fixtures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from lockbot.metrics.gateway import validate_query

from backend.test_resource_agent_helper import KEY, case, helper  # noqa: F401
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def inventory_nodes(count, *, prefix="node-"):
    return [{"id": prefix + str(index).zfill(3), "lock_status": "idle", "holders": [], "booking_list": []}
            for index in range(count)]


def metric_rows(source, values=(), *, cpu=0):
    now = helper.time.time()

    def sample(name, value, **labels):
        return {"metric": {"__name__": "lockbot_" + name, "node": source, **labels},
                "value": [now, str(value)]}

    rows = [sample("collector_success", 1, collector="host"),
            sample("sample_timestamp_seconds", now, collector="host"),
            sample("host_cpu_utilization_percent", cpu),
            sample("collector_success", 1, collector="accelerator"),
            sample("sample_timestamp_seconds", now, collector="accelerator")]
    rows.extend(sample("accelerator_utilization_percent", value, device=str(device))
                for device, value in values if value is not None)
    return rows


@pytest.fixture
def metrics_source():
    state = SimpleNamespace(tokens=set(), rows=[], events=[], status=200, retry_after=None, observe=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            query = urllib.parse.parse_qs(body.decode())["query"][0]
            allowed = {row["metric"]["node"] for row in state.rows}
            names, nodes = validate_query(query, allowed)
            authorization = self.headers.get("Authorization")
            state.events.append({"path": self.path, "authorization": authorization,
                                 "nodes": set(nodes), "query": query})
            if state.observe is not None:
                state.observe()
            status = state.status if authorization in state.tokens else 401
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 429 and state.retry_after is not None:
                self.send_header("Retry-After", str(state.retry_after))
            self.end_headers()
            rows = [row for row in state.rows if row["metric"]["node"] in nodes and row["metric"]["__name__"] in names]
            payload = {"status": "success", "data": {"resultType": "vector", "result": rows}}
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.origin = "http://127.0.0.1:" + str(server.server_port)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def prepare(aggregate, source, number=1, *, names=None, bot_type="NODE", cards=None, model="P800"):
    """Place source metadata in a coherent, service-verified context before SDK reads."""
    state = aggregate.server(number)
    state.bot_type = bot_type
    path = aggregate.record(state, "scope-" + str(number))
    request = urllib.request.Request(state.origin + "/api/agent/context",
                                     headers={"Authorization": "Bearer " + state.key})
    with urllib.request.urlopen(request, timeout=5) as response:
        context = json.load(response)
    names = names or ["node-000"]
    mapping = {name: "source-" + str(number) + "-" + str(index) for index, name in enumerate(names)}
    device_mapping = {}
    records = inventory_nodes(len(names))
    for row, name in zip(records, names, strict=True):
        row["id"] = name
    if cards is not None:
        assert len(names) == 1
        records[0]["devices"] = [
            {"id": index, "dev_model": model, "lock_status": "idle", "holders": [], "booking_list": []}
            for index in range(cards)]
        device_mapping = {names[0] + ":dev" + str(index): "collector-" + str(index) for index in range(cards)}
    credential = "synthetic-metrics-grant-" + str(number) + "-" + "x" * 32
    source.tokens.add("Bearer " + credential)
    context["utilization"] = {
        "configuration": {"type": "prometheus", "profile": "lockbot-v1", "accessMode": "short_lived",
                          "endpoint": source.origin, "nodeMapping": mapping, "deviceMapping": device_mapping},
        "access": {"scope": "metrics:read", "token": credential}}
    state.extra.update(context=context, resources=records)
    config = json.loads(path.read_text())
    config["approved_metrics_origin"] = source.origin
    path.write_text(json.dumps(config))
    for target in mapping.values():
        source.rows.extend(metric_rows(target, [("collector-0", 0), ("collector-1", 60)]))
    state.events.clear()  # Preparation request is outside the SDK invocation being measured.
    return state, path, mapping


def execute(aggregate, *arguments, code=0):
    before = {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}
    completed = subprocess.run([sys.executable, "-I", "-S", str(aggregate.script), *map(str, arguments)],
                               env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
                               capture_output=True, text=True, timeout=25)
    assert completed.returncode == code, completed.stderr
    assert before == {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}
    assert all(state.key not in completed.stdout + completed.stderr for state in aggregate.states)
    assert "synthetic-metrics-grant-" not in completed.stdout + completed.stderr
    assert len(completed.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    return json.loads(completed.stderr if code else completed.stdout)


def listing(aggregate, *arguments, fields="node,states,metrics", limit=20):
    return execute(aggregate, "resource", "list", "--with-metrics", "--json", fields,
                   "--limit", str(limit), *arguments)["result"]


def read_case(case, *, max_calls=12, clock=helper.time.monotonic, **options):
    catalog = case.tmp / "catalog"
    catalog.mkdir(mode=0o700, exist_ok=True)
    directory = catalog / "only"
    directory.mkdir(mode=0o700, exist_ok=True)
    for filename in ("connection.json", "resource.key"):
        shutil.copyfile(case.tmp / filename, directory / filename)
        (directory / filename).chmod(0o600)
    query = helper.resource_list_query(fields=["node", "states", "metrics"], with_metrics=True)
    encoded = helper.resource_list(catalog, query=query, root=case.root, max_calls=max_calls, clock=clock, **options)
    assert KEY not in encoded and "metrics-grant-" not in encoded
    return json.loads(encoded)["result"]


def compact(metrics):
    assert metrics["best_effort"] is True and metrics["does_not_authorize_resources"] is True
    assert "cards" not in metrics and "filesystems" not in metrics and "data" not in metrics
    encoded = json.dumps(metrics)
    assert "source_node" not in encoded and "resource_device" not in encoded and "nodeMapping" not in encoded
    return metrics["accelerator"]


def test_false_telemetry_mode_preserves_existing_query_shape_and_digest():
    old = {"nodes": [], "model": None, "fields": None, "limit": 20, "mine": False, "legacy_read": False}
    query = helper.resource_list_query(with_metrics=False)
    assert query == old
    raw = json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    expected = hashlib.sha256(raw).hexdigest()
    assert helper.resource_list_query_digest(query) == expected
    enabled = helper.resource_list_query(with_metrics=True)
    assert enabled == {**old, "with_metrics": True}
    assert helper.resource_list_query_digest(enabled) != expected


def test_bare_json_with_metrics_discovers_fields_without_catalog_key_or_http(aggregate):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    path.with_name("key").unlink()
    result = execute(aggregate, "resource", "list", "--with-metrics", "--json")
    assert "metrics" in result["json_fields"] and state.events == []


@pytest.mark.parametrize("global_option", ["--resource-sources", "--resource-provider"])
def test_unadvertised_external_telemetry_rejects_before_loading_configuration(aggregate, global_option):
    result = execute(aggregate, global_option, aggregate.home / "missing-private-config",
                     "resource", "list", "--with-metrics", "--json", "node,metrics", code=2)
    assert "error" in result
    assert "missing-private-config" not in json.dumps(result)
    assert aggregate.states == []


def test_aggregate_uses_verified_context_once_and_keeps_whole_node_source_coverage_explicit(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    result = listing(aggregate)
    assert result["http_requests"] == 3 and len(state.events) == 2 and len(metrics_source.events) == 1
    assert result["scope_complete"] and result["metrics_requested"] and result["metrics_read_complete"]
    assert result.get("metrics_retry_after_seconds") is None and result["metrics_unread_reasons"] == []
    row = result["rows"][0]
    assert row["states"]["idle"] == 1 and row["metrics"]["cpu"]["percent"] == 0
    accelerator = compact(row["metrics"])
    assert accelerator["mean_percent"] == 30 and accelerator["ok_devices"] == 2
    assert accelerator["selected_devices"] is None and accelerator["selection_complete"] is False
    assert accelerator["device_identity"] == "source" and accelerator["status"] == "partial"
    assert not any(path == "/api/agent/context" for path, *_ in state.events)


def test_selecting_metrics_field_alone_never_makes_a_source_request(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    result = execute(aggregate, "resource", "list", "--json", "node,metrics")["result"]
    assert result["http_requests"] == len(state.events) == 2 and metrics_source.events == []
    assert not result.get("metrics_requested", False)
    assert "metrics" not in result["rows"][0]


def test_explicit_telemetry_is_allowed_even_when_display_fields_omit_metrics(aggregate, metrics_source):
    _state, _path, _mapping = prepare(aggregate, metrics_source)
    result = listing(aggregate, fields="node")
    assert result["metrics_requested"] and len(metrics_source.events) == 1
    assert "metrics" not in result["rows"][0]


def test_encoder_never_queries_and_only_candidate_page_prefix_gets_telemetry(aggregate, metrics_source):
    state, _path, mapping = prepare(aggregate, metrics_source, names=[row["id"] for row in inventory_nodes(45)])
    later, _later_path, _later_mapping = prepare(aggregate, metrics_source, 2)
    result = listing(aggregate)
    assert len(metrics_source.events) == 1 and later.events == []
    assert 0 < len(result["rows"]) <= 20
    queried = metrics_source.events[0]["nodes"]
    assert queried <= {mapping[row["id"]] for row in inventory_nodes(20)}
    assert {mapping[row["node"]] for row in result["rows"]} <= queried
    assert result["http_requests"] == len(state.events) + len(metrics_source.events) == 3
    assert "--with-metrics" in result["next_command"]


def test_metrics_packing_cursor_never_skips_unreturned_live_rows(aggregate, metrics_source):
    names = ["机器" * 12 + "-" + str(index).zfill(3) for index in range(45)]
    state, _path, _mapping = prepare(aggregate, metrics_source, names=names)
    result, found = listing(aggregate), []
    for _ in range(12):
        found.extend(row["node"] for row in result["rows"])
        if not result["has_more"]:
            break
        result = execute(aggregate, *result["next_command"])["result"]
    else:
        pytest.fail("metric packing continuation did not progress")
    assert found == names and len(set(found)) == 45
    assert len(metrics_source.events) <= 12
    assert all(method == "GET" for _path, method, _auth in state.events)


def test_changed_metrics_mode_is_rejected_before_any_http(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source, names=[row["id"] for row in inventory_nodes(25)])
    first = listing(aggregate)
    before = len(state.events), len(metrics_source.events)
    result = execute(aggregate, "resource", "list", "--json", "node,states,metrics", "--limit", "20",
                     "--cursor", first["next_cursor"], code=2)
    assert result["error"] == "resource_list_cursor_query_changed"
    assert (len(state.events), len(metrics_source.events)) == before


@pytest.mark.parametrize("mine", [False, True])
def test_card_selection_includes_valid_ids_beyond_summary_twenty(aggregate, metrics_source, mine):
    state, _path, mapping = prepare(aggregate, metrics_source, bot_type="QUEUE_DEVICE", cards=31)
    devices = state.extra["resources"][0]["devices"]
    devices[-1]["dev_model"] = "H20"
    if mine:
        for device in devices:
            device["lock_status"] = "exclusive"
            device["holders"] = [{"external_user_id": state.actor["external_user_id"], "remaining_seconds": 7200}]
    target = next(iter(mapping.values()))
    metrics_source.rows = metric_rows(target, [("collector-" + str(index), index) for index in range(31)])
    result = listing(aggregate, "--model", "P800", *(["--mine"] if mine else []))
    accelerator = compact(result["rows"][0]["metrics"])
    assert accelerator["selected_devices"] == accelerator["ok_devices"] == 30
    assert accelerator["non_ok_devices"] == 0 and accelerator["mean_percent"] == pytest.approx(14.5)
    assert accelerator["selection_complete"] is True and accelerator["device_identity"] == "resource"
    assert accelerator["status"] == "ok"


def test_accelerator_average_uses_only_valid_ok_samples_and_retains_unknown_counts(aggregate, metrics_source):
    _state, _path, mapping = prepare(aggregate, metrics_source, bot_type="DEVICE", cards=4)
    target = next(iter(mapping.values()))
    metrics_source.rows = metric_rows(target, [("collector-0", 0), ("collector-1", 40),
                                              ("collector-2", "NaN"), ("collector-3", None)])
    result = listing(aggregate)
    accelerator = compact(result["rows"][0]["metrics"])
    assert accelerator["selected_devices"] == 4 and accelerator["ok_devices"] == 2
    assert accelerator["non_ok_devices"] == 2 and accelerator["mean_percent"] == 20
    assert accelerator["sample_states"]["ok"] == 2
    assert accelerator["sample_states"]["invalid"] == 1 and accelerator["sample_states"]["missing"] == 1
    assert accelerator["status"] == "partial"


@pytest.mark.parametrize("status,retry", [(429, 40), (503, None)])
def test_source_failure_does_not_turn_complete_idle_inventory_into_partial_inventory(
    aggregate, metrics_source, status, retry
):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    metrics_source.status, metrics_source.retry_after = status, retry
    result = listing(aggregate)
    assert result["scope_complete"] and result["rows"][0]["states"]["idle"] == 1
    assert result["metrics_requested"] and not result["metrics_read_complete"]
    assert "http_" + str(status) in result["metrics_unread_reasons"]
    assert result.get("metrics_retry_after_seconds") == retry
    assert result["http_requests"] == len(state.events) + len(metrics_source.events) == 3
    assert len(metrics_source.events) == 1


def test_grant_refresh_consumes_shared_reader_budget_and_updates_counter_on_failure(case):
    case.state.grant_fail = True
    case.state.metrics_rows = metric_rows("host-a", [("0", 0)])
    result = read_case(case, max_calls=5)
    assert result["http_requests"] == len(case.state.requests) == 5
    assert sum(row[0].startswith("/metrics/") for row in case.state.requests) == 1
    assert case.state.context_count == 2
    assert result["scope_complete"] and result["rows"][0]["states"]["idle"] == 1
    assert not result["metrics_read_complete"] and "request_budget_exhausted" in result["metrics_unread_reasons"]


def test_successful_grant_refresh_has_no_fresh_per_source_allowance(case):
    case.state.grant_fail = True
    case.state.metrics_rows = metric_rows("host-a", [("0", 0)])
    result = read_case(case, max_calls=6)
    assert result["http_requests"] == len(case.state.requests) == 6
    assert sum(row[0].startswith("/metrics/") for row in case.state.requests) == 2
    assert case.state.context_count == 2 and result["metrics_read_complete"]


def test_shared_deadline_prevents_source_io_and_reages_resource_evidence(case, monkeypatch):
    clock = [0]
    original = helper.Client.metrics

    def late(self, *args, **kwargs):
        clock[0] = 61
        return original(self, *args, **kwargs)

    monkeypatch.setattr(helper.Client, "metrics", late)
    result = read_case(case, clock=lambda: clock[0])
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)
    assert result["http_requests"] == len(case.state.requests) == 3
    assert not result["metrics_read_complete"] and not result["scope_complete"]
    assert result["rows"][0]["states"]["unknown"] == 1


def test_conflicting_cohort_finishes_inventory_without_fetching_any_metrics(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    original_key = state.key
    second_key = "lbr_" + "z" * 32
    state.key = second_key
    second = aggregate.record(state, "same-scope-second-key")
    state.key, state.accepted_keys = original_key, [second_key]
    second_config = json.loads(second.read_text())
    second_config["approved_metrics_origin"] = metrics_source.origin
    second.write_text(json.dumps(second_config))
    state.extra_per_key = {"Bearer " + second_key: {"resources": [
        {"id": "node-000", "lock_status": "exclusive", "holders": [{"external_user_id": "peer"}], "booking_list": []}]}}
    result = listing(aggregate)
    assert sum(path == "/api/agent/resources" for path, *_ in state.events) == 2
    assert metrics_source.events == []
    assert result["rows"][0]["observation_conflict"] is True
    assert not result["metrics_read_complete"]


def test_identical_cohort_inventory_is_finished_before_any_credential_queries_metrics(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    original_key, second_key = state.key, "lbr_" + "z" * 32
    state.key = second_key
    second = aggregate.record(state, "same-scope-second-key")
    state.key, state.accepted_keys = original_key, [second_key]
    config = json.loads(second.read_text())
    config["approved_metrics_origin"] = metrics_source.origin
    second.write_text(json.dumps(config))
    observed = []
    metrics_source.observe = lambda: observed.append(sum(path == "/api/agent/resources" for path, *_ in state.events))
    result = listing(aggregate)
    assert result["displayed_nodes"] == 1 and observed == [2]



@pytest.mark.parametrize("source_label,source_rows", [
    ("collector-1", [("collector-0", 99), ("collector-1", 5)]),
    ("GPU-8ad3a749-61d4-4c2a-80e2-cc51fb917df9", [
        ("collector-0", 99), ("GPU-8ad3a749-61d4-4c2a-80e2-cc51fb917df9", 5)]),
])
def test_model_filtered_compact_average_respects_source_mapping_not_numeric_identity(
    aggregate, metrics_source, source_label, source_rows
):
    state, _path, mapping = prepare(aggregate, metrics_source, bot_type="DEVICE", cards=2)
    state.extra["resources"][0]["devices"][1]["dev_model"] = "H20"
    configured = state.extra["context"]["utilization"]["configuration"]["deviceMapping"]
    configured["node-000:dev0"], configured["node-000:dev1"] = source_label, "collector-0"
    metrics_source.rows = metric_rows(next(iter(mapping.values())), source_rows)
    result = listing(aggregate, "--model", "P800")
    accelerator = compact(result["rows"][0]["metrics"])
    assert accelerator["selected_devices"] == accelerator["ok_devices"] == 1
    assert accelerator["mean_percent"] == 5
    assert source_label not in json.dumps(result["rows"][0]["metrics"])



def test_missing_device_mapping_cannot_create_identity_fallback_compact_mean(aggregate, metrics_source):
    state, _path, mapping = prepare(aggregate, metrics_source, bot_type="DEVICE", cards=1)
    state.extra["context"]["utilization"]["configuration"]["deviceMapping"] = {}
    metrics_source.rows = metric_rows(next(iter(mapping.values())), [("0", 88)])
    result = listing(aggregate)
    accelerator = compact(result["rows"][0]["metrics"])
    assert accelerator["ok_devices"] == 0 and accelerator["mean_percent"] is None
    assert accelerator["status"] == "missing"



def test_telemetry_counter_is_shared_across_scopes_and_resume_has_no_hidden_allowance(aggregate, metrics_source):
    states = [prepare(aggregate, metrics_source, number + 1)[0] for number in range(5)]
    first = listing(aggregate)
    actual = sum(len(state.events) for state in states) + len(metrics_source.events)
    assert actual == first["http_requests"] == 12
    assert len(first["rows"]) == 4 and first["has_more"] and states[-1].events == []
    assert len(metrics_source.events) == 4
    tail = execute(aggregate, *first["next_command"])["result"]
    assert tail["http_requests"] == 3 and len(tail["rows"]) == 1 and not tail["has_more"]
    assert all(len(state.events) == 2 for state in states)
    assert len(metrics_source.events) == 5


def test_later_scope_reages_metrics_with_source_threshold_without_expiring_fresh_inventory(
    aggregate, metrics_source, monkeypatch
):
    first, first_path, _mapping = prepare(aggregate, metrics_source, 1)
    second, _second_path, _second_mapping = prepare(aggregate, metrics_source, 2)
    for state in (first, second):
        state.extra["context"]["utilization"]["configuration"]["staleSeconds"] = 30
    for metric in metrics_source.rows:
        if metric["metric"]["__name__"] == "lockbot_sample_timestamp_seconds":
            metric["value"][1] = str(float(metric["value"][1]) - 29)
    clock = [0]
    original = helper._ResourceListReader.telemetry

    def telemetry(reader, groups, nodes, *, model=None):
        if groups[0]["_client"].config["bot_uuid"] == second.bot_uuid:
            clock[0] = 2
        return original(reader, groups, nodes, model=model)

    monkeypatch.setattr(helper._ResourceListReader, "telemetry", telemetry)
    query = helper.resource_list_query(fields=["node", "states", "metrics"], with_metrics=True)
    encoded = helper.resource_list(aggregate.catalog, query=query, root=aggregate.script.parents[1],
                                   clock=lambda: clock[0])
    result = json.loads(encoded)["result"]
    reference = helper.resource_reference(json.loads(first_path.read_text()), "node-000", width=64)
    rows = {row["ref"]: row for row in result["rows"]}
    earlier = rows[reference]
    later = next(row for ref, row in rows.items() if ref != reference)
    assert result["scope_complete"] and result["metrics_read_complete"]
    assert earlier["states"]["idle"] == later["states"]["idle"] == 1
    assert earlier["metrics"]["cpu"]["status"] == "stale"
    assert earlier["metrics"]["cpu"]["percent"] is None
    assert earlier["metrics"]["cpu"]["sample_age_seconds"] >= 31
    assert earlier["metrics"]["accelerator"]["mean_percent"] is None
    assert later["metrics"]["cpu"]["status"] == "ok" and later["metrics"]["cpu"]["percent"] == 0


def test_same_node_and_source_endpoint_do_not_merge_different_principals_or_grants(aggregate, metrics_source):
    first, first_path, first_mapping = prepare(aggregate, metrics_source, 1)
    second, second_path, second_mapping = prepare(aggregate, metrics_source, 2)
    metrics_source.rows = [*metric_rows(next(iter(first_mapping.values())), [("0", 10)], cpu=10),
                           *metric_rows(next(iter(second_mapping.values())), [("0", 80)], cpu=80)]
    result = listing(aggregate)
    rows = {row["ref"]: row for row in result["rows"]}
    for state, path, expected in ((first, first_path, 10), (second, second_path, 80)):
        ref = helper.resource_reference(json.loads(path.read_text()), "node-000", width=64)
        assert rows[ref]["metrics"]["cpu"]["percent"] == expected
        assert rows[ref]["metrics"]["accelerator"]["mean_percent"] == expected
        source = state.extra["context"]["utilization"]
        events = [event for event in metrics_source.events
                  if event["authorization"] == "Bearer " + source["access"]["token"]]
        assert len(events) == 1 and events[0]["nodes"] == {source["configuration"]["nodeMapping"]["node-000"]}
    assert len(rows) == 2 and result["metrics_read_complete"]



def test_budget_incomplete_cohort_returns_locator_without_reading_metrics(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    original_key, second_key = state.key, "lbr_" + "z" * 32
    state.key = second_key
    second = aggregate.record(state, "same-scope-second-key")
    state.key, state.accepted_keys = original_key, [second_key]
    config = json.loads(second.read_text())
    config["approved_metrics_origin"] = metrics_source.origin
    second.write_text(json.dumps(config))
    query = helper.resource_list_query(fields=["node", "states", "metrics"], with_metrics=True)
    result = json.loads(helper.resource_list(
        aggregate.catalog, query=query, root=aggregate.script.parents[1], max_calls=2))["result"]
    assert result["http_requests"] == len(state.events) == 2
    assert metrics_source.events == [] and not result["scope_complete"]
    assert result["rows"][0]["observation_incomplete"] is True
    assert "states" not in result["rows"][0]
    assert not result["metrics_read_complete"]
