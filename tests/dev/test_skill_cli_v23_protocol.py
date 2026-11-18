"""Versioned host routing reaches the complete packaged auto-scope CLI."""

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned


def test_unscoped_v23_resolves_hint_with_the_packaged_cli_and_one_post(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-lock-tail", 23)
    result = run(fixture, "command", operation="lock", nodes=["19"])["result"]
    assert result["verified"] is True
    assert result["resolution"]["scope"] == "saved_connections_only"
    assert result["resolution"]["complete"] is True
    (call,) = events(fixture)
    assert "--connection" not in call["argv"]
    assert "--whole-node" not in call["argv"]  # The real SDK infers the observed bot mode.
    assert call["argv"][call["argv"].index("--node") + 1] == "19"
    wire = events(fixture, "cli-http.jsonl")
    posts = [row for row in wire if row["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["command"] == "lock node-19"
    assert posts[0]["status"] == 200
    assert {7, 8} <= {row.get("resource_bot") for row in wire[: wire.index(posts[0])]}
    assert len(wire) <= 12


@pytest.mark.parametrize("version", [18, 19, 20, 21, 22])
def test_legacy_host_routing_does_not_gain_unscoped_commands(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-lock-tail", version)
    assert run(fixture, "command", operation="lock", nodes=["19"]) == {"error": "group_required"}
    (call,) = events(fixture)
    assert call["http_request_span"] == [0, 0]
    assert "argv" not in call
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_v23_explicit_scope_stays_explicit(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 23)
    result = run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation="slock",
        nodes=["node-19"],
        devices=["0"],
        device_count=1,
        duration="4h",
    )["result"]
    assert result["verified"] is True
    assert "resolution" not in result
    (call,) = events(fixture)
    assert "--connection" in call["argv"]
    assert call["argv"][call["argv"].index("--device-count") + 1] == "1"
    posts = [row for row in events(fixture, "cli-http.jsonl") if row["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["command"] == "slock node-19 dev0 4h"


@pytest.mark.parametrize(
    "scope", [{"group": "unregistered-group"}, {"connection": "private/not-a-saved-connection.json"}]
)
def test_v23_does_not_replace_a_bad_explicit_scope_with_automatic_discovery(tmp_path, monkeypatch, scope):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-lock-tail", 23)
    assert run(fixture, "command", operation="lock", nodes=["19"], **scope) == {"error": "group_required"}
    (call,) = events(fixture)
    assert call["http_request_span"] == [0, 0] and "argv" not in call


def test_v23_ambiguous_hint_reaches_sdk_and_never_dispatches(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous", 23)
    result = run(fixture, "command", operation="lock", nodes=["训练节点"])
    assert result["command_sent"] is False and result["retry_mutation"] is False
    assert result["clarification"]["candidate_count"] == 2
    (call,) = events(fixture)
    assert "argv" in call and "--connection" not in call["argv"]
    wire = events(fixture, "cli-http.jsonl")
    assert {7, 8} <= {row.get("resource_bot") for row in wire}
    assert not any(row["method"] == "POST" for row in wire)
