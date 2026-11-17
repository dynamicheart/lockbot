import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/trace-viewer/publish_snapshot.py"


def test_link_only_publishes_available_synthetic_dialogue_without_raw_urls(tmp_path):
    scenario = "link-only"
    runner = {"client": "fixture", "model": "deepseek-v4.1-flash"}
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": [f"cli:{scenario}"],
        "runners": [runner],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    location = f"cli/{scenario}/001/phase-1"
    folder = tmp_path / location
    folder.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    row = {"scenario": scenario, "phase": 1, "skill_sha256": digest}
    (folder / "subject.md").write_text(subject)
    (folder / "case.json").write_text(json.dumps(row))
    url = "https://private.example.test/api/agent-skill"
    (folder / "prompt.txt").write_text(f"请看这个链接 {url}")
    (folder / "response.json").write_text(json.dumps({"text": "这是 Skill 下载入口。"}, ensure_ascii=False))
    actual_args = ["/fixture/lockbot_cli.py", "connections"]
    tool_events = [
        {"tool": "inspect_lockbot", "args": {"url": url}, "result": {"url": url}},
        {"tool": "run_lockbot", "args": {"action": "connections"}, "result": {}, "argv": actual_args},
    ]
    (folder / "trace.jsonl").write_text("".join(json.dumps(event) + "\n" for event in tool_events))
    (tmp_path / "progress.jsonl").write_text(
        json.dumps(
            {**row, **runner, "suite": "cli", "evidence": location, "status": "completed", "observable_pass": True}
        )
        + "\n"
    )
    output = tmp_path / "latest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(output), "--include-dialogue"], check=True)
    raw = output.read_text()
    (session,) = json.loads(raw)["cases"][scenario][0]["sessions"]
    assert [event["role"] for event in session] == ["user", "tools", "assistant"]
    assert session[1]["steps"][0][0] == "inspect_lockbot"
    assert session[2]["text"] == "这是 Skill 下载入口。"
    assert url not in raw
    assert actual_args[0] not in raw

    original = tmp_path / "raw.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(original), "--raw-local"], check=True)
    private = json.loads(original.read_text())
    assert original.stat().st_mode & 0o777 == 0o600
    assert private["visibility"] == "raw"
    (raw_session,) = private["cases"][scenario][0]["sessions"]
    assert raw_session[0]["text"] == f"请看这个链接 {url}"
    assert raw_session[1]["evidence"][0]["input"]["url"] == url
    assert raw_session[1]["evidence"][1]["argv"] == actual_args


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


def test_snapshot_projects_non_cli_and_planned_consultations(tmp_path):
    runner = {"client": "fixture", "model": "gpt-6.1-sol"}
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["onboarding:install", "consultation:default-duration", "cli:link-only"],
        "runners": [runner],
        "prompts": {"cli:link-only": "http://192.0.2.10:8155/api/agent-skill"},
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    rows = []
    for suite, scenario in [("onboarding", "install"), ("consultation", "default-duration")]:
        location = f"{suite}/{scenario}/001/phase-1"
        folder = tmp_path / location
        folder.mkdir(parents=True)
        subject = "Synthetic Skill"
        digest = sha256(subject.encode()).hexdigest()
        (folder / "subject.md").write_text(subject)
        (folder / "case.json").write_text(json.dumps({"scenario": scenario, "skill_sha256": digest}))
        (folder / "prompt.txt").write_text("没写时长多久" if suite == "consultation" else "装一下")
        (folder / "response.json").write_text(json.dumps({"text": "已根据说明回答"}))
        rows.append(
            {
                **runner,
                "suite": suite,
                "scenario": scenario,
                "phase": 1,
                "evidence": location,
                "skill_sha256": digest,
                "status": "completed",
                "observable_pass": True,
                "checks": {"answer_present": True},
                "manual_review_required": ["Review explanation"] if suite == "consultation" else [],
            }
        )
    (tmp_path / "progress.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = tmp_path / "latest.json"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(tmp_path),
            str(output),
            "--include-dialogue",
            "--include-planned",
        ],
        check=True,
    )
    snapshot = json.loads(output.read_text())
    assert set(snapshot["cases"]) == {"onboarding-install", "consultation-default-duration", "link-only"}
    consultation = snapshot["cases"]["consultation-default-duration"][0]
    assert consultation["state"] == "pending"
    assert consultation["sessions"][0][0]["text"] == "没写时长多久"
    assert snapshot["cases"]["onboarding-install"][0]["sessions"]
    pending = snapshot["cases"]["link-only"][0]
    assert pending["state"] == "pending"
    assert pending["sessions"][0][0]["text"] == "https://lockbot.example.test/api/agent-skill"


