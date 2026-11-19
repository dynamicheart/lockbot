"""Versioned suite identities preserve actual prepared packaged-CLI evidence."""

import copy
import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools/trace-viewer"), str(ROOT / "scripts/dev")]

import publish_dataset as publisher  # noqa: E402
import skill_cli_release_eval as release  # noqa: E402
import skill_eval_matrix as evaluation  # noqa: E402
import skill_resource_cli_eval as resources  # noqa: E402


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n")


def prepared_run(tmp_path, suite="resource-cli", scenario="find-p800"):
    """Prepare and execute the shipped fixture; never invent its case.json schema."""
    artifact = tmp_path / "artifact"
    artifact.mkdir(mode=0o700)
    markdown, bundle, manifest = skill._skill_artifacts(evaluation.onboarding.ORIGIN)
    (artifact / "SKILL.md").write_bytes(markdown)
    (artifact / "lockbot-agent.zip").write_bytes(bundle)
    write(artifact / "manifest.json", manifest)
    source = tmp_path / "run"
    source.mkdir(mode=0o700)
    frozen = evaluation.freeze(artifact, source / "artifacts/current", evaluation.onboarding.ORIGIN)
    location = source / suite / scenario / "001/phase-1"
    evaluation.prepare(location, suite, scenario, source / "artifacts")
    if suite == "resource-cli":
        fixture = resources.Fixture(location)
        fixture.call("read_lockbot_skill", {"offset": 0})
        output = fixture.call(
            "run_lockbot", {"argv": ["resource", "list", "--model", "P800", "--limit", "1", "--json", "node,hardware"]}
        )
        assert output["result"]["rows"]
        result = resources.score(location, "Local fixture execution only.")
    else:
        fixture = release.ReleaseFixture(location)
        fixture.call("read_lockbot_skill", {"offset": 0})
        output = fixture.call("run_lockbot", {"action": "alias", "node": "node-19", "alias": "跑测试"})
        assert output["result"]["command_sent"] is True
        result = release.score(location, "Local fixture execution only.")
    case = json.loads((location / "case.json").read_text())
    runner = {"client": "local-fixture", "model": "fixture-only"}
    result.update(
        runner,
        suite=suite,
        scenario=scenario,
        phase=1,
        status="completed",
        fixture_version=case["fixture_version"],
        evidence=location.relative_to(source).as_posix(),
        skill_sha256=case["skill_sha256"],
        artifact_revision=frozen["revision"],
    )
    if suite == "cli-release":
        result.update(fixture_family=suite, fixture_version=case["release_fixture_version"])
    plan = {
        "simulation_only": True,
        "artifacts": {"current": frozen},
        "cases": [f"{suite}:{scenario}"],
        "runners": [runner],
    }
    write(source / "run.json", plan)
    write(source / "matrix.json", {**plan, "results": [result]})
    write(location / "result.json", result)
    write(location / "response.json", {"text": "Local fixture execution only."})
    (source / "progress.jsonl").write_text("")
    return source, result, location


def tree_hashes(source):
    return {
        path.relative_to(source).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.is_file()
    }


def test_release_wrapper_exports_executed_alias_case_without_changing_source(tmp_path):
    source, result, location = prepared_run(tmp_path, "cli-release", "alias-set")
    meta = json.loads((location / "case.json").read_text())
    assert meta["scenario"] == "intent-lock-tail"
    assert meta["release_scenario"] == "alias-set"
    before = tree_hashes(source)
    report = publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    dataset = tmp_path / "board/datasets" / report["dataset"]["dataset_id"]
    raw = json.loads((dataset / "raw.json").read_text())
    assert report["source_io_audit"]["prompts"] == report["source_io_audit"]["finals"] == 1
    assert report["source_io_audit"]["fixture_tools"] > 1
    trial = raw["cases"]["cli-release-alias-set"][0]
    assert trial["state"] == ("pass" if result["observable_pass"] else "fail")
    assert {"role": "user", "text": (location / "prompt.txt").read_text()} in trial["sessions"][0]
    assert {"role": "assistant", "text": "Local fixture execution only."} in trial["sessions"][0]
    assert tree_hashes(source) == before
    assert publisher.verify_dataset(tmp_path / "board/datasets", report["dataset"]["dataset_id"])


@pytest.mark.parametrize(
    "suite,version", [("resource-cli", 1), ("resource-cli", 2), ("cli-release", 3), ("cli-release", 4)]
)
def test_known_wrapper_versions_export_prepared_evidence(tmp_path, monkeypatch, suite, version):
    # These wrapper revisions retain their scenario fields and underlying fixture map.
    if suite == "resource-cli":
        monkeypatch.setattr(resources, "VERSION", version)
        scenario, field = "find-p800", "resource_cli_version"
    else:
        monkeypatch.setattr(release, "FIXTURE_VERSION", version)
        scenario, field = "alias-set", "release_fixture_version"
    source, _, location = prepared_run(tmp_path, suite, scenario)
    assert json.loads((location / "case.json").read_text())[field] == version
    report = publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    assert report["source_io_audit"]["prompts"] == report["source_io_audit"]["finals"] == 1


@pytest.mark.parametrize("suite,scenario", [("resource-cli", "find-p800"), ("cli-release", "alias-set")])
@pytest.mark.parametrize("tamper", ["family", "version", "boolean_version", "scenario", "underlying", "subject"])
def test_wrapper_identity_or_subject_tampering_never_publishes(tmp_path, suite, scenario, tamper):
    source, _, location = prepared_run(tmp_path, suite, scenario)
    case_path = location / "case.json"
    meta = json.loads(case_path.read_text())
    field = "resource_cli_scenario" if suite == "resource-cli" else "release_scenario"
    version = "resource_cli_version" if suite == "resource-cli" else "release_fixture_version"
    if tamper == "family":
        meta["fixture_family"] = "other-family"
    elif tamper == "version":
        meta[version] = 999
    elif tamper == "boolean_version":
        meta[version] = True
    elif tamper == "scenario":
        meta[field] = "list-resources" if suite == "resource-cli" else "alias-clear"
    elif tamper == "underlying":
        meta["scenario"] = "unrelated-fixture"
    else:
        (location / "subject.md").write_text("Changed instructions")
    write(case_path, meta)
    with pytest.raises(ValueError, match="omitted or changed"):
        publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    assert not (tmp_path / "board/datasets/index.json").exists()


@pytest.mark.parametrize("kind", ["prompt", "final", "tools"])
def test_raw_io_audit_remains_exact_for_wrapper_suites(tmp_path, kind):
    source, _, _ = prepared_run(tmp_path)
    report = publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    dataset = tmp_path / "board/datasets" / report["dataset"]["dataset_id"]
    raw = json.loads((dataset / "raw.json").read_text())
    altered = copy.deepcopy(raw)
    session = altered["cases"]["resource-cli-find-p800"][0]["sessions"][0]
    role = {"prompt": "user", "final": "assistant", "tools": "tools"}[kind]
    entry = next(row for row in session if row["role"] == role)
    if kind == "tools":
        entry["evidence"][0]["output"] = {"changed": True}
    else:
        entry["text"] = "Changed conversation"
    with pytest.raises(ValueError, match="omitted or changed"):
        publisher.audit_source_io(source, altered)
