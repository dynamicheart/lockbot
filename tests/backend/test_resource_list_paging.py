"""Result-driven live pages through the shipped SDK, with localhost fixtures only."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import os
import subprocess
import sys

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def execute(aggregate, *args, code=0, json_output=True):
    process = subprocess.run(
        [sys.executable, "-I", "-S", str(aggregate.script), *map(str, args)],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == code, process.stderr
    assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    assert all(state.key not in process.stdout + process.stderr for state in aggregate.states)
    return (json.loads(process.stdout if code == 0 else process.stderr) if json_output else process.stdout), process


def nodes(count, *, prefix="node"):
    return [
        {"id": f"{prefix}{number:04}", "lock_status": "idle", "holders": [], "booking_list": []}
        for number in range(count)
    ]


def listing(aggregate, *args, fields="node,unit", limit=20):
    return execute(aggregate, "resource", "list", "--json", fields, "--limit", limit, *args)[0]["result"]


def test_first_scope_fills_page_without_contacting_later_scopes(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"] = nodes(100)
    first_page = listing(aggregate)
    assert len(first_page["rows"]) == 20 and first_page["http_requests"] == 2
    assert first_page["next_cursor"] and first_page["has_more"] and not first_page["scope_complete"]
    assert len(first.events) == 2 and second.events == []
    assert first_page["next_command"][:2] == ["resource", "list"]


def test_large_scope_pages_without_losing_tail_or_rereading_completed_scope(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"], second.extra["resources"] = nodes(45), nodes(3, prefix="other")
    before = {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}
    page, found = listing(aggregate), []
    for _ in range(8):
        found.extend(row["ref"] for row in page["rows"])
        if page["traversal_exhausted"]:
            break
        page = execute(aggregate, *page["next_command"])[0]["result"]
    else:
        pytest.fail("cursor did not progress")
    assert len(found) == len(set(found)) == 48 and page["next_cursor"] is None
    assert len(first.events) == 6 and len(second.events) == 2
    assert not page["scope_complete"]  # a resumed tail is not a whole-inventory observation
    assert before == {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}


def test_exact_scope_boundary_does_not_reread_the_finished_scope(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"], second.extra["resources"] = nodes(20), nodes(2)
    page = listing(aggregate)
    tail = execute(aggregate, *page["next_command"])[0]["result"]
    assert tail["displayed_nodes"] == 2 and tail["traversal_exhausted"]
    assert len(first.events) == len(second.events) == 2


def test_budget_exhaustion_stops_at_unstarted_scope_and_resumes_there(aggregate):
    states = [aggregate.server(number + 1) for number in range(7)]
    for number, state in enumerate(states):
        aggregate.record(state, f"record-{number}")
    page = listing(aggregate)
    assert page["http_requests"] == 12 and page["displayed_nodes"] == 6
    assert states[-1].events == []
    tail = execute(aggregate, *page["next_command"])[0]["result"]
    assert tail["displayed_nodes"] == 1 and tail["traversal_exhausted"]
    assert all(len(state.events) == 2 for state in states)


@pytest.mark.parametrize("change", ["model", "node", "fields", "limit", "actor", "insert"])
def test_changed_query_or_catalog_fails_before_http(aggregate, change):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    state.extra["resources"] = nodes(30)
    page = listing(aggregate)
    state.events.clear()
    options = ["resource", "list", "--json", "node,unit", "--limit", "20", "--cursor", page["next_cursor"]]
    if change == "model":
        options += ["--model", "P800"]
    elif change == "node":
        options += ["--node", "node0000"]
    elif change == "fields":
        options[3] = "node"
    elif change == "limit":
        options[5] = "10"
    elif change == "actor":
        record = json.loads(path.read_text())
        record["actor"]["external_user_id"] = "new-subject"
        path.write_text(json.dumps(record))
    else:
        aggregate.record(state, "another")
    value, _ = execute(aggregate, *options, code=2)
    assert value["error"] == (
        "resource_list_cursor_catalog_changed"
        if change in {"actor", "insert"}
        else "resource_list_cursor_query_changed"
    )
    assert not state.events


def test_filter_skips_empty_scopes_but_does_not_claim_global_absence(aggregate):
    states = [aggregate.server(number + 1) for number in range(7)]
    for number, state in enumerate(states):
        aggregate.record(state, f"record-{number}")
    page = listing(aggregate, "--model", "P800")
    assert page["rows"] == [] and page["has_more"] and not page["scope_complete"]
    assert page["http_requests"] == 12 and states[-1].events == []


def test_alias_peers_are_checked_before_page_limit_and_field_projection(aggregate):
    first, unrelated = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "a-first")
    aggregate.record(unrelated, "m-unrelated")
    original = first.key
    first.key = "lbr_" + "second" * 8
    aggregate.record(first, "z-second")
    second_key = first.key
    first.accepted_keys, first.key = [second_key], original
    row = {"id": "node07", "alias": "19", "lock_status": "idle", "holders": [], "booking_list": []}
    first.extra["resources"] = [row]
    first.extra_per_key = {"Bearer " + second_key: {"resources": [{**row, "alias": "other"}]}}
    page = listing(aggregate, "--node", "19", fields="node", limit=1)
    assert page["rows"][0]["observation_conflict"]
    assert set(page["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert len(first.events) == 3 and unrelated.events == []


def test_failed_peer_returns_only_locator_and_incomplete_marker(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "unaccepted" * 8
    aggregate.record(state, "second")
    state.key = original
    page = listing(aggregate, fields="node,unit,hardware,states")
    assert set(page["rows"][0]) == {"ref", "node", "observation_incomplete"}
    assert not page["scope_complete"] and "http_401" in page["unread_reasons"]


def test_oversized_peer_cohort_is_bounded_and_can_advance(aggregate):
    state = aggregate.server(1)
    state.accepted_keys = []
    original = state.key
    for number in range(15):
        state.key = "lbr_" + f"synthetic-peer-{number}-" * 3
        aggregate.record(state, f"peer-{number:02}")
        state.accepted_keys.append(state.key)
    state.key = original
    page = listing(aggregate)
    assert page["http_requests"] == 12 and page["traversal_exhausted"]
    assert page["rows"][0]["observation_incomplete"] and not page["scope_complete"]
    assert "peer_observations_not_read" in page["unread_reasons"]


def test_identifier_changes_on_resume_keep_keyset_semantics(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = nodes(6)
    page = listing(aggregate, limit=2)
    state.extra["resources"] = [*nodes(1, prefix="aaa"), *nodes(6)[2:], *nodes(1, prefix="zzz")]
    tail = execute(aggregate, *page["next_command"])[0]["result"]
    assert [row["node"] for row in tail["rows"]] == ["node0002", "node0003"]
    assert not tail["scope_complete"]


def test_node_secret_is_not_encoded_into_a_cursor(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{**nodes(1)[0], "id": state.key}, *nodes(3)]
    page = listing(aggregate, limit=1)
    assert page["unidentified_nodes"] >= 1
    assert helper.parse_resource_reference(page["rows"][0]["ref"])[1] == "node0000"
    assert page["next_cursor"] and not page["scope_complete"]


def test_default_table_and_exact_reference_view(aggregate):
    state, other = aggregate.server(1), aggregate.server(2)
    path = aggregate.record(state, "first")
    aggregate.record(other, "second")
    text, _ = execute(aggregate, "resource", "list", "--limit", "1", json_output=False)
    assert "NODE  UNIT" in text and "Next: lockbot resource list" in text
    other.events.clear()
    reference = helper.resource_reference(json.loads(path.read_text()), "same-node", width=64)
    text, _ = execute(aggregate, "resource", "view", reference, json_output=False)
    assert "same-node" in text and "whole_node" in text and not other.events


@pytest.mark.parametrize(
    "arguments",
    [
        ["--model", "x" * 81],
        ["--limit", "0"],
        ["--node", "bad\nnode"],
        ["--json", "bad-field"],
        ["--cursor", "not-a-cursor"],
    ],
)
def test_invalid_selectors_never_open_a_key_or_contact_service(aggregate, arguments):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    (path.parent / "key").unlink()
    execute(aggregate, "resource", "list", *arguments, code=2)
    assert not state.events


def test_field_discovery_and_list_metrics_rejection_are_local(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    value, _ = execute(aggregate, "resource", "list", "--json")
    assert "ref" in value["json_fields"]
    execute(aggregate, "resource", "list", "--with-metrics", code=2)
    assert not state.events


def test_byte_bound_preserves_all_rows_with_long_unicode_and_two_hundred_cards(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [
        {
            "id": "机器" * 64 + str(number),
            "devices": [
                {"id": card, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
                for card in range(200)
            ],
        }
        for number in range(7)
    ]
    page = listing(
        aggregate,
        fields="node,hardware,matched_device_ids,idle_device_ids,total_cards,devices_has_more,idle_devices_has_more",
    )
    found = []
    for _ in range(16):
        for row in page["rows"]:
            assert row["total_cards"] == 200 and row["devices_has_more"] and row["idle_devices_has_more"]
            assert row["matched_device_ids"] == row["idle_device_ids"] == list(range(20))
            found.append(row["ref"])
        if page["traversal_exhausted"]:
            break
        if "next_command" in page:
            page = execute(aggregate, *page["next_command"])[0]["result"]
        else:
            page = listing(
                aggregate,
                "--cursor",
                page["next_cursor"],
                fields="node,hardware,matched_device_ids,idle_device_ids,total_cards,devices_has_more,idle_devices_has_more",
            )
    assert len(found) == len(set(found)) == 7


def test_earlier_scope_is_reaged_after_a_later_read(aggregate, monkeypatch):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"], second.extra["resources"] = nodes(1), nodes(1)
    original, now = helper._ResourceListReader.read, [0]

    def delayed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if len(self.clients) == 2:
            now[0] = 6
        return result

    monkeypatch.setattr(helper._ResourceListReader, "read", delayed)
    query = helper.resource_list_query(fields=["node", "states", "snapshot_fresh"])
    value = json.loads(
        helper.resource_list(aggregate.catalog, query=query, root=aggregate.script.parents[1], clock=lambda: now[0])
    )["result"]
    assert all(not row["snapshot_fresh"] and row["states"]["unknown"] == 1 for row in value["rows"])
    assert not value["scope_complete"]


def test_deadline_after_catalog_read_returns_a_zero_position_cursor(aggregate, monkeypatch):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    original, now = helper.listed_connections, [0]

    def expired(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 61
        return result

    monkeypatch.setattr(helper, "listed_connections", expired)
    query = helper.resource_list_query(fields=["node"])
    value = json.loads(
        helper.resource_list(aggregate.catalog, query=query, root=aggregate.script.parents[1], clock=lambda: now[0])
    )["result"]
    assert not state.events and value["http_requests"] == 0 and value["next_cursor"]
    assert not value["scope_complete"] and not value["traversal_exhausted"]


def test_429_keeps_retry_hint_and_does_not_starve_later_scopes(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.status, first.retry_after = 429, 35
    page = listing(aggregate)
    assert len(first.events) == len(second.events) == 2
    assert page["displayed_nodes"] == 1 and page["traversal_exhausted"]
    assert "http_429" in page["unread_reasons"] and page["retry_after_seconds"] == 35
    assert not page["scope_complete"]


def test_model_conflict_is_checked_before_filtering_a_peer(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "first")
    original = state.key
    state.key = "lbr_" + "other-material" * 5
    aggregate.record(state, "second")
    second = state.key
    state.accepted_keys, state.key = [second], original
    card = {"id": 0, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
    state.extra["resources"] = [{"id": "node07", "devices": [card]}]
    state.extra_per_key = {
        "Bearer " + second: {"resources": [{"id": "node07", "devices": [{**card, "dev_model": "H20"}]}]}
    }
    page = listing(aggregate, "--model", "P800", fields="node,matched_device_ids", limit=1)
    assert set(page["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert len(state.events) == 3 and not page["scope_complete"]


def test_generic_protected_parse_error_forces_new_discovery_for_next_key(aggregate, monkeypatch):
    state = aggregate.server(1)
    aggregate.record(state, "first")
    original_key = state.key
    state.key = "lbr_" + "other-material" * 5
    aggregate.record(state, "second")
    state.accepted_keys, state.key = [state.key], original_key
    original = helper.Client._read_context

    def malformed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.key == original_key:
            raise AttributeError("synthetic malformed protected context")
        return result

    monkeypatch.setattr(helper.Client, "_read_context", malformed)
    query = helper.resource_list_query(fields=["node"])
    page = json.loads(helper.resource_list(aggregate.catalog, query=query, root=aggregate.script.parents[1]))["result"]
    assert [path for path, _method, _auth in state.events] == ["/api/agent-skill", "/api/agent/resources"] * 2
    assert page["rows"][0]["observation_incomplete"] and not page["scope_complete"]


def test_malformed_filtered_source_is_a_gap_and_does_not_hide_later_scope(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"] = [None]
    page = listing(aggregate, "--node", "same-node")
    assert page["displayed_nodes"] == 1 and page["traversal_exhausted"]
    assert page["observed_scopes"] == 1 and page["attempted_scopes"] == 2
    assert "invalid_resource_projection" in page["unread_reasons"] and not page["scope_complete"]
