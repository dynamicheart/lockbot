"""Version 36 certainty checks use the real successful claim as their baseline."""

import copy

import pytest
from test_skill_cli_argv_protocol import cli, prepare, run
from test_skill_cli_v35_counterexamples import rows, save_rows
from test_skill_cli_v36_followup import claim_sequence

ANSWER = "已领取 node-20，沿用预约时长2小时。"


def claim_case(tmp_path, sequence=True, version=36):
    if sequence:
        return claim_sequence(tmp_path, version=version)
    fixture = prepare(tmp_path, "intent-claim", version=version)
    result = run(fixture, "command", "--operation", "claim", "--node", "20" if version >= 36 else "node-20")
    assert result["result"]["verified"] is True
    return fixture


def bridge(fixture, events):
    wire = rows(fixture, "cli-http.jsonl")
    for row in wire:
        if row.get("resource_bot") is not None:
            row["url"] = row["url"].replace("/api/agent/", f"/api/bots/{row['resource_bot']}/")
    return cli.verified_sdk_resolutions(fixture, cli.argv_host.for_grading(events, fixture.case), wire)


@pytest.mark.parametrize("sequence", [False, True])
@pytest.mark.parametrize(
    "layer,field,value",
    [
        ("top", "command_outcome", "unknown"),
        ("top", "command_outcome", "rejected"),
        ("inner", "command_outcome", "unknown"),
        ("inner", "command_outcome", "rejected"),
        ("top", "retry_mutation", True),
        ("inner", "retry_mutation", True),
        ("top", "command_sent", False),
        ("inner", "command_sent", False),
        ("top", "unknown_write", True),
        ("inner", "unknown_write", True),
        ("inner", "error", "command_outcome_unknown"),
        ("server", "error", "rejected"),
        ("top", "exit_code", False),
        ("top", "exit_code", 0.0),
        ("inner", "verification", "unknown"),
        ("top", "mutation_attempted", False),
    ],
)
def test_contradictory_claim_metadata_cannot_pass_classifier_or_bridge(tmp_path, sequence, layer, field, value):
    fixture = claim_case(tmp_path, sequence=sequence)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    events = rows(fixture)
    assert any(proof["positive"] for proof in bridge(fixture, events).values())
    claim = [row for row in events if row["tool"] == "run_lockbot"][-1]
    target = claim["result"]
    if layer in {"inner", "server"}:
        target = target["result"]
    if layer == "server":
        target = target["command_result"]
    target[field] = value
    assert not any(proof["positive"] for proof in bridge(fixture, events).values())
    save_rows(fixture, "trace.jsonl", events)
    grade = cli.score(fixture.root, ANSWER)
    assert not grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["read_only_claim_diagnostics"] == 0
    assert grade["cli_command_metrics"]["resource_posts"] == 1
    assert grade["cli_command_metrics"]["tool_attempts"] == (2 if sequence else 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified", True),
        ("mutation_attempted", True),
        ("unknown_write", True),
        ("command_outcome", "unknown"),
        ("exit_code", 2.0),
    ],
)
def test_read_only_diagnostic_cannot_claim_a_write_or_uncertainty(tmp_path, field, value):
    fixture = claim_case(tmp_path)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    events = rows(fixture)
    diagnostic = [row for row in events if row["tool"] == "run_lockbot"][0]
    diagnostic["result"][field] = value
    save_rows(fixture, "trace.jsonl", events)
    grade = cli.score(fixture.root, ANSWER)
    assert not grade["observable_pass"]
    assert grade["cli_command_metrics"]["read_only_claim_diagnostics"] == 0


@pytest.mark.parametrize("sequence", [False, True])
def test_optional_absence_and_consistent_flags_remain_valid(tmp_path, sequence):
    fixture = claim_case(tmp_path, sequence=sequence)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    events = rows(fixture)
    claim = [row for row in events if row["tool"] == "run_lockbot"][-1]
    assert "retry_mutation" not in claim["result"]
    for layer in (claim["result"], claim["result"]["result"]):
        layer.update(
            command_sent=True,
            retry_mutation=False,
            unknown_write=False,
            verified=True,
            mutation_attempted=True,
            exit_code=0,
            command_outcome="not_structured_by_server",
            verification="observed_state_only",
        )
    save_rows(fixture, "trace.jsonl", events)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]


def test_v35_direct_claim_metadata_grading_is_not_rewritten(tmp_path):
    fixture = claim_case(tmp_path, sequence=False, version=35)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    events = rows(fixture)
    claim = [row for row in events if row["tool"] == "run_lockbot"][-1]
    claim["result"]["command_outcome"] = "unknown"
    save_rows(fixture, "trace.jsonl", events)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]


@pytest.mark.parametrize(
    "field", ["retry_mutation", "unknown_write", "command_sent", "verified", "exit_code", "command_outcome"]
)
def test_present_null_metadata_is_not_treated_as_absence(field):
    output = {"result": {"verified": True, "command_outcome": "not_structured_by_server"}}
    assert cli.claim_metadata_consistent(output)
    altered = copy.deepcopy(output)
    altered[field] = None
    assert not cli.claim_metadata_consistent(altered)
