"""v30 proves selection independently of an intentionally unknown mutation."""

import copy
import json

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned

FINAL = "node-10的2、3号卡锁定结果无法确认。只核对当前占用，未重新提交。"


def attempted(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-write-unknown", 30)
    result = run(fixture, "command", nodes=["node-10"], devices=["2", "3"], operation="lock", duration="2h")
    assert result["error"] == "mutation_outcome_unknown_read_resources_do_not_replay"
    assert result["command_outcome"] == "unknown"
    run(fixture, "resources-all", mine=True)
    return fixture


def test_unknown_attempt_has_verified_selection_but_never_verified_execution(tmp_path, monkeypatch):
    fixture = attempted(tmp_path, monkeypatch)
    original = (fixture.root / "trace.jsonl").read_bytes()
    grade = evaluation.score(fixture.root, FINAL)
    assert grade["observable_pass"], grade
    assert grade["checks"]["sdk_resolved_target_before_post"]
    assert grade["cli_command_metrics"]["sdk_verified_unknown_attempts"] == 1
    assert grade["cli_command_metrics"]["sdk_resolved_writes"] == 0
    assert grade["cli_command_metrics"]["resource_posts"] == 1
    assert (fixture.root / "trace.jsonl").read_bytes() == original
    fixture.case["fixture_version"] = 29
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    old = evaluation.score(fixture.root, FINAL)
    assert not old["checks"]["sdk_resolved_target_before_post"]
    assert "sdk_verified_unknown_attempts" not in old["cli_command_metrics"]


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "actor",
        "hardware",
        "missing_snapshot",
        "command_sent",
        "replay",
        "status",
        "freshness",
        "command",
        "read_url",
    ],
)
def test_unknown_selection_requires_correlated_wire_and_exact_request(tmp_path, monkeypatch, fault):
    fixture = attempted(tmp_path, monkeypatch)
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    command = next(row for row in trace if row.get("args", {}).get("action") == "command")
    if fault == "scope":
        command["result"]["resolution"]["selected_scope"]["origin"] = "https://wrong.example"
    elif fault == "actor":
        command["result"]["resolution"]["selected_scope"]["actor"] = {"external_user_id": "other"}
    elif fault == "command_sent":
        command["result"]["command_sent"] = False
    elif fault == "freshness":
        for row in wire:
            if "resource_read_state" in row:
                row["resource_read_state"]["age_seconds"] = 60
    elif fault == "command":
        next(row for row in wire if row["method"] == "POST")["command"] = "lock node-10 dev2 1h"
    elif fault == "read_url":
        for row in wire:
            if "resource_resolution_snapshot" in row:
                row["url"] = "https://wrong.example/api/agent/resources"
    elif fault == "replay":
        wire.append(copy.deepcopy(next(row for row in wire if row["method"] == "POST")))
    elif fault == "status":
        next(row for row in wire if row["method"] == "POST")["status"] = 200
    else:
        for row in wire:
            if "resource_resolution_snapshot" not in row:
                continue
            if fault == "missing_snapshot":
                del row["resource_resolution_snapshot"]
            else:
                row["resource_resolution_snapshot"]["resources"] = []
    for name, rows in (("trace.jsonl", trace), ("cli-http.jsonl", wire)):
        (fixture.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert not evaluation.score(fixture.root, FINAL)["checks"]["sdk_resolved_target_before_post"]