def test_packaged_cli_regression_preserves_version_and_actual_steps(tmp_path):
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "simulation_only": True,
                "artifacts": {"current": {"package_version": "0123456789abcdef"}},
                "cases": [],
                "runners": [],
            }
        )
    )
    (tmp_path / "progress.jsonl").write_text("")
    actions = ["init", "resources", "resources", "command", "resources"]
    tools = [
        {
            "action": action,
            "argv": [action, "--connection", "/private/fixture/connection.json"],
            "exit_code": 0,
            "output": {"result": {"verified": True}},
            "http": [
                {
                    "method": "POST" if action == "command" else "GET",
                    "path": "/api/agent/commands" if action == "command" else "/api/agent/resources",
                    "status": 200,
                }
            ],
        }
        for action in actions
    ]
    evidence = {
        "case": "packaged-cli-query-immediate-lock-verification",
        "simulation_only": True,
        "result": "passed",
        "transport": "FastAPI TestClient",
        "verified_single_holder": True,
        "verified_duration_seconds": 3600,
        "package_version": "fedcba9876543210",
        "tools": tools,
        "http": [event for item in tools for event in item["http"]],
    }
    regression = tmp_path / "regression.json"
    regression.write_text(json.dumps(evidence))
    output = tmp_path / "latest.json"
    command = [sys.executable, str(SCRIPT), str(tmp_path), str(output), "--regression", str(regression)]
    subprocess.run(command, check=True)
    data = json.loads(output.read_text())
    trial = data["cases"]["regression-immediate-query-lock"][0]
    assert trial["package_version"] == "fedcba9876543210"
    assert trial["model"] == "pytest 自动回归"
    assert trial["state"] == "pass"
    assert trial["metrics"]["phases"][0]["http_requests"] == 5
    assert len(trial["sessions"][0][1]["steps"]) == 5
    assert "/private/fixture" not in output.read_text()
    raw = tmp_path / "raw.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), str(raw), "--regression", str(regression), "--raw-local"],
        check=True,
    )
    raw_trial = json.loads(raw.read_text())["cases"]["regression-immediate-query-lock"][0]
    assert raw_trial["sessions"][0][1]["evidence"][0]["output"]["response"] == tools[0]["output"]

    tools[-1]["http"][0]["status"] = 429
    regression.write_text(json.dumps(evidence))
    rejected = tmp_path / "rejected.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), str(rejected), "--regression", str(regression)],
        capture_output=True,
    )
    assert result.returncode != 0
    assert not rejected.exists()


@pytest.mark.parametrize("repetitions", [30_000, 1_600_000])
def test_raw_snapshot_preserves_large_tool_output_and_final_response(tmp_path, repetitions):
    runner = {"client": "fixture", "model": "gpt-6.1-sol"}
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:link-only"],
        "runners": [runner],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    location = "cli/link-only/001/phase-1"
    folder = tmp_path / location
    folder.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    row = {"scenario": "link-only", "phase": 1, "skill_sha256": digest}
    (folder / "subject.md").write_text(subject)
    (folder / "case.json").write_text(json.dumps(row))
    (folder / "prompt.txt").write_text("看看这个链接")
    payload = "large synthetic output " * repetitions
    (folder / "trace.jsonl").write_text(
        json.dumps(
            {
                "tool": "inspect_lockbot",
                "args": {},
                "result": {"content": payload},
            }
        )
        + "\n"
    )
    (folder / "response.json").write_text(json.dumps({"text": "这是安装入口"}))
    (tmp_path / "progress.jsonl").write_text(
        json.dumps(
            {
                **row,
                **runner,
                "suite": "cli",
                "evidence": location,
                "status": "completed",
                "observable_pass": True,
            }
        )
        + "\n"
    )
    output = tmp_path / "raw.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(output), "--raw-local"], check=True)
    session = json.loads(output.read_text())["cases"]["link-only"][0]["sessions"][0]
    assert session[0]["text"] == "看看这个链接"
    if repetitions == 30_000:
        assert session[1]["evidence"][0]["output"]["content"] == payload
    else:
        assert session[1]["role"] == "missing"
        assert "32 MB" in session[1]["text"]
    assert session[-1]["text"] == "这是安装入口"


@pytest.mark.parametrize(("fixture_version", "count"), [(10, 3), (13, 4)])
def test_reset_snapshot_preserves_historical_and_new_consent_phase_counts(tmp_path, fixture_version, count):
    runner = {"client": "fixture", "model": "gpt-6.1-sol"}
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:reset-personal-key"],
        "runners": [runner],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    rows = [
        {
            **runner,
            "suite": "cli",
            "scenario": "reset-personal-key",
            "status": "completed",
            "observable_pass": True,
            "phase": phase,
            "fixture_version": fixture_version,
        }
        for phase in range(1, count + 1)
    ]
    (tmp_path / "progress.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = tmp_path / "latest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(output)], check=True)
    trial = json.loads(output.read_text())["cases"]["case-3"][0]
    assert trial["state"] == "pass"
    assert len(trial["metrics"]["phases"]) == count


def test_own_resource_answers_remain_pending_semantic_review(tmp_path):
    runner = {"client": "fixture", "model": "gpt-6.1-sol"}
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": "0123456789abcdef"}},
        "cases": ["cli:intent-my-locks"],
        "runners": [runner],
    }
    (tmp_path / "run.json").write_text(json.dumps(plan))
    row = {
        **runner,
        "suite": "cli",
        "scenario": "intent-my-locks",
        "status": "completed",
        "observable_pass": True,
        "phase": 1,
        "manual_review_required": ["Check exact ownership and roles."],
    }
    (tmp_path / "progress.jsonl").write_text(json.dumps(row) + "\n")
    output = tmp_path / "latest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(output)], check=True)
    assert json.loads(output.read_text())["cases"]["intent-my-locks"][0]["state"] == "pending"
