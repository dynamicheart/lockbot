"""Whole-run reviews bind actual CLI35 phases without changing archived outcomes."""

import copy
import json
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.trace_assessment import checked_assessment
from lockbot.trace_cli_assessment import PHASE_COUNTS
from test_skill_cli_argv_protocol import artifact, cli, run
from test_trace_assessment_overlay import saved_review
from test_trace_fixture_identity import tree_hashes, write

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
import assessment_overlay as overlay  # noqa: E402
import cli_run_assessment as adapter  # noqa: E402
import publish_dataset as publisher  # noqa: E402
import skill_eval_matrix as evaluation  # noqa: E402
from merge_snapshots import trial_identity  # noqa: E402
from test_trace_dataset_ui import VIEWER, function  # noqa: E402
from test_trace_dataset_ui import run as js_run
from test_trace_render_labels import label_functions  # noqa: E402


def source_run(tmp_path, *, blocked=False, scenario="approved-enrollment"):
    """Execute packaged enrollment and its actual simulated acknowledgment transition."""
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    frozen = evaluation.freeze(artifact(tmp_path), source / "artifacts/current", cli.base.ORIGIN)
    first = source / "cli" / scenario / "001/phase-1"
    cli.prepare(first, source / "artifacts/current", scenario, fixture_version=35)
    fixture = cli.Fixture(first)
    fixture.call("read_lockbot_skill", {"offset": 0})
    if scenario == "reset-personal-key":
        final = "旧 Key 会失效，其他 Agent 会断开，需要换新 Key。确认重置吗？"
    else:
        issued = run(fixture, "enroll", "--origin", fixture.origin, "--group", cli.GROUP, "--authorized")
        assert issued.get("exit_code") == 0, issued
        final = issued["result"]["user_instruction_zh"] + " 在验证群发送。"
    runner = {"client": "local-fixture", "model": "fixture-only"}
    rows = []

    def finish(location, final):
        case = json.loads((location / "case.json").read_text())
        row = {
            **cli.score(location, final),
            **runner,
            "suite": "cli",
            "scenario": scenario,
            "phase": case["phase"],
            "status": "completed",
            "fixture_version": 35,
            "cli_fixture_version": 35,
            "evidence": location.relative_to(source).as_posix(),
            "skill_sha256": case["skill_sha256"],
            "artifact_revision": frozen["revision"],
        }
        write(location / "response.json", {"text": final})
        write(location / "result.json", row)
        rows.append(row)

    finish(first, final)
    if not blocked:
        second = first.parent / "phase-2"
        cli.resume(first, second)
        resumed = cli.Fixture(second)
        if scenario == "approved-enrollment":
            pending = run(resumed, "connections")["result"]["pending_enrollments"][0]["enrollment_dir"]
            assert run(resumed, "finish-enrollment", "--enrollment-dir", pending)["result"]["connection_created"]
            connection = run(resumed, "connections")["result"]["connections"][0]["connection"]
            resources = run(resumed, "--connection", connection, "resources")
            assert resources["result"]["resources"], resources
            finish(second, "node-a")
    for phase in range(len(rows) + 1, PHASE_COUNTS[scenario] + 1):
        rows.append(
            {
                **runner,
                "suite": "cli",
                "scenario": scenario,
                "phase": phase,
                "cli_fixture_version": 35,
                "status": f"blocked_by_phase_{phase - 1}",
            }
        )
    plan = {
        "simulation_only": True,
        "artifacts": {"current": frozen},
        "cases": ["cli:" + scenario],
        "runners": [runner],
        "cli_fixture_version": 35,
    }
    write(source / "run.json", plan)
    write(source / "matrix.json", {**plan, "results": rows})
    (source / "progress.jsonl").write_text("")
    return source, {**runner, "suite": "cli", "scenario": scenario, "phase_scope": "all"}


def data_and_entry(source, target):
    entry = adapter.run_source(source, target)
    key = entry["target"]["source_run"]
    data = {"cases": {entry["target"]["case_id"]: [{"source_run": key}]}, "assessments": {key: entry}}
    return data, entry


def review(tmp_path, source, target, *, status="pass", phases=(1, 2), findings=()):
    report = tmp_path / "review.md"
    report.write_text("Independent model review of exact recorded phases only.")
    record = overlay.bind_review(
        source,
        target,
        report,
        reviewer={"kind": "model", "name": "Astra", "requested_model": "gpt-6-astra"},
        status=status,
        reviewed_phases=list(phases),
        findings=findings,
    )
    return record, report


