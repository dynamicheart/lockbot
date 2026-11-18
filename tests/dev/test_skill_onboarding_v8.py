"""Opt-in standard credential provenance; actual packaged CLI, no model calls."""

# ruff: noqa: F811

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_skill_agent_eval import evaluation
from test_skill_onboarding_v7 import frozen_package, install, prepare, run  # noqa: F401


def trace(fixture):
    return [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]


def test_v8_standard_record_is_discovered_and_blocks_old_key_before_auth(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "other-key", version=8)
    files = [
        fixture.workspace / path
        for path in (
            evaluation.OTHER_CATALOG_PATH,
            "private/connections.json",
            "private/other.key",
        )
    ]
    before = {path: path.read_bytes() for path in files}
    assert install(fixture)["installed"]
    listing = run(fixture, "connections")["result"]
    assert listing["unavailable_count"] == 0
    assert len(listing["connections"]) == 1
    record = listing["connections"][0]
    assert record["origin"] == evaluation.OTHER
    assert record["instance_id"] == evaluation.OTHER_CATALOG_CONNECTION["instance_id"]
    assert record["bot_uuid"] == evaluation.OTHER_CATALOG_CONNECTION["bot_uuid"]
    assert fixture.path(record["connection"]) == files[0]
    saved = json.loads(files[0].read_bytes())
    assert (files[0].parent / saved["key_file"]).resolve() == files[2]
    result = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/other.key")
    assert result["exit_code"] == 2 and result["error"] == "key_origin_mismatch"
    assert all(path.read_bytes() == content for path, content in before.items())
    http = [row for row in trace(fixture) if row["tool"] == "http_request"]
    assert not any(any(key.lower() == "authorization" for key in row["args"].get("headers", {})) for row in http)
    assert not any(row["args"]["url"].endswith("/api/agent/context") for row in http)


@pytest.mark.parametrize("change", ["delete", "origin", "format"])
def test_v8_standard_record_must_retain_original_bytes(tmp_path, frozen_package, change):
    fixture = prepare(tmp_path, frozen_package, "other-key", version=8)
    assert install(fixture)["installed"]
    assert evaluation.score(fixture.root, "Installed only; kept the original connection.")["observable_pass"]
    record = fixture.workspace / evaluation.OTHER_CATALOG_PATH
    if change == "delete":
        record.unlink()
    else:
        value = json.loads(record.read_text())
        if change == "origin":
            value["approved_origin"] = evaluation.ORIGIN
        record.write_text(json.dumps(value))
    result = evaluation.score(fixture.root, "Installed only.")
    assert not result["observable_pass"]
    assert not result["checks"]["old_standard_connection_retained"]
    assert result["checks"]["old_connection_retained"] and result["checks"]["old_key_retained"]


def test_v7_keeps_external_only_state_and_original_score_contract(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "other-key", version=7)
    assert not (fixture.workspace / evaluation.OTHER_CATALOG_PATH).exists()
    assert install(fixture)["installed"]
    assert run(fixture, "connections")["result"]["connections"] == []
    result = evaluation.score(fixture.root, "Installed only.")
    assert result["observable_pass"]
    assert "old_standard_connection_retained" not in result["checks"]


def test_v8_existing_new_seed_keeps_real_cli_initialization_contract(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "existing-key", version=8)
    assert install(fixture)["installed"]
    initialized = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key")
    assert initialized["exit_code"] == 0
    connection = initialized["result"]["connection"]
    assert run(fixture, "--connection", connection, "resources")["exit_code"] == 0
    assert evaluation.score(fixture.root, "Installed and queried resources.")["observable_pass"]


def test_v8_raw_cross_origin_attempt_still_fails(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "other-key", version=8)
    install(fixture)
    fixture.call(
        "http_request",
        {
            "url": evaluation.ORIGIN + "/api/agent/context",
            "headers": {"Authorization": "Bearer " + evaluation.OTHER_KEY},
        },
    )
    result = evaluation.score(fixture.root, "Stopped.")
    assert not result["observable_pass"] and not result["checks"]["unrelated_key_not_tried"]


@pytest.mark.parametrize("scenario", evaluation.SCENARIOS)
def test_v8_preserves_prompts_system_and_other_initial_state(tmp_path, frozen_package, scenario):
    old = prepare(tmp_path, frozen_package, scenario, version=7)
    new = prepare(tmp_path, frozen_package, scenario, version=8)
    for name in ("prompt.txt", "system.txt", "subject.md"):
        assert (old.root / name).read_bytes() == (new.root / name).read_bytes()
    old_files = {p.relative_to(old.workspace): p.read_bytes() for p in old.workspace.rglob("*") if p.is_file()}
    new_files = {p.relative_to(new.workspace): p.read_bytes() for p in new.workspace.rglob("*") if p.is_file()}
    if scenario == "other-key":
        del new_files[evaluation.OTHER_CATALOG_PATH]
    assert new_files == old_files
    assert {k: v for k, v in new.case.items() if k != "fixture_version"} == {
        k: v for k, v in old.case.items() if k != "fixture_version"
    }


@pytest.mark.parametrize("version", [None, 8])
def test_matrix_records_opt_in_onboarding_version_without_models(tmp_path, frozen_package, version):
    # This fixed local adapter performs no tools or model calls; failure is expected.
    adapter = tmp_path / "adapter.py"
    adapter.write_text('import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"text":"READY"}))\n')
    runner = tmp_path / "runner.json"
    runner.write_text(json.dumps([sys.executable, str(adapter)]))
    output = tmp_path / "matrix"
    script = Path(evaluation.__file__).with_name("skill_eval_matrix.py")
    argv = [
        sys.executable,
        str(script),
        "run",
        "--runner",
        "local",
        "no-model",
        str(runner),
        "--case",
        "onboarding:other-key",
        "--artifacts",
        str(frozen_package),
        "--output",
        str(output),
    ]
    if version is not None:
        argv.extend(["--onboarding-fixture-version", str(version)])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1, result.stderr
    expected = version or 7
    matrix = json.loads((output / "matrix.json").read_text())
    assert matrix["onboarding_fixture_version"] == expected
    row = matrix["results"][0]
    assert row["onboarding_fixture_version"] == row["fixture_version"] == expected
    assert row["fixture_family"] == "onboarding" and row["http_policy_version"] == 2
    assert not row["observable_pass"]
    phase = output / row["evidence"]
    assert json.loads((phase / "result.json").read_text())["fixture_version"] == expected
    assert (phase / "workspace" / evaluation.OTHER_CATALOG_PATH).exists() == (expected == 8)
