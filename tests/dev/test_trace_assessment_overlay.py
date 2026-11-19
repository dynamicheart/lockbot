"""Assessment sidecars preserve actual trial bytes and independently bind review evidence."""

import copy
import json
import sys
from pathlib import Path

import pytest
from test_trace_fixture_identity import prepared_run, tree_hashes

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
import assessment_overlay as assessments  # noqa: E402
import publish_dataset as publisher  # noqa: E402
import skill_consultation_eval as consultation  # noqa: E402
from merge_snapshots import trial_identity  # noqa: E402


def source_and_review(tmp_path, *, status="pass", findings=()):
    source, row, _ = prepared_run(tmp_path)
    target = {key: row[key] for key in ("suite", "scenario", "client", "model", "phase")}
    report = tmp_path / "review.md"
    report.write_text("Independent model review of the recorded phase.\n")
    record = assessments.bind_review(
        source,
        target,
        report,
        reviewer={"kind": "model", "name": "Astra", "requested_model": "gpt-6-astra"},
        status=status,
        findings=findings,
    )
    return source, record, report


def saved_review(tmp_path, record, name="review.json"):
    path = tmp_path / name
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path


def exported(tmp_path, source):
    board = tmp_path / "board"
    result = publisher.export_dataset(source, board, minimal_summary=True)
    identity = result["dataset"]["dataset_id"]
    folder = board / "datasets" / identity
    original = {name: (folder / name).read_bytes() for name in ("raw.json", "latest.json", "metadata.json")}
    return board, result, original


def test_model_review_does_not_change_registered_dataset_comment_ids_or_trials(tmp_path):
    source, review, report = source_and_review(tmp_path)
    before_source = tree_hashes(source)
    board, first, original = exported(tmp_path, source)
    identity = first["dataset"]["dataset_id"]
    prior = publisher.previous_assessments(board, identity)
    assert len(prior) == 1
    record = next(iter(prior.values()))
    assert (record["behavior"], record["semantic"], record["human_gate"]) == ("pass", "not_reviewed", "not_required")
    sidecar = saved_review(tmp_path, review)
    second = publisher.export_dataset(source, board, minimal_summary=True, assessment_reviews=[(sidecar, report)])
    assert second["dataset"] == first["dataset"]
    folder = board / "datasets" / identity
    assert {name: (folder / name).read_bytes() for name in original} == original
    after = publisher.previous_assessments(board, identity)
    entry = next(iter(after.values()))
    assert entry["semantic"] == "pass" and entry["human_gate"] == "not_required"
    assert entry["reviews"][0]["reviewer"]["kind"] == "model"
    for raw, name in ((True, "raw.json"), (False, "latest.json")):
        old = json.loads(original[name])
        current = json.loads((folder / name).read_bytes())
        assert "assessments" not in old and len(current["cases"]) == 1
        for case, trials in old["cases"].items():
            assert len(trials) == len(current["cases"][case]) == 1
            assert trial_identity(old, case, 0, raw) == trial_identity(current, case, 0, raw)
    index = (board / "assessments/index.json").read_bytes()
    publisher.export_dataset(source, board, minimal_summary=True, assessment_reviews=[(sidecar, report)])
    assert (board / "assessments/index.json").read_bytes() == index
    assert tree_hashes(source) == before_source


@pytest.mark.parametrize(
    "filename",
    [
        "prompt.txt",
        "response.json",
        "trace.jsonl",
        "cli-http.jsonl",
        "result.json",
        "case.json",
        "subject.md",
        "artifact/SKILL.md",
        "artifact/manifest.json",
        "artifact/lockbot-agent.zip",
        "matrix.json",
    ],
)
def test_changed_reviewed_evidence_is_rejected_without_changing_published_files(tmp_path, filename):
    source, review, report = source_and_review(tmp_path)
    board, result, original = exported(tmp_path, source)
    data = {
        "cases": json.loads(original["raw.json"])["cases"],
        "assessments": publisher.previous_assessments(board, result["dataset"]["dataset_id"]),
    }
    before = copy.deepcopy(data)
    phase = source / "resource-cli/find-p800/001/phase-1"
    if filename.startswith("artifact/"):
        path = source / "artifacts/current" / filename.split("/", 1)[1]
    elif filename == "matrix.json":
        path = source / filename
        value = json.loads(path.read_text())
        value["results"][0]["duration_seconds"] = 999
        path.write_text(json.dumps(value))
    else:
        path = phase / filename
    if filename != "matrix.json":
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises((ValueError, KeyError)):
        assessments.attach_review(data, source, review, report)
    assert data == before
    folder = board / "datasets" / result["dataset"]["dataset_id"]
    assert {name: (folder / name).read_bytes() for name in original} == original