def test_real_two_phase_review_import_is_opt_in_and_preserves_every_original_identity(tmp_path):
    source, target = source_run(tmp_path)
    before = tree_hashes(source)
    board = tmp_path / "board"
    first = publisher.export_dataset(source, board, minimal_summary=True)
    identity = first["dataset"]["dataset_id"]
    assert not publisher.previous_assessments(board, identity)
    directory = board / "datasets" / identity
    original = {name: (directory / name).read_bytes() for name in ("raw.json", "latest.json", "metadata.json")}
    record, report = review(tmp_path, source, target)
    second = publisher.export_dataset(
        source,
        board,
        minimal_summary=True,
        cli_assessments=True,
        assessment_reviews=[(saved_review(tmp_path, record), report)],
    )
    assert first["dataset"] == second["dataset"]
    entry = publisher.previous_assessments(board, identity)[record["target"]["source_run"]]
    raw = json.loads(original["raw.json"])
    assert entry["semantic"] == "pass" and len(entry["phase_evidence"]) == 2
    assert checked_assessment(entry, record["target"]["source_run"], raw) == entry
    assert len(raw["cases"][target["scenario"]]) == 1
    for name, content in original.items():
        assert (directory / name).read_bytes() == content
        if name != "metadata.json":
            assert trial_identity(json.loads(content), target["scenario"], 0, name == "raw.json") == trial_identity(
                json.loads((directory / name).read_bytes()), target["scenario"], 0, name == "raw.json"
            )
    index = (board / "assessments/index.json").read_bytes()
    publisher.export_dataset(
        source,
        board,
        minimal_summary=True,
        cli_assessments=True,
        assessment_reviews=[(saved_review(tmp_path, record, "again.json"), report)],
    )
    assert (board / "assessments/index.json").read_bytes() == index
    assert tree_hashes(source) == before


@pytest.mark.parametrize("scenario", ["approved-enrollment", "reset-personal-key"])
def test_blocked_later_phases_are_explicit_and_cannot_receive_a_complete_pass(tmp_path, scenario):
    source, target = source_run(tmp_path, blocked=True, scenario=scenario)
    data, entry = data_and_entry(source, target)
    assert entry["execution"] == "incomplete"
    assert len(entry["phase_evidence"]) == PHASE_COUNTS[scenario]
    assert all(not phase["observed"] and not any(phase["hashes"].values()) for phase in entry["phase_evidence"][1:])
    with pytest.raises(ValueError, match="cover completed"):
        review(tmp_path, source, target, phases=(1,))
    record, report = review(tmp_path, source, target, status="uncertain", phases=(1,))
    assert overlay.attach_review(data, source, record, report)
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    with pytest.raises(ValueError, match="cover completed"):
        review(tmp_path, source, target, status="uncertain", phases=(1, 2))


@pytest.mark.parametrize(
    "change",
    [
        "later-prompt",
        "later-trace",
        "native",
        "absent-file",
        "matrix",
        "plan",
        "report",
        "phase-omission",
        "cross-run",
        "wrong-family",
        "symlink",
    ],
)
def test_any_phase_input_or_explicit_coverage_tampering_rejects_without_mutation(tmp_path, change):
    source, target = source_run(tmp_path)
    record, report = review(tmp_path, source, target)
    data, entry = data_and_entry(source, target)
    directory = source / "cli/approved-enrollment/001/phase-2"
    if change in {"later-prompt", "later-trace", "native", "absent-file"}:
        name = {
            "later-prompt": "prompt.txt",
            "later-trace": "trace.jsonl",
            "native": "native-ducc.stderr",
            "absent-file": "cli-http.jsonl",
        }[change]
        path = directory / name
        path.write_bytes((path.read_bytes() if path.exists() else b"") + b"changed")
    elif change in {"matrix", "plan"}:
        path = source / ("matrix.json" if change == "matrix" else "run.json")
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "report":
        report.write_text("Changed report")
    elif change == "symlink":
        original = directory / "response.json"
        moved = directory / "original-response.json"
        original.rename(moved)
        original.symlink_to(moved)
    else:
        if change == "phase-omission":
            record["reviewed_phases"] = [1]
        elif change == "cross-run":
            record["target"]["client"] = "another-runner"
        else:
            record["target"]["suite"] = "resource-cli"
        record["review_id"] = overlay.digest({key: value for key, value in record.items() if key != "review_id"})
    before = copy.deepcopy(data)
    with pytest.raises((ValueError, KeyError)):
        overlay.attach_review(data, source, record, report)
    assert data == before and entry["reviews"] == []


@pytest.mark.parametrize(
    "change", ["old-version", "unknown-scenario", "manual-policy", "human-gate", "extra-phase", "unexecuted-file"]
)
def test_unknown_policy_or_phase_never_waives_a_gate(tmp_path, change):
    source, target = source_run(tmp_path, blocked=True)
    path = source / "run.json"
    plan = json.loads(path.read_text())
    if change == "old-version":
        plan["cli_fixture_version"] = 34
    elif change == "unknown-scenario":
        target["scenario"] = "intent-unknown"
    elif change == "manual-policy":
        plan["manual_review_required"] = ["Explicit human acceptance required."]
    elif change == "human-gate":
        plan["human_gate"] = False  # Even a present false field is not silently reinterpreted.
    elif change == "extra-phase":
        matrix_path = source / "matrix.json"
        matrix = json.loads(matrix_path.read_text())
        matrix["results"].append({**matrix["results"][-1], "phase": 3})
        write(matrix_path, matrix)
    else:
        missing = source / "cli/approved-enrollment/001/phase-2"
        missing.mkdir()
        (missing / "response.json").write_text('{"text":"later invented answer"}')
    write(path, plan)
    with pytest.raises(ValueError):
        adapter.run_source(source, target)


