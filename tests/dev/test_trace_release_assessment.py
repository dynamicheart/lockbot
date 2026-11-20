"""Opt-in release semantics stay separate from unchanged observed action grades."""

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
import assessment_overlay as assessments  # noqa: E402
import publish_dataset as publisher  # noqa: E402
from lockbot.backend.app.trace_assessment import checked_assessment  # noqa: E402
from test_skill_cli_argv_protocol import variant_artifact  # noqa: E402
from test_trace_assessment_overlay import saved_review, source_and_review  # noqa: E402
from test_trace_fixture_identity import evaluation, prepared_run, release, tree_hashes, write  # noqa: E402


@pytest.mark.parametrize("version", [3, 4, 5, 6])
def test_actual_release_versions_require_opt_in_and_keep_dataset_and_original_grade(tmp_path, version):
    source, review, report = source_and_review(
        tmp_path, suite="cli-release", scenario="alias-set", release_fixture_version=version
    )
    before_source = tree_hashes(source)
    output = tmp_path / "board"
    first = publisher.export_dataset(source, output, minimal_summary=True)
    assert first["assessment_overlay"] is None
    folder = output / "datasets" / first["dataset"]["dataset_id"]
    original = {name: (folder / name).read_bytes() for name in ("raw.json", "latest.json", "metadata.json")}
    sidecar = saved_review(tmp_path, review)
    with pytest.raises(ValueError, match="no matching derived assessment"):
        publisher.export_dataset(source, output, minimal_summary=True, assessment_reviews=[(sidecar, report)])
    second = publisher.export_dataset(
        source, output, minimal_summary=True, release_assessments=True, assessment_reviews=[(sidecar, report)]
    )
    assert second["dataset"] == first["dataset"]
    assert {name: (folder / name).read_bytes() for name in original} == original
    entries = publisher.previous_assessments(output, first["dataset"]["dataset_id"])
    assert len(entries) == 1
    entry = entries[review["target"]["source_run"]]
    assert entry["adapter"] == {"family": "cli-release", "version": version}
    assert entry["behavior"] == entry["semantic"] == "pass"
    assert entry["human_gate"] == "not_required"
    raw = json.loads(original["raw.json"])
    assert checked_assessment(entry, review["target"]["source_run"], raw) == entry
    assert raw["cases"]["cli-release-alias-set"][0]["state"] == "pass"
    index = (output / "assessments/index.json").read_bytes()
    publisher.export_dataset(
        source, output, minimal_summary=True, release_assessments=True, assessment_reviews=[(sidecar, report)]
    )
    assert (output / "assessments/index.json").read_bytes() == index
    assert tree_hashes(source) == before_source


@pytest.mark.parametrize("gate", ["row-human", "case-human", "row-checklist", "case-checklist"])
def test_release_adapter_does_not_waive_any_explicit_or_unknown_gate(tmp_path, gate):
    source, row, location = prepared_run(tmp_path, "cli-release", "alias-set", release_fixture_version=6)
    path = source / "matrix.json" if gate.startswith("row") else location / "case.json"
    value = json.loads(path.read_text())
    target = value["results"][0] if gate.startswith("row") else value
    key = "human_review_gate" if gate.endswith("human") else "manual_review_required"
    target[key] = {"required": True} if gate.endswith("human") else ["An explicit legacy review gate."]
    write(path, value)
    data = {"cases": {}}
    assessments.add_assessments(data, source, release_assessments=True)
    assert not data.get("assessments")
    report = tmp_path / "report.md"
    report.write_text("No approval is inferred.")
    with pytest.raises(ValueError, match="supported checklist adapter"):
        assessments.bind_review(
            source,
            row,
            report,
            reviewer={"kind": "model", "name": "Astra", "requested_model": "gpt-6-astra"},
            status="pass",
        )