@pytest.mark.parametrize("change", ["report", "review-id", "wrong-model", "wrong-phase", "human", "symlink"])
def test_report_identity_and_reviewer_tampering_never_become_approval(tmp_path, change):
    source, review, report = source_and_review(tmp_path)
    board, result, original = exported(tmp_path, source)
    data = {
        "cases": json.loads(original["raw.json"])["cases"],
        "assessments": publisher.previous_assessments(board, result["dataset"]["dataset_id"]),
    }
    if change == "report":
        report.write_text("A different report")
    elif change == "review-id":
        review["review_id"] = "a" * 64
    elif change == "wrong-model":
        review["target"]["model"] = "another-model"
    elif change == "wrong-phase":
        review["target"]["phase"] = 2
    elif change == "human":
        review["reviewer"]["kind"] = "human"
    else:
        link = tmp_path / "linked-report"
        link.symlink_to(report)
        report = link
    if change in {"wrong-model", "wrong-phase", "human"}:
        review["review_id"] = assessments.digest({k: v for k, v in review.items() if k != "review_id"})
    before = copy.deepcopy(data)
    with pytest.raises(ValueError):
        assessments.attach_review(data, source, review, report)
    assert data == before


def test_conflicting_model_reviews_stay_uncertain_and_corrections_retain_history(tmp_path):
    source, first, report = source_and_review(tmp_path)
    board, result, original = exported(tmp_path, source)
    data = {
        "cases": json.loads(original["raw.json"])["cases"],
        "assessments": publisher.previous_assessments(board, result["dataset"]["dataset_id"]),
    }
    assessments.attach_review(data, source, first, report)
    finding = {"severity": "P3", "blocking": False, "text": "One unnecessary read."}
    second = assessments.bind_review(
        source, first["target"], report, reviewer=first["reviewer"], status="findings", findings=[finding]
    )
    assessments.attach_review(data, source, second, report)
    entry = data["assessments"][first["target"]["source_run"]]
    assert entry["semantic"] == "uncertain" and len(entry["reviews"]) == 2
    correction = assessments.bind_review(
        source,
        first["target"],
        report,
        reviewer=first["reviewer"],
        status="findings",
        findings=[finding],
        supersedes=first["review_id"],
    )
    assessments.attach_review(data, source, correction, report)
    assert entry["semantic"] == "findings" and len(entry["reviews"]) == 3
    assert len(data["cases"][first["target"]["case_id"]]) == 1


def test_full_review_history_rejects_new_records_without_losing_old_reviews(tmp_path):
    source, review, report = source_and_review(tmp_path)
    board, result, original = exported(tmp_path, source)
    data = {
        "cases": json.loads(original["raw.json"])["cases"],
        "assessments": publisher.previous_assessments(board, result["dataset"]["dataset_id"]),
    }
    for index in range(16):
        review["bound_at"] = f"test-binding-{index}"
        review["review_id"] = assessments.digest({key: item for key, item in review.items() if key != "review_id"})
        assessments.attach_review(data, source, review, report)
    before = copy.deepcopy(data)
    assert assessments.attach_review(data, source, review, report) is False
    review["bound_at"] = "one-too-many"
    review["review_id"] = assessments.digest({key: item for key, item in review.items() if key != "review_id"})
    with pytest.raises(ValueError, match="history is full"):
        assessments.attach_review(data, source, review, report)
    assert data == before


@pytest.mark.parametrize("explicit", [False, True])
def test_unknown_checklist_or_explicit_human_policy_keeps_legacy_gate(tmp_path, explicit):
    source, _, _ = source_and_review(tmp_path)
    path = source / "matrix.json"
    matrix = json.loads(path.read_text())
    row = matrix["results"][0]
    if explicit:
        row["human_review_gate"] = {"required": True, "reason": "Explicit test policy"}
    else:
        row["manual_review_required"] = ["Human acceptance is explicitly required here."]
    path.write_text(json.dumps(matrix))
    data = {"cases": {}}
    assessments.add_assessments(data, source)
    assert not data.get("assessments")


@pytest.mark.parametrize("version", [1, 2, 3])
def test_known_consultation_versions_keep_model_review_separate_from_human_gate(tmp_path, monkeypatch, version):
    source, original_row, _ = prepared_run(tmp_path)
    monkeypatch.setattr(consultation, "VERSION", version)
    location = source / "consultation/zero-utilization/001/phase-1"
    consultation.prepare(location, source / "artifacts/current", "zero-utilization")
    fixture = consultation.Fixture(location)
    fixture.call("read_consultation_skill", {"document": "references/workbench-and-metrics.md"})
    row = {
        **original_row,
        **consultation.score(location, "Utilization is not allocation."),
        "suite": "consultation",
        "scenario": "zero-utilization",
        "fixture_version": version,
        "evidence": location.relative_to(source).as_posix(),
    }
    plan = json.loads((source / "run.json").read_text())
    plan["cases"] = ["consultation:zero-utilization"]
    for name, value in [("run.json", plan), ("matrix.json", {**plan, "results": [row]})]:
        (source / name).write_text(json.dumps(value))
    (location / "result.json").write_text(json.dumps(row))
    (location / "response.json").write_text(json.dumps({"text": "Utilization is not allocation."}))
    record = publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    entries = publisher.previous_assessments(tmp_path / "board", record["dataset"]["dataset_id"])
    assert len(entries) == 1
    entry = next(iter(entries.values()))
    assert entry["adapter"] == {"family": "consultation", "version": version}
    assert (entry["behavior"], entry["semantic"], entry["human_gate"]) == ("pass", "not_reviewed", "not_required")
