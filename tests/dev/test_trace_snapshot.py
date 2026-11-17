import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

SCRIPT = Path(__file__).resolve().parents[2] / "tools/trace-viewer/publish_snapshot.py"


def test_snapshot_waits_for_complete_case_and_excludes_private_input(tmp_path):
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:reset-personal-key"],
        "runners": [{"client": "private-provider", "model": "private-model"}],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    rows = [
        {
            "client": "private-provider",
            "model": "private-model",
            "suite": "cli",
            "scenario": "reset-personal-key",
            "status": "completed",
            "observable_pass": True,
            "phase": phase,
            "model_turns": 3,
            "evidence": "/private/file",
            "response": "lbr_secretinputfromprivatetrace",
        }
        for phase in range(1, 4)
    ]
    progress = tmp_path / "progress.jsonl"
    progress.write_text("\n".join(map(json.dumps, rows)))
    output = tmp_path / "latest.json"
    argv = [sys.executable, str(SCRIPT), str(tmp_path), str(output)]
    subprocess.run(argv, check=True)
    assert not output.exists()

    with progress.open("a") as trace:
        trace.write("\n")
    subprocess.run(argv, check=True)
    raw = output.read_text()
    for forbidden in ("lbr_", "/private/file", "private-provider", "private-model", "response"):
        assert forbidden not in raw
    case = json.loads(raw)["cases"]["case-3"][0]
    assert case["state"] == "pass"
    assert case["sessions"] == []
    assert [stage["turns"] for stage in case["metrics"]["phases"]] == [3, 3, 3]


def test_follow_emits_each_finished_case_before_matrix_completion(tmp_path):
    models = ["glm-5.3", "kimi-k3"]
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:reset-personal-key"],
        "runners": [{"client": "fixture", "model": model} for model in models],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    progress = tmp_path / "progress.jsonl"
    callback = tmp_path / "transfer.py"
    delivered = tmp_path / "delivered.jsonl"
    callback.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "state = json.loads(Path(sys.argv[2]).read_text())\n"
        "with Path(sys.argv[1]).open('a') as output:\n"
        "    output.write(str(len(state['cases']['case-3'])) + '\\n')\n"
    )
    transport = tmp_path / "transfer.json"
    transport.write_text(json.dumps([sys.executable, str(callback), str(delivered)]))
    argv = [
        sys.executable,
        str(SCRIPT),
        str(tmp_path),
        str(tmp_path / "latest.json"),
        "--follow",
        "--on-update",
        str(transport),
    ]
    with subprocess.Popen(argv) as publisher:
        try:
            for number, model in enumerate(models, 1):
                with progress.open("a") as log:
                    for phase in range(1, 4):
                        row = {
                            "client": "fixture",
                            "model": model,
                            "suite": "cli",
                            "scenario": "reset-personal-key",
                            "status": "completed",
                            "observable_pass": True,
                            "phase": phase,
                        }
                        log.write(json.dumps(row) + "\n")
                deadline = monotonic() + 8
                while monotonic() < deadline:
                    if delivered.exists() and len(delivered.read_text().splitlines()) == number:
                        break
                    sleep(0.05)
                else:
                    raise AssertionError("Publisher failed to deliver a finished case")
                assert not (tmp_path / "matrix.json").exists()
            (tmp_path / "matrix.json").write_text("{}")
            assert publisher.wait(timeout=8) == 0
        finally:
            if publisher.poll() is None:
                publisher.terminate()
    assert delivered.read_text().splitlines() == ["1", "2"]


def test_snapshot_groups_cli_cases_by_scenario_and_counts_only_behavior_runs(tmp_path):
    plan = {
        "simulation_only": True,
        "artifacts": {
            "current": {"package_version": "0123456789abcdef", "revision": "current-fixture"},
            "panel": {"package_version": "0123456789abcdef", "revision": "https-fixture"},
        },
        "cases": [
            "onboarding:install",
            "continuity:panel",
            "cli:panel",
            "cli:install-from-zip",
            "cli:recover",
            "cli:intent-claim",
        ],
        "runners": [
            {"client": "Official Codex", "model": "private-model"},
            {"client": "private-provider", "model": "glm-5.3"},
        ],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    cases = [
        ("continuity", "panel", 1),
        ("cli", "panel", 1),
        ("cli", "install-from-zip", 1),
        ("cli", "recover", 1),
        ("cli", "intent-claim", 1),
    ]
    rows = []
    for suite, scenario, phase in cases:
        rows.append(
            {
                **plan["runners"][0],
                "suite": suite,
                "scenario": scenario,
                "phase": phase,
                "artifact_revision": "https-fixture" if scenario == "panel" else "current-fixture",
                "status": "completed",
                "observable_pass": scenario != "intent-claim",
                "checks": {"exactly_one_correct_cli_write": False, "private-detail": False},
            }
        )
    rows.extend(
        [
            {**rows[3], "artifact_revision": "old-fixture", "observable_pass": False},
            {**rows[2], **plan["runners"][1], "status": "blocked_by_model_preflight", "preflight_status": "timeout"},
        ]
    )
    progress = tmp_path / "progress.jsonl"
    progress.write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = tmp_path / "latest.json"
    argv = [sys.executable, str(SCRIPT), str(tmp_path), str(output), "--include-dialogue"]
    subprocess.run(argv, check=True)
    snapshot = json.loads(output.read_text())
    assert "panel" not in snapshot["cases"]
    assert "install" not in snapshot["cases"]
    assert snapshot["groups"] == {
        "install-from-zip": "安装与绑定",
        "recover": "查询与工作台",
        "intent-claim": "资源操作",
        "model-preflight": "运行环境",
    }
    assert [run["state"] for run in snapshot["cases"]["install-from-zip"]] == ["pass"]
    assert [run["state"] for run in snapshot["cases"]["model-preflight"]] == ["blocked"]
    operation = snapshot["cases"]["intent-claim"][0]
    assert operation["model"] == "官方 Codex"
    assert operation["state"] == "fail"
    assert "通过 CLI 准确提交一次操作" in operation["note"]
    assert "private-detail" not in output.read_text()

    panel = {
        **rows[1],
        "phase": 2,
        "model_turns": 3,
        "duration_seconds": 42.2,
        "simulated_user_actions": ["browser_consent", "private-action"],
    }
    with progress.open("a") as trace:
        trace.write(json.dumps(panel) + "\n")
    (tmp_path / "preflight.json").write_text(
        json.dumps([{**plan["runners"][0], "status": "ready"}, {**plan["runners"][1], "status": "timeout"}]) + "\n"
    )
    subprocess.run(argv, check=True)
    snapshot = json.loads(output.read_text())
    assert snapshot["groups"]["panel"] == "查询与工作台"
    assert [run["state"] for run in snapshot["cases"]["model-preflight"]] == ["pass", "blocked"]
    panel_run = snapshot["cases"]["panel"][0]
    assert panel_run["state"] == "pass"
    assert panel_run["sessions"] == []
    assert panel_run["metrics"]["phases"] == [
        {"phase": 1},
        {"phase": 2, "turns": 3, "duration_seconds": 42.2, "simulated_user_actions": 1},
    ]