def test_reader_and_viewer_keep_partial_reviews_unknown_and_original_failure_priority(tmp_path):
    source, target = source_run(tmp_path)
    data, entry = data_and_entry(source, target)
    record, report = review(tmp_path, source, target)
    overlay.attach_review(data, source, record, report)
    checked_assessment(entry, entry["target"]["source_run"], data)
    altered = copy.deepcopy(entry)
    altered["reviews"][0]["reviewed_phases"] = [1]
    altered["reviews"][0]["review_id"] = overlay.digest(
        {key: value for key, value in altered["reviews"][0].items() if key != "review_id"}
    )
    with pytest.raises(ValueError):
        checked_assessment(altered, altered["target"]["source_run"], data)
    result = js_run(
        label_functions()
        + function(VIEWER, "assessmentPanel")
        + "\nlet data="
        + json.dumps(data)
        + ";"
        + """
const trial=Object.values(data.cases)[0][0];
const states=['fail','timeout','blocked'];
const preserved=states.map(state=>trialStatus({...trial,state}));
const untouched=JSON.stringify(data);
const label=trialStatus({...trial,state:'pending'});
console.log(JSON.stringify({preserved,label,unchanged:untouched===JSON.stringify(data)}));
"""
    )
    assert result["preserved"] == ["未通过", "超时", "受阻"] and result["unchanged"]
    assert result["label"] == ("通过" if entry["behavior"] == "pass" else "未通过")


def test_partial_nonblocking_finding_does_not_approve_unreviewed_phase(tmp_path):
    source, target = source_run(tmp_path)
    data, entry = data_and_entry(source, target)
    record, report = review(
        tmp_path,
        source,
        target,
        status="findings",
        phases=(1,),
        findings=[{"severity": "P3", "blocking": False, "text": "An unnecessary read in phase one."}],
    )
    overlay.attach_review(data, source, record, report)
    assert entry["semantic"] == "uncertain"
    checked_assessment(entry, entry["target"]["source_run"], data)
    entry["semantic"] = "findings"
    with pytest.raises(ValueError, match="semantic"):
        checked_assessment(entry, entry["target"]["source_run"], data)


def test_runtime_phase_with_no_final_can_be_reviewed_but_cannot_pass_whole_run(tmp_path):
    source, target = source_run(tmp_path)
    matrix_path = source / "matrix.json"
    matrix = json.loads(matrix_path.read_text())
    row = matrix["results"][1]
    row.update(status="budget_exhausted", observable_pass=False)
    directory = source / row["evidence"]
    (directory / "response.json").unlink()
    write(directory / "result.json", row)
    write(matrix_path, matrix)
    data, entry = data_and_entry(source, target)
    assert entry["execution"] == "incomplete" and entry["phase_evidence"][1]["observed"]
    record, report = review(tmp_path, source, target, status="uncertain")
    overlay.attach_review(data, source, record, report)
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    with pytest.raises(ValueError, match="cover completed"):
        review(tmp_path, source, target)


@pytest.mark.parametrize("change", ["binding", "phase-count", "version", "family", "topics", "coverage", "human"])
def test_reader_rejects_forged_schema2_policy_or_coverage(tmp_path, change):
    source, target = source_run(tmp_path)
    data, entry = data_and_entry(source, target)
    record, report = review(tmp_path, source, target)
    overlay.attach_review(data, source, record, report)
    altered = copy.deepcopy(entry)
    if change == "binding":
        altered["phase_evidence"][1]["hashes"]["prompt.txt"] = "a" * 64
    elif change == "phase-count":
        altered["phase_evidence"].pop()
    elif change == "version":
        altered["adapter"]["version"] = 37
    elif change == "family":
        altered["adapter"]["family"] = "cli-release"
    elif change == "topics":
        altered["topics"] = ["An explicit human acceptance requirement is waived."]
    elif change == "coverage":
        altered["reviews"][0]["reviewed_phases"] = [1, True]
    else:
        altered["reviews"][0]["reviewer"]["kind"] = "human"
    if change in {"coverage", "human"}:
        item = altered["reviews"][0]
        item["review_id"] = overlay.digest({key: value for key, value in item.items() if key != "review_id"})
    with pytest.raises(ValueError):
        checked_assessment(altered, altered["target"]["source_run"], data)


def test_completed_but_ungraded_run_does_not_display_an_overall_pass(tmp_path):
    source, target = source_run(tmp_path)
    data, entry = data_and_entry(source, target)
    for phase in entry["phase_evidence"]:
        phase["observable_pass"] = None
    entry["behavior"] = "unknown"
    entry["binding"]["phase_evidence"] = overlay.digest(entry["phase_evidence"])
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    value = js_run(
        label_functions()
        + "\nlet data="
        + json.dumps(data)
        + ";"
        + """
const trial=Object.values(data.cases)[0][0];
console.log(JSON.stringify({badge:assessmentBadge({...trial,state:'pending'})}));
"""
    )
    assert value["badge"] is None
