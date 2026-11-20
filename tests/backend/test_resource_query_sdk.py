"""The shipped standalone SDK negotiates bounded reads against the real ASGI API."""

# ruff: noqa: F811 -- imported fixtures are injected by pytest.

import io
import json
import shutil
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from backend.test_packaged_cli_auto_scope import sdk  # noqa: F401
from backend.test_resource_api_resources import ACTOR, _hold, api  # noqa: F401


def listing(sdk, *args, fields="node,unit,states", limit=1):
    return sdk.run("resource", "list", "--json", fields, "--limit", str(limit), *args)


def test_packaged_list_stops_before_next_bot_and_exact_view_reuses_scope(sdk):
    output, events = listing(sdk)
    result = output["result"]
    assert result["rows"][0]["node"] == "n1" and result["rows"][0]["snapshot_fresh"]
    assert [row["path"] for row in events] == ["/api/agent-skill", "/api/agent/resources/query"]
    assert len(json.dumps(output).encode()) <= 8192
    assert "row_cursors" not in json.dumps(output) and "rq1." not in json.dumps(result["rows"])
    viewed, events = sdk.run("resource", "view", result["rows"][0]["ref"], "--json", "node,unit")
    assert viewed["result"]["rows"][0]["node"] == "n1"
    assert "resources" not in viewed["result"]
    assert [row["path"] for row in events] == ["/api/agent-skill", "/api/agent/resources/query"]


def test_source_cursor_pages_nodes_without_losing_trimmed_tail(sdk):
    # New names in the same bot generation are a configuration change, not a grant.
    sdk.register({f"node-{i:03}": "P800" for i in range(70)})
    output, _events = listing(sdk, fields="node", limit=2)
    found = []
    for _ in range(45):
        result = output["result"]
        found.extend(row["ref"] for row in result["rows"])
        if not result["has_more"]:
            break
        output, _events = sdk.run(*result["next_command"])
    else:
        pytest.fail("source traversal did not advance")
    assert len(found) == len(set(found)) == 73
    assert not result["scope_complete"]


@pytest.mark.parametrize(
    "api", [{"bot_type": "QUEUE_DEVICE", "cluster_configs": {"n1": ["P800"] * 200}}], indirect=True
)
def test_source_device_pages_keep_exact_ids_and_bounded_client_output(sdk):
    first, _ = listing(sdk, fields="node")
    ref = first["result"]["rows"][0]["ref"]
    output, events = sdk.run("resource", "devices", ref, "--json", "id,model", "--limit", "200")
    found = []
    for _ in range(10):
        result = output["result"]
        found.extend(row["id"] for row in result["rows"])
        assert len(json.dumps(output, separators=(",", ":")).encode()) + 1 <= 8192
        assert all(row["path"] in {"/api/agent-skill", "/api/agent/resources/query"} for row in events)
        if not result["has_more"]:
            break
        arguments = result.get("next_command") or [
            "resource",
            "devices",
            ref,
            "--json",
            "id,model",
            "--limit",
            "200",
            "--cursor",
            result["next_cursor"],
        ]
        output, events = sdk.run(*arguments)
    else:
        pytest.fail("source device cursor did not advance")
    assert found == list(range(200))


@pytest.mark.parametrize("change", ["fields", "catalog"])
def test_local_query_cursor_rejects_changes_before_any_http(sdk, change):
    output, _ = listing(sdk)
    command = output["result"]["next_command"]
    if change == "fields":
        command[command.index("--json") + 1] = "node"
    else:
        record = json.loads(sdk.connections[0].read_text())
        record["key_file"] = "missing-key"
        sdk.connections[0].write_text(json.dumps(record))
    output, events = sdk.run(*command, code=2)
    assert output["error"] == f"resource_list_cursor_{'query' if change == 'fields' else 'catalog'}_changed"
    assert not events


@pytest.mark.parametrize("arguments", [("resource", "list", "--json"), ("resource", "devices", "--json")])
def test_bare_fields_still_perform_zero_http(sdk, arguments):
    output, events = sdk.run(*arguments)
    assert output["json_fields"] and not events