@pytest.mark.parametrize(
    "change",
    [
        "empty-final",
        "timeout",
        "wrong-family",
        "unknown-version",
        "row-version",
        "row-family",
        "wrong-underlying",
        "unbound-source",
    ],
)
def test_incomplete_or_unbound_release_phase_cannot_receive_review(tmp_path, change):
    source, review, report = source_and_review(
        tmp_path, suite="cli-release", scenario="alias-set", release_fixture_version=6
    )
    output = tmp_path / "board"
    result = publisher.export_dataset(source, output, minimal_summary=True)
    folder = output / "datasets" / result["dataset"]["dataset_id"]
    data = json.loads((folder / "raw.json").read_text())
    location = source / "cli-release/alias-set/001/phase-1"
    if change == "empty-final":
        write(location / "response.json", {"text": ""})
    elif change in {"timeout", "row-version", "row-family"}:
        for path in (source / "matrix.json", location / "result.json"):
            value = json.loads(path.read_text())
            row = value["results"][0] if "results" in value else value
            key, item = {
                "timeout": ("status", "timeout"),
                "row-version": ("fixture_version", 99),
                "row-family": ("fixture_family", "other"),
            }[change]
            row[key] = item
            write(path, value)
    elif change == "unbound-source":
        data["cases"] = {}
    else:
        value = json.loads((location / "case.json").read_text())
        key, item = {
            "wrong-family": ("fixture_family", "other"),
            "unknown-version": ("release_fixture_version", 99),
            "wrong-underlying": ("scenario", "intent-unrelated"),
        }[change]
        value[key] = item
        write(location / "case.json", value)
    before = copy.deepcopy(data)
    if change in {"empty-final", "timeout", "unknown-version", "row-version", "row-family"}:
        assessments.add_assessments(data, source, release_assessments=True)
        assert data == before
    else:
        with pytest.raises(ValueError):
            assessments.add_assessments(data, source, release_assessments=True)
        assert data == before
    with pytest.raises(ValueError):
        assessments.attach_review(data, source, review, report)
    assert data == before


def update_source(tmp_path):
    source, original, old_location = prepared_run(tmp_path, "cli-release", "alias-set", release_fixture_version=6)
    current = source / "artifacts/current"
    older = variant_artifact(current, tmp_path / "older")
    installed = evaluation.freeze(older, source / "artifacts/installed", evaluation.onboarding.ORIGIN)
    shutil.rmtree(old_location.parents[1])
    location = source / "cli-release/update-inquiry/001/phase-1"
    release.prepare(
        location, current, "update-inquiry", installed_artifact=source / "artifacts/installed", fixture_version=6
    )
    fixture = release.ReleaseFixture(location)
    fixture.call("read_lockbot_skill", {"reference": "installation.json", "offset": 0})
    fixture.call("run_lockbot", {"argv": ["discover", "--origin", fixture.origin]})
    final = "A package difference is not proof of live compatibility."
    row = {
        **original,
        **release.score(location, final),
        "scenario": "update-inquiry",
        "evidence": location.relative_to(source).as_posix(),
    }
    plan = json.loads((source / "run.json").read_text())
    plan["cases"] = ["cli-release:update-inquiry"]
    plan["artifacts"]["installed"] = installed
    write(source / "run.json", plan)
    write(source / "matrix.json", {**plan, "results": [row]})
    write(location / "result.json", row)
    write(location / "response.json", {"text": final})
    return source, row


@pytest.mark.parametrize("changed_file", [None, "manifest.json", "SKILL.md", "lockbot-agent.zip"])
def test_update_semantics_bind_actual_older_artifact_and_reject_later_changes(tmp_path, changed_file):
    source, row = update_source(tmp_path)
    report = tmp_path / "review.md"
    report.write_text("An old SDK's default flag cannot establish live compatibility.")
    review = assessments.bind_review(
        source,
        row,
        report,
        reviewer={"kind": "model", "name": "Astra", "requested_model": "gpt-6-astra"},
        status="findings",
        findings=[{"severity": "P2", "blocking": True, "text": "Unsupported compatibility assurance."}],
    )
    assert len(review["binding"]) == 14
    assert "installed/lockbot-agent.zip" in review["binding"]
    output = tmp_path / "board"
    first = publisher.export_dataset(source, output, minimal_summary=True, release_assessments=True)
    raw = json.loads((output / "datasets" / first["dataset"]["dataset_id"] / "raw.json").read_text())
    data = {**raw, "assessments": publisher.previous_assessments(output, first["dataset"]["dataset_id"])}
    before = copy.deepcopy(data)
    if changed_file:
        path = source / "artifacts/installed" / changed_file
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(ValueError):
            assessments.attach_review(data, source, review, report)
        assert data == before
    else:
        assessments.attach_review(data, source, review, report)
        entry = data["assessments"][review["target"]["source_run"]]
        assert checked_assessment(entry, review["target"]["source_run"], raw)["semantic"] == "findings"
        assert data["cases"] == before["cases"]
