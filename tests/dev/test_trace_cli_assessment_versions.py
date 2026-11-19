"""Synthetic metadata controls for known policy versions, not model acceptance.

These controls deliberately relabel only disposable fixture metadata to exercise
constructor/reader compatibility. Actual v36 runtime/model tests are separate.
"""

import copy
import json

import pytest
from lockbot.backend.app.trace_assessment import checked_assessment
from test_trace_cli_run_assessment import (
    adapter,
    data_and_entry,
    js_run,
    label_functions,
    overlay,
    publisher,
    review,
    source_run,
    write,
)


def metadata_source(tmp_path, version):
    """Create explicitly synthetic version metadata from a local CLI35 fixture."""
    source, target = source_run(tmp_path)
    matrix = json.loads((source / "matrix.json").read_text())
    plan = json.loads((source / "run.json").read_text())
    plan["cli_fixture_version"] = version
    matrix["cli_fixture_version"] = version
    for row in matrix["results"]:
        row.update(fixture_version=version, cli_fixture_version=version)
        directory = source / row["evidence"]
        case = json.loads((directory / "case.json").read_text())
        case["fixture_version"] = version
        write(directory / "case.json", case)
        write(directory / "result.json", row)
    write(source / "run.json", plan)
    write(source / "matrix.json", matrix)
    return source, target


@pytest.mark.parametrize("version", [35, 36])
def test_known_version_constructor_binder_reader_and_ui_accept_same_phase_policy(tmp_path, version):
    source, target = metadata_source(tmp_path, version)
    data, entry = data_and_entry(source, target)
    assert entry["adapter"] == {"family": "cli", "version": version}
    assert [phase["phase"] for phase in entry["phase_evidence"]] == [1, 2]
    record, report = review(tmp_path, source, target)
    overlay.attach_review(data, source, record, report)
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    result = js_run(
        label_functions()
        + "\nlet data="
        + json.dumps(data)
        + ";"
        + """
const trial=Object.values(data.cases)[0][0];
console.log(JSON.stringify({version:assessmentForTrial(trial)?.adapter.version,
 label:trialStatus({...trial,state:'pending'})}));
"""
    )
    assert result == {"version": version, "label": "通过" if entry["behavior"] == "pass" else "未通过"}
    board = tmp_path / "board"
    initial = publisher.export_dataset(source, board, minimal_summary=True)
    assert not publisher.previous_assessments(board, initial["dataset"]["dataset_id"])
    opted = publisher.export_dataset(source, board, minimal_summary=True, cli_assessments=True)
    assert opted["dataset"] == initial["dataset"]


@pytest.mark.parametrize("version", [35, 36])
@pytest.mark.parametrize(
    "mismatch",
    [
        "plan",
        "matrix",
        "row",
        "case",
        "row-fixture",
        "case-bool",
        "row-bool",
        "plan-bool",
        "unknown37",
        "human-gate",
        "manual-policy",
    ],
)
def test_version_mismatch_unknown_or_explicit_gate_is_not_a_known_policy(tmp_path, version, mismatch):
    source, target = metadata_source(tmp_path, version)
    other = 36 if version == 35 else 35
    plan_path, matrix_path = source / "run.json", source / "matrix.json"
    plan, matrix = json.loads(plan_path.read_text()), json.loads(matrix_path.read_text())
    directory = source / matrix["results"][1]["evidence"]
    case_path = directory / "case.json"
    case = json.loads(case_path.read_text())
    if mismatch == "plan":
        plan["cli_fixture_version"] = other
    elif mismatch == "matrix":
        matrix["cli_fixture_version"] = other
    elif mismatch == "row":
        matrix["results"][1]["cli_fixture_version"] = other
    elif mismatch == "case":
        case["fixture_version"] = other
    elif mismatch == "row-fixture":
        matrix["results"][1]["fixture_version"] = other
    elif mismatch == "case-bool":
        case["fixture_version"] = True
    elif mismatch == "row-bool":
        matrix["results"][1]["cli_fixture_version"] = True
    elif mismatch == "plan-bool":
        plan["cli_fixture_version"] = True
    elif mismatch == "unknown37":
        plan["cli_fixture_version"] = matrix["cli_fixture_version"] = 37
    elif mismatch == "human-gate":
        case["human_review_gate"] = {"required": True}
    else:
        case["manual_review_required"] = ["Explicit human acceptance is required."]
    write(plan_path, plan)
    write(matrix_path, matrix)
    write(case_path, case)
    write(directory / "result.json", matrix["results"][1])
    with pytest.raises(ValueError):
        adapter.run_source(source, target)


@pytest.mark.parametrize("version", [True, False, "36", 34, 37])
def test_reader_and_ui_reject_unknown_or_noninteger_adapter_versions(tmp_path, version):
    source, target = metadata_source(tmp_path, 36)
    data, entry = data_and_entry(source, target)
    altered = copy.deepcopy(entry)
    altered["adapter"]["version"] = version
    with pytest.raises(ValueError):
        checked_assessment(altered, altered["target"]["source_run"], data)
    data["assessments"][entry["target"]["source_run"]] = altered
    result = js_run(
        label_functions()
        + "\nlet data="
        + json.dumps(data)
        + ";"
        + """
const trial=Object.values(data.cases)[0][0];
console.log(JSON.stringify({accepted:assessmentForTrial(trial)!==null}));
"""
    )
    assert result["accepted"] is False