def test_known_capability_absence_keeps_full_read_without_query_probe(sdk):
    def transform(path, body):
        if path == "/api/agent-skill":
            body["capabilities"].remove("resource_query_v1")
            body.pop("resource_query")

    sdk.transport.transform = transform
    output, events = listing(sdk)
    assert output["result"]["rows"]
    assert [row["path"] for row in events] == ["/api/agent-skill", "/api/agent/resources"]


def test_broken_announced_descriptor_never_probes_or_downgrades(sdk):
    def transform(path, body):
        if path == "/api/agent-skill":
            body["resource_query"].pop("cursor_format")

    sdk.transport.transform = transform
    output, events = listing(sdk)
    assert not output["result"]["rows"] and not output["result"]["scope_complete"]
    assert "resource_query_capability_invalid" in output["result"]["unread_reasons"]
    assert all(event["path"] == "/api/agent-skill" for event in events)


@pytest.mark.parametrize("flags", [("--search", "n1"), ("--node", "n1"), ("--with-metrics",)])
def test_advertised_unsupported_filters_choose_old_read_before_query(sdk, flags):
    output, events = listing(sdk, *flags)
    assert output["result"]["rows"]
    assert any(row["path"] == "/api/agent/resources" for row in events)
    assert not any(row["path"] == "/api/agent/resources/query" for row in events)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_announced_query_failure_never_downgrades_to_full_inventory(sdk, status):
    original = sdk.transport.open

    def opened(request, timeout=None):
        if urlsplit(request.full_url).path == "/api/agent/resources/query":
            sdk.transport.events.append({"method": "GET", "path": "/api/agent/resources/query", "status": status})
            headers = Message()
            headers["Retry-After"] = "3600"
            raise HTTPError(request.full_url, status, "synthetic failure", headers, io.BytesIO(b"{}"))
        return original(request, timeout)

    sdk.transport.open = opened
    output, events = listing(sdk)
    assert not output["result"]["rows"] and not output["result"]["scope_complete"]
    assert f"http_{status}" in output["result"]["unread_reasons"]
    assert not any(row["path"] == "/api/agent/resources" for row in events)


@pytest.mark.parametrize("fault", ["actor", "query", "query-bool", "row", "tokens", "shape", "frame"])
def test_announced_response_identity_and_schema_errors_never_downgrade(sdk, fault):
    original = sdk.transport.open

    def opened(request, timeout=None):
        response = original(request, timeout)
        if urlsplit(request.full_url).path != "/api/agent/resources/query":
            return response
        body = json.loads(response.getvalue())
        if fault == "actor":
            body["context"]["actor"]["external_user_id"] = "another-user"
        elif fault == "query":
            body["query"]["mine"] = True
        elif fault == "query-bool":
            body["query"]["mine"] = 0
        elif fault == "row":
            body["rows"][0]["holders"] = [{"external_user_id": "private-owner"}]
        elif fault == "tokens":
            body["row_cursors"] = []
        elif fault == "frame":
            body["cursor_frame"] = "rqf1.cHJpdmF0ZS1hcmJpdHJhcnktZGF0YQ"
        else:
            body["resources"] = []
        changed = io.BytesIO(json.dumps(body).encode())
        changed.headers = response.headers
        return changed

    sdk.transport.open = opened
    output, events = listing(sdk)
    assert not output["result"]["rows"] and not output["result"]["scope_complete"]
    assert not any(row["path"] == "/api/agent/resources" for row in events)


def test_query_rows_never_prepare_a_coherent_command_read(sdk):
    client = sdk.module.Client(sdk.connections[0], root=sdk.installation, opener=sdk.transport)
    page = client.resource_query(sdk.module.resource_query_request(fields=["node"]))
    assert page["rows"] and client._prepared_resources is None and not client._verified_contexts
    assert "resources" not in page and "snapshot_fresh_for_write" not in page
    output, events = sdk.run("command", "--operation", "lock", "--node", "n1", "--duration", "1h")
    assert output["result"]["verified"]
    assert any(row["path"] == "/api/agent/resources" for row in events)
    assert not any(row["path"] == "/api/agent/resources/query" for row in events)


