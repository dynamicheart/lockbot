"""Finite read traversal over the actual packaged CLI, with unchanged budgets."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import os
import subprocess
import sys

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def execute(aggregate, *args, code=0):
    process = subprocess.run(
        [sys.executable, "-S", str(aggregate.script), *map(str, args)],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == code, process.stderr
    assert all(state.key not in process.stdout + process.stderr for state in aggregate.states)
    return json.loads(process.stdout or process.stderr)


def catalog(aggregate, count):
    states = [aggregate.server(number + 1) for number in range(count)]
    paths = [aggregate.record(state, f"record-{number:02}") for number, state in enumerate(states)]
    return states, paths


def test_thirteen_connections_traverse_without_rereading_prefix_or_raising_budget(aggregate):
    states, _ = catalog(aggregate, 13)
    before = {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}
    cursor, refs, attempts = None, [], []
    for _ in range(3):
        options = ["--after-scope", cursor] if cursor else []
        result = execute(aggregate, "resources-all", "--json", "node", *options)["result"]
        page = result["scope_page"]
        assert result["scope_complete"] is False
        assert result["http_requests"] <= helper.MAX_CALLS == 12
        assert result["has_more"] is False  # output rows and unread scopes are independent
        refs.extend(row["ref"] for row in result["rows"])
        attempts.append(page["attempted_connections"])
        cursor = page["next_after_scope"]
    assert cursor is None and page["exhausted"] is True
    assert attempts == [6, 6, 1] and len(refs) == len(set(refs)) == 13
    assert all(len(state.events) == 2 for state in states)
    assert all(method == "GET" for state in states for _, method, _ in state.events)
    assert before == {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}


@pytest.mark.parametrize("change", ["actor", "bot", "key_path", "rename", "insert", "delete"])
def test_changed_catalog_rejects_resume_before_http(aggregate, change):
    states, paths = catalog(aggregate, 7)
    result = execute(aggregate, "resources-all", "--summary")["result"]
    cursor = result["scope_page"]["next_after_scope"]
    assert cursor
    if change in {"actor", "bot", "key_path"}:
        record = json.loads(paths[-1].read_text())
        if change == "actor":
            record["actor"]["external_user_id"] = "changed-actor"
        elif change == "bot":
            record["bot_uuid"] = "changed-bot"
        else:
            record["key_file"] = "another-key"
        paths[-1].write_text(json.dumps(record))
    elif change == "rename":
        paths[-1].parent.rename(paths[-1].parent.with_name("renamed"))
    elif change == "insert":
        aggregate.record(states[-1], "another-record")
    else:
        paths[-1].unlink()
    for state in states:
        state.events.clear()
    result = execute(aggregate, "resources-all", "--after-scope", cursor, code=2)
    assert result["error"] == "scope_cursor_catalog_changed"
    assert all(not state.events for state in states)


def test_429_is_reported_with_retry_hint_and_not_retried_by_continuation(aggregate):
    states, _ = catalog(aggregate, 8)
    states[0].status, states[0].retry_after = 429, 35
    first = execute(aggregate, "resources-all", "--summary")["result"]
    assert first["retry_after_seconds"] == 35 and "http_429" in first["unread_reasons"]
    cursor = first["scope_page"]["next_after_scope"]
    assert cursor
    second = execute(aggregate, "resources-all", "--after-scope", cursor)["result"]
    assert second["complete"] is False and second["scope_page"]["exhausted"] is True
    assert len(second["groups"]) == 2 and second["unread"] == []
    assert all(len(state.events) == 2 for state in states)


def test_discovery_without_remaining_protected_budget_defers_same_scope_on_resume(aggregate):
    states, _ = catalog(aggregate, 7)
    states[0].coherent = False  # three requests, then four two-request scopes leave one slot
    first = execute(aggregate, "resources-all")["result"]
    assert first["http_requests"] == 12 and len(first["groups"]) == 5
    assert any(item["error"] == "request_budget_exhausted" and item["count"] == 2 for item in first["unread"])
    assert len(states[5].events) == 1
    second = execute(aggregate, "resources-all", "--after-scope", first["scope_page"]["next_after_scope"])["result"]
    assert second["scope_page"]["prior_connections"] == 5 and second["scope_page"]["exhausted"]
    assert len(second["groups"]) == 2 and second["unread"] == [] and not second["complete"]
    assert len(states[5].events) == 3 and len(states[6].events) == 2


def test_equivalent_records_on_different_pages_remain_separate_verified_observations(aggregate):
    states, _ = catalog(aggregate, 7)
    aggregate.record(states[0], "record-99-duplicate")
    first = execute(aggregate, "resources-all", "--summary")["result"]
    second = execute(aggregate, "resources-all", "--summary", "--after-scope", first["scope_page"]["next_after_scope"])[
        "result"
    ]
    assert second["scope_page"]["attempted_connections"] == 2
    assert second["scope_page"]["exhausted"] is True and not second["scope_complete"]
    assert len(states[0].events) == 4
    assert {row["ref"] for row in second["rows"]} & {row["ref"] for row in first["rows"]}


@pytest.mark.parametrize("token", ["", "sc2.bad.1", "sc1." + "a" * 64 + ".-1", "sc1." + "a" * 64 + ".01", "x" * 4096])
def test_malformed_cursors_fail_locally_without_reading_a_key(aggregate, token):
    states, paths = catalog(aggregate, 1)
    (paths[0].parent / "key").unlink()
    result = execute(aggregate, "resources-all", "--after-scope", token, code=2)
    assert result["error"] == "invalid_scope_cursor" and not states[0].events


def test_incomplete_catalog_cannot_issue_or_accept_a_continuation(aggregate, monkeypatch):
    states, _ = catalog(aggregate, 3)
    original = helper.CatalogScanBudget

    class Limited(original):
        def __init__(self, deadline, clock):
            super().__init__(deadline, clock, records=2)

    monkeypatch.setattr(helper, "CatalogScanBudget", Limited)
    output = helper.resources_all(aggregate.catalog, root=aggregate.script.parents[1])["result"]
    assert not output["complete"] and not output["scope_page"]["resumable_catalog"]
    assert output["scope_page"]["next_after_scope"] is None
    for state in states:
        state.events.clear()
    with pytest.raises(helper.ClientError, match="scope_cursor_catalog_incomplete"):
        helper.resources_all(aggregate.catalog, root=aggregate.script.parents[1], after_scope="sc1." + "a" * 64 + ".1")
    assert all(not state.events for state in states)


def test_deadline_before_first_http_returns_a_valid_zero_position_cursor(aggregate, monkeypatch):
    states, _ = catalog(aggregate, 1)
    now = [0]
    original = helper.listed_connections

    def catalog_at_deadline(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 61
        return result

    with monkeypatch.context() as patch:
        patch.setattr(helper, "listed_connections", catalog_at_deadline)
        first = helper.resources_all(aggregate.catalog, root=aggregate.script.parents[1], clock=lambda: now[0])[
            "result"
        ]
    cursor = first["scope_page"]["next_after_scope"]
    assert cursor.endswith(".0") and first["http_requests"] == 0 and not states[0].events
    second = helper.resources_all(
        aggregate.catalog, root=aggregate.script.parents[1], clock=lambda: now[0], after_scope=cursor
    )["result"]
    assert second["scope_page"]["exhausted"] and second["complete"] is False
    assert len(second["groups"]) == 1 and len(states[0].events) == 2


@pytest.mark.parametrize("action", ["resources", "command"])
def test_continuation_is_not_an_option_for_single_scope_or_mutation_discovery(aggregate, action):
    states, _ = catalog(aggregate, 1)
    execute(aggregate, action, "--after-scope", "sc1." + "a" * 64 + ".1", code=2)
    assert not states[0].events
