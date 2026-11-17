"""New continuity HTTPS phases bind their actual artifact; old rows keep their source."""

import io
import json
import zipfile
from hashlib import sha256

import pytest
from test_trace_merge import matrix, merge, write


def continuity_panel(tmp_path):
    directory, report = matrix(tmp_path, terminal=True, scenario="panel")
    (directory / "cli").rename(directory / "continuity")
    origin = "https://lockbot.example.test"
    revision = "c" * 16
    package = report["artifacts"]["current"]["package_version"]
    skill = (directory / "artifacts/current/SKILL.md").read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("lockbot-agent/installation.json", json.dumps({"deployment_origin": origin}))
    content = stream.getvalue()
    frozen = directory / "artifacts/panel"
    frozen.mkdir()
    (frozen / "SKILL.md").write_bytes(skill)
    (frozen / "lockbot-agent.zip").write_bytes(content)
    metadata = {"revision": revision, "package_version": package, "sha256": sha256(content).hexdigest()}
    write(
        frozen / "manifest.json",
        {
            "version": revision,
            "package_version": package,
            "skill_sha256": sha256(skill).hexdigest(),
            "installation": {"sha256": metadata["sha256"]},
        },
    )
    report["artifacts"]["panel"] = metadata
    report["cases"] = ["continuity:panel"]
    row = report["results"][0]
    row.update(
        suite="continuity",
        fixture_family="continuity",
        fixture_version=11,
        artifact_kind="panel",
        artifact_revision=revision,
        origin=origin,
        evidence="continuity/panel/001/phase-1",
    )
    location = directory / row["evidence"]
    case = json.loads((location / "case.json").read_text())
    case.update(
        {key: row[key] for key in ("fixture_family", "fixture_version", "artifact_kind", "artifact_revision", "origin")}
    )
    write(location / "case.json", case)
    write(location / "result.json", row)
    report["results"].append(
        {**{key: value for key, value in row.items() if key != "evidence"}, "phase": 2, "status": "blocked_by_phase_1"}
    )
    write(directory / "run.json", {key: value for key, value in report.items() if key != "results"})
    write(directory / "matrix.json", report)
    return directory, report, location


def test_https_continuity_v11_export_does_not_silently_drop_actual_revision(tmp_path):
    directory, report, location = continuity_panel(tmp_path)
    projected = merge.snapshot(directory, raw=True)
    trial = projected["cases"]["continuity-panel"][0]
    assert any(entry.get("text") == "Actual native reply" for session in trial["sessions"] for entry in session)
    pair = report["runners"][0]
    row, _ = merge.bind_semantic_phase(directory, report, {}, {"phase": 1}, "continuity", "panel", pair, location, 1)
    assert row["artifact_revision"] == report["artifacts"]["panel"]["revision"]


def test_forged_origin_in_both_matrix_and_case_still_fails_frozen_stamp_binding(tmp_path):
    directory, report, location = continuity_panel(tmp_path)
    report["results"][0]["origin"] = "https://foreign.invalid"
    write(directory / "matrix.json", report)
    case = json.loads((location / "case.json").read_text())
    case["origin"] = "https://foreign.invalid"
    write(location / "case.json", case)
    with pytest.raises(ValueError, match="deployment origin"):
        merge.bind_semantic_phase(
            directory, report, {}, {"phase": 1}, "continuity", "panel", report["runners"][0], location, 1
        )


def test_legacy_continuity_panel_does_not_inherit_new_panel_artifact_selection(tmp_path):
    directory, report, location = continuity_panel(tmp_path)
    row = report["results"][0]
    row.pop("fixture_family")
    row.pop("artifact_kind")
    row.pop("origin")
    row["fixture_version"] = 6
    row["artifact_revision"] = report["artifacts"]["current"]["revision"]
    write(directory / "matrix.json", report)
    write(location / "result.json", row)
    case = json.loads((location / "case.json").read_text())
    for key in ("fixture_family", "artifact_kind", "artifact_revision", "origin"):
        case.pop(key, None)
    case["fixture_version"] = 6
    write(location / "case.json", case)
    original, _ = merge.bind_semantic_phase(
        directory, report, {}, {"phase": 1}, "continuity", "panel", report["runners"][0], location, 1
    )
    assert original["artifact_revision"] == report["artifacts"]["current"]["revision"]