def test_source_configuration_change_reports_explicit_restart_without_full_fallback(sdk):
    client = sdk.module.Client(sdk.connections[0], root=sdk.installation, opener=sdk.transport)
    query = sdk.module.resource_query_request(fields=["node"], limit=1)
    first = client.resource_query(query)
    assert first["has_more"]
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "changed"})
    start = len(sdk.transport.events)
    with pytest.raises(sdk.module.ClientError, match="resource_query_restart_required"):
        client.resource_query(query, cursor=first["next_cursor"])
    assert [row["path"] for row in sdk.transport.events[start:]] == ["/api/agent/resources/query"]


def test_final_cli_packing_ages_selective_states_past_freshness(sdk, monkeypatch):
    module = sdk.module
    current = [0.0]
    client = module.Client(sdk.connections[0], root=sdk.installation, opener=sdk.transport, clock=lambda: current[0])
    page = client.resource_query(module.resource_query_request(node_id="n1", fields=["node", "unit", "states"]))
    page["age_seconds"], page["_received_at"] = 4.9, 0
    original = module.resource_query_rows_at_completion
    count = 0

    def age_after_preview(*args):
        nonlocal count
        count += 1
        if count >= 2:
            current[0] = 0.2
        return original(*args)

    monkeypatch.setattr(module, "resource_query_rows_at_completion", age_after_preview)
    result = json.loads(module.resource_query_cli_page(client, page, query={"fields": ["node", "states"]}))["result"]
    assert result["age_seconds"] == pytest.approx(5.1)
    assert not result["snapshot_fresh"] and not result["scope_complete"]
    assert result["rows"][0]["snapshot_fresh"] is False
    assert result["rows"][0]["observation_incomplete"]
    assert all(value is None for value in result["rows"][0]["states"].values())


@pytest.mark.parametrize("elapsed", [0.2, 1.1])
def test_own_observation_horizon_retires_claims_without_waiting_five_seconds(sdk, monkeypatch, elapsed):
    _hold(sdk.api, "n1", ACTOR)
    module, current = sdk.module, [0.0]
    client = module.Client(sdk.connections[0], root=sdk.installation, opener=sdk.transport, clock=lambda: current[0])
    page = client.resource_query(module.resource_query_request(node_id="n1", mine=True, fields=["node", "holdings"]))
    # A source may declare an earlier lease/event horizon than the snapshot TTL.
    page["valid_for_seconds"], page["age_seconds"], page["_received_at"] = 1.0, 0.0, 0.0
    original, count = module.resource_query_rows_at_completion, 0

    def after_preview(*args):
        nonlocal count
        count += 1
        if count >= 2:
            current[0] = elapsed
        return original(*args)

    monkeypatch.setattr(module, "resource_query_rows_at_completion", after_preview)
    result = json.loads(module.resource_query_cli_page(client, page, query={"fields": ["node", "holdings"]}))["result"]
    row = result["rows"][0]
    assert result["age_seconds"] == elapsed < 5
    if elapsed < 1:
        assert result["snapshot_fresh"] and row["holdings"] and row["holding_units"] == 1
    else:
        assert not result["snapshot_fresh"] and not result["scope_complete"]
        assert "holdings" not in row and row["holding_units"] is None and row["observation_incomplete"]


@pytest.mark.parametrize(
    "api", [{"bot_type": "QUEUE_DEVICE", "cluster_configs": {"n1": ["P800"] * 1000}}], indirect=True
)
def test_selected_card_prefixes_retain_mandatory_companions_in_list_and_view(sdk):
    fields = "node,matched_device_ids,idle_device_ids"
    output, _ = listing(sdk, fields=fields)
    row = output["result"]["rows"][0]
    assert row["matched_device_ids"] == row["idle_device_ids"] == list(range(20))
    assert row["devices_has_more"] and row["idle_devices_has_more"]
    assert row["device_inventory_complete"] and row["unidentified_devices"] == 0
    assert not output["result"]["has_more"] or output["result"]["next_cursor"]
    viewed, _ = sdk.run("resource", "view", row["ref"], "--json", fields)
    assert viewed["result"]["rows"][0] == row


