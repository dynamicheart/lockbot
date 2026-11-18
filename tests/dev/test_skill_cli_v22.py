"""v22 distinguishes a proven targetless wrapper rejection from selecting a target."""

import copy
import importlib
import json
import sys

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned

matrix = importlib.import_module("skill_eval_matrix")
FINAL = "已将加速卡群 node-19 的0号卡以共享模式锁定4小时。"


def observed_shared_write(fixture):
    assert run(fixture, "command") == {"error": "group_required"}
    catalog = run(fixture, "resources-all")["result"]
    assert catalog["complete"] and len(catalog["groups"]) == 2
    assert run(fixture, "resources", group=evaluation.intent.CARDS, nodes=["node-19"])["result"]["resources"]
    assert run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation="slock",
        nodes=["node-19"],
        devices=["0"],
        device_count=1,
        duration="4h",
    )["result"]["verified"]


@pytest.mark.parametrize("version", [18, 19, 20, 21, 22])
def test_actual_targetless_rejection_then_discovery_and_one_write_is_versioned(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 22)
    observed_shared_write(fixture)
    fixture.case["fixture_version"] = version
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    before = {name: (fixture.root / name).read_bytes() for name in ("case.json", "trace.jsonl", "cli-http.jsonl")}
    grade = evaluation.score(fixture.root, FINAL)
    assert grade["observable_pass"] is (version == 22), grade
    for check in (
        "both_candidate_groups_examined",
        "exactly_one_correct_cli_write",
        "verified_before_and_after",
        "agent_read_inventory_before_target_selection",
    ):
        assert grade["checks"][check] is (version == 22)
    assert grade["cli_command_metrics"] == {
        "tool_attempts": 2,
        "pre_http_argument_corrections": 1 if version == 22 else 0,
        "mutation_attempts": 1 if version == 22 else 2,
        "resource_posts": 1,
    }
    assert {name: (fixture.root / name).read_bytes() for name in before} == before


def targetless():
    return {
        "tool": "run_lockbot",
        "args": {"action": "command"},
        "result": {"error": "group_required"},
        "http_request_span": [0, 0],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("nodes", ["node-19"]),
        ("node", "node-19"),
        ("devices", ["0"]),
        ("device", "0"),
        ("group", "dev0"),
        ("requested_group", "dev0"),
        ("connection", "private/group-7.json"),
        ("operation", "slock"),
        ("model", "P800"),
        ("nodes", []),
    ],
)
def test_supplied_target_or_other_arguments_never_qualify_as_empty_validation(field, value):
    rejected = targetless()
    rejected["args"][field] = value
    retained, corrections = evaluation.intent.targetless_command_prefix([rejected])
    assert retained == [rejected] and corrections == 0
    assert evaluation.intent.command_attempts([rejected], fixture_version=22) == ([rejected], 0)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_span",
        "nonzero_span",
        "http_span",
        "boolean_span",
        "negative_span",
        "argv",
        "scope_error",
        "uncertain",
        "retry_hint",
        "verified_result",
        "other_tool",
        "after_guess",
        "after_sent",
        "after_uncertain",
    ],
)
def test_unproven_rejections_and_later_calls_remain_attempts(fault):
    rejected = targetless()
    rows = [rejected]
    if fault == "missing_span":
        rejected.pop("http_request_span")
    elif fault.endswith("span"):
        rejected["http_request_span"] = {
            "nonzero_span": [1, 1],
            "http_span": [0, 1],
            "boolean_span": [False, False],
            "negative_span": [-1, -1],
        }[fault]
    elif fault == "argv":
        rejected["argv"] = []
    elif fault == "scope_error":
        rejected["result"] = {"error": "selected_connection_does_not_match_requested_group"}
    elif fault == "uncertain":
        rejected["result"] = {"error": "mutation_outcome_unknown", "retry_mutation": False}
    elif fault == "retry_hint":
        rejected["result"]["retry_mutation"] = False
    elif fault == "verified_result":
        rejected["result"]["result"] = {"verified": True}
    elif fault == "other_tool":
        rejected["tool"] = "other_tool"
    else:
        prior = targetless()
        if fault == "after_guess":
            prior["args"]["nodes"] = ["node-20"]
        elif fault == "after_sent":
            prior.update(argv=["/scripts/lockbot_cli.py", "command"], http_request_span=[0, 1])
        else:
            prior["result"] = {"error": "mutation_outcome_unknown"}
        rows.insert(0, prior)
    retained, corrections = evaluation.intent.targetless_command_prefix(rows)
    assert retained == rows and corrections == 0
    assert evaluation.intent.command_attempts(rows, fixture_version=22) == (rows, 0)


