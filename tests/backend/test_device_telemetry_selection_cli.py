"""Installed CLI against the actual gateway; only upstream samples are synthetic."""

# ruff: noqa: F811 -- imported pytest fixture
import hashlib
import importlib.util
import json
import re
import threading
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from lockbot.metrics import gateway as gateway_module

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_list_metrics import execute, listing, metric_rows, prepare


@pytest.fixture
def source(tmp_path, monkeypatch):
    state = SimpleNamespace(tokens=set(), rows=[], events=[], upstream=[], mode=None)
    path = tmp_path / "gateway.json"
    path.write_text("{}")
    gateway = gateway_module.Gateway({})

    def upstream(request, **_kwargs):
        query = parse_qs(request.data.decode())["query"][0]
        state.upstream.append(query)
        match = re.search(r"device=~(" + gateway_module.JSON_STRING + ")", query)
        selected = re.compile(json.loads(match[1])) if match else None
        rows = [
            row
            for row in state.rows
            if selected is None
            or (
                row["metric"].get("collector") == "accelerator"
                and "device" not in row["metric"]
                or row["metric"].get("device") is not None
                and selected.fullmatch(row["metric"]["device"])
            )
        ]
        return BytesIO(json.dumps({"status": "success", "data": {"resultType": "vector", "result": rows}}).encode())

    gateway.opener.open = upstream
    original = gateway.query

    def query(authorization, body):
        fields = parse_qs(body.decode())
        state.events.append({"fields": fields, "authorization": authorization, "body_bytes": len(body)})
        if state.mode == "401_once" and len(state.events) == 1:
            raise gateway_module.Rejected(401, "synthetic_expired")
        if state.mode in (401, 403, 429):
            raise gateway_module.Rejected(state.mode, "synthetic_refusal", 40 if state.mode == 429 else 0)
        raw = original(authorization, body)
        result = json.loads(raw)
        if state.mode == "missing_echo":
            result.pop("lockbot_device_selection", None)
        elif state.mode == "wrong_echo":
            result["lockbot_device_selection"]["devices"] = ["other"]
        elif state.mode == "wrong_row":
            result["data"]["result"].append(
                {
                    "metric": {"__name__": helper.METRICS[0], "node": "source-1-0", "device": "other"},
                    "value": [helper.time.time(), "1"],
                }
            )
        elif state.mode == "bool_echo":
            result["lockbot_device_selection"]["version"] = True
        raw = json.dumps(result).encode()
        state.events[-1]["response_bytes"] = len(raw)
        return raw

    gateway.query = query
    monkeypatch.setattr(gateway_module, "Gateway", lambda _config: gateway)
    server = gateway_module.make_server(path, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state.origin = "http://127.0.0.1:" + str(server.server_port)

    def authorize():
        path.write_text(
            json.dumps(
                {
                    "readers": [
                        {
                            "sha256": hashlib.sha256(token.removeprefix("Bearer ").encode()).hexdigest(),
                            "nodes": ["source-1-0"],
                        }
                        for token in state.tokens
                    ]
                }
            )
        )

    state.authorize = authorize
    yield state
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def configured(aggregate, source, count=20):
    state, path, _mapping = prepare(aggregate, source, bot_type="DEVICE", cards=count)
    state.connection_path = path
    config = state.extra["context"]["utilization"]["configuration"]
    # Existing mapping schema supports 512 entries; inventory can be larger.
    config["deviceMapping"] = {f"node-000:dev{i}": f"collector-{i}" for i in range(min(count, 512))}
    source.rows = metric_rows("source-1-0", [(f"collector-{i}", i % 101) for i in range(count)])
    source.authorize()
    return state, config, helper.resource_reference(json.loads(path.read_text()), "node-000", width=64)


def page(aggregate, reference, *, limit=20, cursor=None, fields="id,metrics", mine=False):
    args = ["resource", "devices", reference, "--with-metrics", "--json", fields, "--limit", str(limit)]
    if cursor:
        args += ["--cursor", cursor]
    if mine:
        args += ["--mine"]
    return execute(aggregate, *args)["result"]


def test_actual_gateway_large_node_keeps_page_ids_values_and_one_source_request(aggregate, source, record_property):
    state, config, reference = configured(aggregate, source, 1000)
    old = page(aggregate, reference)
    old_bytes = source.events[-1]["response_bytes"]
    assert len(source.events) == 1 and old["metrics_scope"] == "selected_node"
    config["deviceQuery"] = "exact-v1"
    new = page(aggregate, reference)
    assert len(source.events) == 2 and new["metrics_scope"] == "selected_devices"
    assert [row["id"] for row in old["rows"]] == [row["id"] for row in new["rows"]] == list(range(20))
    assert [row["metrics"]["percent"] for row in old["rows"]] == [row["metrics"]["percent"] for row in new["rows"]]
    assert all(row["metrics"]["status"] == "ok" for row in new["rows"])
    assert new["metrics_read_complete"] and new["metrics_queried_devices"] == 20
    assert source.events[-1]["response_bytes"] < old_bytes / 10
    record_property("old_source_bytes", old_bytes)
    record_property("selected_source_bytes", source.events[-1]["response_bytes"])
    record_property("same_page_ids", 20)
    record_property("inventory_devices", 1000)
    assert len(source.upstream) == 2 and " or " in source.upstream[-1]
    assert new["http_requests"] == old["http_requests"] == 3
    assert all(method == "GET" for _path, method, _auth in state.events)


@pytest.mark.parametrize("mode", ["missing_echo", "wrong_echo", "bool_echo", "wrong_row", 403, 429])
def test_announced_selection_failure_never_retries_broadly(aggregate, source, mode):
    _state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = "exact-v1"
    source.mode = mode
    result = page(aggregate, reference)
    assert len(source.events) == 1 and "device_selection" in source.events[0]["fields"]
    assert not result["metrics_read_complete"] and len(result["rows"]) == 20
    assert result["metrics_unread_reasons"]
    if mode == 429:
        assert result["metrics_retry_after_seconds"] == 40


def test_collision_outside_page_and_unsupported_mapping_never_leak(aggregate, source):
    _state, config, reference = configured(aggregate, source, 100)
    config["deviceQuery"] = "exact-v1"
    config["deviceMapping"]["node-000:dev99"] = "collector-0"
    result = page(aggregate, reference)
    selection = json.loads(source.events[0]["fields"]["device_selection"][0])
    assert "collector-0" not in selection["devices"]
    assert result["rows"][0]["metrics"]["mapping_status"] == "ambiguous"
    assert not result["metrics_read_complete"]


def test_byte_trim_resume_preserves_every_id_without_extra_fetches(aggregate, source):
    _state, config, reference = configured(aggregate, source, 100)
    config["deviceQuery"] = "exact-v1"
    ids, cursor = [], None
    for _ in range(10):
        before = len(source.events)
        result = page(aggregate, reference, limit=256, cursor=cursor)
        assert len(source.events) == before + 1
        ids.extend(row["id"] for row in result["rows"])
        assert result["metrics_queried_devices"] <= 64
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert ids == list(range(100))


@pytest.mark.parametrize("bad", [None, False, "future"])
def test_bad_capability_is_not_absence(aggregate, source, bad):
    _state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = bad
    result = page(aggregate, reference)
    assert source.events == [] and "invalid_metrics_device_query" in result["metrics_unread_reasons"]


def installed_client(aggregate, state, *, clock=helper.time.monotonic):
    spec = importlib.util.spec_from_file_location("device_selection_package", aggregate.script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.Client(state.connection_path, root=aggregate.script.parent.parent, clock=clock)
    return module, client


@pytest.mark.parametrize("change", [False, True])
def test_401_refresh_stays_in_selected_mode_and_binds_configuration(aggregate, source, change):
    state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = "exact-v1"
    source.mode = "401_once"
    module, client = installed_client(aggregate, state)
    original = client._request

    def request(url, **kwargs):
        result = original(url, **kwargs)
        if url.endswith("/api/agent/context"):
            result["utilization"] = deepcopy(state.extra["context"]["utilization"])
            if change:
                result["utilization"]["configuration"].pop("deviceQuery")
        return result

    client._request = request
    result = json.loads(
        module.resource_devices(
            client, reference, query=module.resource_devices_query(with_metrics=True, fields=["id", "metrics"])
        )
    )["result"]
    assert len(source.events) == (1 if change else 2)
    assert all("device_selection" in event["fields"] for event in source.events)
    assert result["metrics_read_complete"] is (not change)
    if change:
        assert "metrics_mapping_changed" in result["metrics_unread_reasons"]


@pytest.mark.parametrize("credential", ["lba1.synthetic.private", "lbk_" + "K" * 43])
def test_selective_metrics_keeps_wrong_credential_refusal(aggregate, source, credential):
    state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = "exact-v1"
    state.extra["context"]["utilization"]["access"]["token"] = credential
    source.tokens.add("Bearer " + credential)
    source.authorize()
    result = page(aggregate, reference)
    assert source.events == []
    assert "resource_key_for_metrics_refused" in result["metrics_unread_reasons"]


def test_aging_mine_rebuild_keeps_earlier_unqueried_id_and_no_second_fetch(aggregate, source, monkeypatch):
    state, config, reference = configured(aggregate, source, 3)
    config["deviceQuery"] = "exact-v1"
    state.extra["resources"][0]["devices"][2].update(
        lock_status="exclusive", holders=[{"external_user_id": state.actor["external_user_id"]}]
    )
    clock, projections = [0.0], [0]
    module, client = installed_client(aggregate, state, clock=lambda: clock[0])
    original = module.observation_at_completion

    def age(client, snapshot):
        projections[0] += 1
        if projections[0] == 3:
            clock[0] = 5.1
        return original(client, snapshot)

    monkeypatch.setattr(module, "observation_at_completion", age)
    result = json.loads(
        module.resource_devices(
            client,
            reference,
            query=module.resource_devices_query(
                with_metrics=True, mine=True, fields=["id", "held_by_you", "metrics"], limit=1
            ),
        )
    )["result"]
    assert len(source.events) == 1
    assert result["rows"][0]["id"] == 0 and result["rows"][0]["held_by_you"] is None
    assert result["rows"][0]["metrics"]["read_status"] == "not_queried"
    assert not result["metrics_read_complete"] and not result["snapshot_fresh"]
    assert result["has_more"] and result["metrics_queried_devices"] == 1


def test_large_id_only_page_reports_unqueried_readings_without_losing_inventory(aggregate, source):
    _state, config, reference = configured(aggregate, source, 100)
    config["deviceQuery"] = "exact-v1"
    result = page(aggregate, reference, limit=256, fields="id")
    assert [row["id"] for row in result["rows"]] == list(range(100))
    assert result["scope_complete"] and not result["metrics_read_complete"]
    assert result["metrics_queried_devices"] == 64 and len(source.events) == 1


def test_encoded_selection_bounds_keep_resource_rows(aggregate, source):
    _state, config, reference = configured(aggregate, source, 100)
    config["deviceQuery"] = "exact-v1"
    config["deviceMapping"] = {f"node-000:dev{i}": str(i) + '"' * 230 for i in range(100)}
    source.rows = metric_rows("source-1-0", [(label, 0) for label in config["deviceMapping"].values()])
    result = page(aggregate, reference, limit=256, fields="id")
    assert len(result["rows"]) == 100 and not result["metrics_read_complete"]
    assert source.events, result["metrics_unread_reasons"]
    raw = source.events[0]["fields"]["device_selection"][0]
    assert len(raw.encode()) <= 16384
    assert source.events[0]["body_bytes"] <= 16384
    assert 0 < result["metrics_queried_devices"] < 64


def test_selected_read_keeps_remaining_http_budget(aggregate, source):
    state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = "exact-v1"
    module, client = installed_client(aggregate, state)
    client.calls = 10
    result = json.loads(
        module.resource_devices(
            client, reference, query=module.resource_devices_query(with_metrics=True, fields=["id", "metrics"])
        )
    )["result"]
    assert result["http_requests"] == 12 and source.events == []
    assert "request_budget_exhausted" in result["metrics_unread_reasons"] and len(result["rows"]) == 20


def test_selected_samples_age_during_final_packing_without_refetch(aggregate, source, monkeypatch):
    state, config, reference = configured(aggregate, source)
    config["deviceQuery"] = "exact-v1"
    current, projections = [0.0], [0]
    module, client = installed_client(aggregate, state, clock=lambda: current[0])
    original_metrics, original_observation = client.metrics, module.observation_at_completion

    def reading(*args, **kwargs):
        result = original_metrics(*args, **kwargs)
        for node in result["utilization"]:
            for card in node["cards"]:
                card["sample_age_seconds"] = 89.9
        return result

    def observation(client, snapshot):
        projections[0] += 1
        if projections[0] == 3:
            current[0] = 0.2
        return original_observation(client, snapshot)

    monkeypatch.setattr(client, "metrics", reading)
    monkeypatch.setattr(module, "observation_at_completion", observation)
    result = json.loads(
        module.resource_devices(
            client, reference, query=module.resource_devices_query(with_metrics=True, fields=["id", "metrics"])
        )
    )["result"]
    assert len(source.events) == 1 and result["metrics_read_complete"]
    assert all(row["metrics"]["status"] == "stale" and row["metrics"]["percent"] is None for row in result["rows"])


def test_capability_does_not_narrow_list_node_aggregates(aggregate, source):
    _state, config, _reference = configured(aggregate, source, 100)
    old = listing(aggregate)
    config["deviceQuery"] = "exact-v1"
    new = listing(aggregate)
    assert all("query" in event["fields"] and "device_selection" not in event["fields"] for event in source.events)
    assert old["rows"][0]["metrics"]["accelerator"] == new["rows"][0]["metrics"]["accelerator"]