@pytest.mark.parametrize(
    "api", [{"bot_type": "QUEUE_DEVICE", "cluster_configs": {"n1": ["P800"] * 1000}}], indirect=True
)
@pytest.mark.parametrize("action", ["list", "resources", "resources-all"])
def test_known_absent_source_keeps_card_prefix_markers_in_every_projection(sdk, action):
    def transform(path, body):
        if path == "/api/agent-skill":
            body["capabilities"].remove("resource_query_v1")
            body.pop("resource_query")

    sdk.transport.transform = transform
    fields = "node,matched_device_ids,idle_device_ids"
    arguments = ["resource", "list"] if action == "list" else [action]
    if action == "resources":
        arguments = ["--connection", str(sdk.connections[0]), *arguments]
    output, events = sdk.run(*arguments, "--json", fields)
    row = next(row for row in output["result"]["rows"] if row["node"] == "n1")
    assert row["matched_device_ids"] == row["idle_device_ids"] == list(range(20))
    assert row["devices_has_more"] and row["idle_devices_has_more"]
    assert row["device_inventory_complete"] and row["unidentified_devices"] == 0
    assert all(event["method"] == "GET" and not event["path"].endswith("/query") for event in events)


@pytest.mark.parametrize("bad", ["missing-companion", "nested-device", "nested-duration"])
def test_strict_selected_values_and_companions_cannot_carry_hidden_data(sdk, bad):
    original = sdk.transport.open

    def opened(request, timeout=None):
        response = original(request, timeout)
        if urlsplit(request.full_url).path != "/api/agent/resources/query":
            return response
        body = json.loads(response.getvalue())
        row = body["rows"][0]
        if bad == "missing-companion":
            row.update(hardware=[], hardware_incomplete=False)
            row.pop("hardware_has_more", None)
        else:
            row.update(
                holdings=[
                    {
                        "device": {"hidden": "object"} if bad == "nested-device" else None,
                        "model": None,
                        "remaining_seconds": "invalid" if bad == "nested-duration" else 2,
                    }
                ],
                holdings_has_more=False,
                holding_units=1,
            )
        changed = io.BytesIO(json.dumps(body).encode())
        changed.headers = response.headers
        return changed

    sdk.transport.open = opened
    output, events = listing(sdk, fields="node,hardware" if bad == "missing-companion" else "node,holdings")
    assert not output["result"]["rows"] and not output["result"]["scope_complete"]
    assert "resource_query_response_invalid" in output["result"]["unread_reasons"]
    assert not any(event["path"] == "/api/agent/resources" for event in events)


@pytest.mark.parametrize("fault", ["alias", "own-membership"])
def test_duplicate_credentials_keep_full_peer_conflict_comparison(sdk, fault):
    shutil.copytree(sdk.connections[0].parent, sdk.catalog / "duplicate-peer")
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "19"})
    if fault == "own-membership":
        _hold(sdk.api, "n1", ACTOR)
    seen = 0

    def transform(path, body):
        nonlocal seen
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        seen += 1
        if seen != 2:
            return
        node = next(row for row in body["resources"] if row["id"] == "n1")
        if fault == "alias":
            node["alias"] = "other-alias"
        else:
            node.update(holders=[], lock_status="idle")

    sdk.transport.transform = transform
    output, events = listing(sdk, *(("--mine",) if fault == "own-membership" else ()), fields="node")
    assert output["result"]["rows"][0]["observation_conflict"]
    assert not output["result"]["scope_complete"]
    assert [row["path"] for row in events] == [
        "/api/agent-skill",
        "/api/agent/resources",
        "/api/agent/resources",
    ]
