"""Telemetry v21 corrects read ordering without rewriting historical grading."""

import importlib
import json
import sys
from urllib.parse import urlsplit

import pytest
from dev.test_skill_cli_eval import evaluation, install, prepare, run
from dev.test_skill_cli_v20 import events, versioned

matrix = importlib.import_module("skill_eval_matrix")
FINAL = "node-a 的卡利用率为 42%，样本约 8 秒前采集。"


@pytest.mark.parametrize("requested", [18, 19, 20, 21])
def test_telemetry_protocol_is_explicit_and_preserves_prompts_and_tools(tmp_path, monkeypatch, requested):
    original, current = versioned(tmp_path, monkeypatch, "telemetry", requested)
    assert original.case["fixture_version"] == 17
    assert current.case["fixture_version"] == (21 if requested == 21 else 17)
    for name in ("prompt.txt", "system.txt"):
        assert (current.root / name).read_bytes() == (original.root / name).read_bytes()
    assert evaluation.tools(current.root) == evaluation.tools(original.root)


@pytest.mark.parametrize("version", [17, 18, 19, 20, 21])
@pytest.mark.parametrize("ownership_first", [False, True])
def test_actual_packaged_telemetry_grading_changes_only_new_version_order(
    tmp_path, monkeypatch, version, ownership_first
):
    _, fixture = versioned(tmp_path, monkeypatch, "telemetry", 21)
    install(fixture)
    if ownership_first:
        assert run(fixture, "resources")["result"]["resources"][0]["holders"]
    readings = run(fixture, "metrics", nodes=["node-a"])["result"]["utilization"]
    assert readings[0]["cards"][0]["percent"] == 42
    if not ownership_first:
        assert run(fixture, "resources")["result"]["resources"][0]["holders"]
    fixture.case["fixture_version"] = version
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    before = {name: (fixture.root / name).read_bytes() for name in ("case.json", "trace.jsonl", "cli-http.jsonl")}
    grade = evaluation.score(fixture.root, FINAL)
    assert grade["checks"]["refreshed_context_and_read_holding"] is (ownership_first or version == 21)
    assert grade["observable_pass"] is (ownership_first or version == 21)
    assert all(value for key, value in grade["checks"].items() if key != "refreshed_context_and_read_holding")
    assert {name: (fixture.root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize(
    "fault,failed_check",
    [
        ("missing_ownership", "refreshed_context_and_read_holding"),
        ("failed_ownership", "refreshed_context_and_read_holding"),
        ("missing_refresh", "refreshed_context_and_read_holding"),
        ("selector_mismatch", "one_retry_with_same_selector"),
        ("wrong_grant", "credentials_scoped"),
        ("extra_retry", "one_retry_with_same_selector"),
        ("resource_write", "no_resource_mutation"),
        ("missing_sample_age", "reports_utilization_and_sample_age"),
    ],
)
def test_v21_preserves_ownership_scopes_refresh_retry_mutation_and_sample_guards(
    tmp_path, monkeypatch, fault, failed_check
):
    _, fixture = versioned(tmp_path, monkeypatch, "telemetry", 21)
    install(fixture)
    assert run(fixture, "metrics", nodes=["node-a"])["result"]
    assert run(fixture, "resources")["result"]["resources"]
    wire = events(fixture, "cli-http.jsonl")
    metrics = [index for index, row in enumerate(wire) if row["url"] == evaluation.continuity.METRICS_URL]
    first, second = metrics
    ownership = next(
        index
        for index, row in enumerate(wire)
        if urlsplit(row["url"]).path.endswith("/resources") and row.get("resource_bot") == 7
    )
    final = FINAL
    if fault == "missing_ownership":
        wire.pop(ownership)
    elif fault == "failed_ownership":
        wire[ownership]["status"] = 403
    elif fault == "missing_refresh":
        wire = [
            row
            for index, row in enumerate(wire)
            if not (first < index < second and row["url"].endswith("/api/agent/context"))
        ]
    elif fault == "selector_mismatch":
        wire[second]["selector"] += "changed"
    elif fault == "wrong_grant":
        wire[second]["metrics_grant_used"] = False
    elif fault == "extra_retry":
        wire.append(dict(wire[second]))
    elif fault == "resource_write":
        wire.append(
            {
                "url": evaluation.base.ORIGIN + "/api/bots/7/commands",
                "method": "POST",
                "status": 200,
                "authorization_present": True,
            }
        )
    else:
        final = "node-a 的卡利用率为 42%。"
    (fixture.root / "cli-http.jsonl").write_text("".join(json.dumps(row) + "\n" for row in wire))
    grade = evaluation.score(fixture.root, final)
    assert grade["checks"][failed_check] is False
    assert not grade["observable_pass"]


@pytest.mark.parametrize("scenario", ["intent-find-p800", evaluation.COLD_REQUEST])
def test_v21_reuses_v20_query_protocol_without_prompt_changes(tmp_path, monkeypatch, scenario):
    _, current = versioned(tmp_path, monkeypatch, scenario, 21)
    old_root = tmp_path / "fixture-20"
    evaluation.prepare(old_root, tmp_path / "artifact", scenario, fixture_version=20)
    assert current.case["fixture_version"] == 21
    assert evaluation.tools(current.root) == evaluation.tools(old_root)
    for name in ("prompt.txt", "system.txt"):
        assert (current.root / name).read_bytes() == (old_root / name).read_bytes()


@pytest.mark.parametrize("entrypoint", ["prepare", "matrix"])
def test_v21_is_available_only_by_explicit_cli_selection(tmp_path, monkeypatch, entrypoint):
    prepare(tmp_path, monkeypatch, "telemetry")
    destination = tmp_path / "explicit"
    if entrypoint == "prepare":
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fixture",
                "prepare",
                str(destination),
                "--artifact",
                str(tmp_path / "artifact"),
                "--scenario",
                "telemetry",
                "--fixture-version",
                "21",
            ],
        )
        evaluation.main()
        assert evaluation.Fixture(destination).case["fixture_version"] == 21
    else:
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
                "cli:telemetry",
                "--artifacts",
                str(tmp_path / "artifact"),
                "--output",
                str(destination),
                "--cli-fixture-version",
                "21",
            ],
        )
        monkeypatch.setattr(matrix, "freeze", lambda *args: {"revision": "revision"})
        observed = []
        monkeypatch.setattr(matrix, "run_matrix", lambda *args, **kwargs: observed.append(kwargs) or True)
        assert matrix.main() == 0
        assert observed == [{"cli_fixture_version": 21, "onboarding_fixture_version": 7}]