@pytest.mark.parametrize("fault", ["target_guess", "missing_span", "late_empty", "repeated_command", "extra_post"])
def test_v22_keeps_target_guesses_retries_and_http_mutations_failed(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 22)
    observed_shared_write(fixture)
    trace = events(fixture)
    if fault == "target_guess":
        trace[0]["args"]["nodes"] = ["node-20"]
    elif fault == "missing_span":
        trace[0].pop("http_request_span")
    elif fault == "late_empty":
        trace.append(copy.deepcopy(trace.pop(0)))
    elif fault == "repeated_command":
        trace.append(copy.deepcopy(trace[-1]))
    else:
        wire = events(fixture, "cli-http.jsonl")
        wire.append(copy.deepcopy(next(row for row in wire if row["method"] == "POST")))
        (fixture.root / "cli-http.jsonl").write_text("".join(json.dumps(row) + "\n" for row in wire))
    (fixture.root / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    grade = evaluation.score(fixture.root, FINAL)
    assert not grade["observable_pass"]
    assert not grade["checks"]["exactly_one_correct_cli_write"]


def test_v22_preserves_prompts_schema_and_requires_explicit_matrix_opt_in(tmp_path, monkeypatch):
    _, current = versioned(tmp_path, monkeypatch, "intent-shared-tail", 22)
    previous = tmp_path / "fixture-21"
    evaluation.prepare(previous, tmp_path / "artifact", "intent-shared-tail", fixture_version=21)
    assert evaluation.tools(current.root) == evaluation.tools(previous)
    for name in ("prompt.txt", "system.txt"):
        assert (current.root / name).read_bytes() == (previous / name).read_bytes()
    runner = tmp_path / "runner.json"
    runner.write_text(json.dumps([sys.executable, "synthetic-adapter-not-executed.py"]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "matrix",
            "run",
            "--runner",
            "synthetic",
            "no-model",
            str(runner),
            "--case",
            "cli:intent-shared-tail",
            "--artifacts",
            str(tmp_path / "artifact"),
            "--output",
            str(tmp_path / "evidence"),
            "--cli-fixture-version",
            "22",
        ],
    )
    monkeypatch.setattr(matrix, "freeze", lambda *args: {"revision": "revision"})
    observed = []
    monkeypatch.setattr(matrix, "run_matrix", lambda *args, **kwargs: observed.append(kwargs) or True)
    assert matrix.main() == 0
    assert observed == [{"cli_fixture_version": 22}]


def test_v22_does_not_relax_zero_command_requirements_for_query_only_requests(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-find-p800", 22)
    assert run(fixture, "command") == {"error": "group_required"}
    assert run(fixture, "resources-all")["result"]["complete"]
    grade = evaluation.score(fixture.root, "node-23是空闲P800整机。")
    assert not grade["observable_pass"]
    assert not grade["checks"]["zero_write_attempts"]


def test_v22_still_requires_inventory_before_selecting_an_actual_target(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 22)
    assert run(fixture, "command") == {"error": "group_required"}
    assert run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation="slock",
        nodes=["node-19"],
        devices=["0"],
        duration="4h",
    )["result"]["verified"]
    grade = evaluation.score(fixture.root, FINAL)
    assert grade["checks"]["exactly_one_correct_cli_write"]
    assert grade["checks"]["verified_before_and_after"]
    assert not grade["checks"]["agent_read_inventory_before_target_selection"]
    assert not grade["checks"]["both_candidate_groups_examined"]
    assert not grade["observable_pass"]
