"""Exercise matrix scheduling with synthetic adapters and no model service."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/skill_eval_matrix.py"
ORIGIN = "http://192.0.2.10:8155"

ADAPTER = """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
root = Path.cwd()
model = request["model"]
if root.parent.name == "preflight":
    assert request["max_turns"] == 2 and request["output_tokens"] == 128
    assert json.loads(Path(request["mcp_config"]).read_text()) == {"mcpServers": {}}
    if model == "preflight-timeout":
        time.sleep(30)
    print(json.dumps({"text": 42 if model == "preflight-invalid" else "READY"}))
    raise SystemExit
case = json.loads((root / "case.json").read_text())
if model == "case-timeout":
    time.sleep(30)
if model == "case-invalid":
    print(json.dumps({"text": 42}))
    raise SystemExit
if model == "bad":
    # A deliberately failing answer with no actions.
    Path(sys.argv[1], "beta-started").touch()
    print(json.dumps({"text": "完成"}))
    raise SystemExit

config = json.loads(Path(request["mcp_config"]).read_text())["mcpServers"]["fixture"]
phase = case["phase"]
assert root.name == "phase-" + str(phase)
assert not (root / "response.json").exists()
assert request["max_turns"] == 28 and request["output_tokens"] == 1024
if phase == 1:
    # This cannot be satisfied by a serial scheduler.
    for _ in range(100):
        if Path(sys.argv[1], "beta-started").exists():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("other adapter did not start")
    (root / "conversation-memory").touch()
else:
    assert not (root / "conversation-memory").exists()
    assert not (root / "trace.jsonl").exists()
    if case["scenario"] != "reset-personal-key" or phase >= 3:
        assert (root / "workspace/private/binding/enrollment.json").exists()

with subprocess.Popen(
    [config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
) as tools:
    def call(name, **arguments):
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        tools.stdin.write(json.dumps(payload) + "\\n")
        tools.stdin.flush()
        reply = json.loads(tools.stdout.readline())
        return json.loads(reply["result"]["content"][0]["text"])

    logical_phase = phase - 1 if case["scenario"] == "reset-personal-key" else phase
    if logical_phase == 0:
        answer = "重置后其他 Agent 会失效，需要重新配置。确认重置吗？"
    elif logical_phase == 1:
        if not call("run_lockbot", action="local-state")["result"]["installed"]:
            assert call("install_lockbot", url="http://192.0.2.10:8155/api/agent-skill/lockbot-agent.zip")["installed"]
        assert call("run_lockbot", action="discover")["result"]
        if case["scenario"] == "reset-personal-key":
            result = call("run_lockbot", action="key-challenge", purpose="reset", authorized=True)["result"]
            answer = result["user_instruction_zh"]
        else:
            result = call("run_lockbot", action="enroll", group="验证群", authorized=True)["result"]
            answer = "在验证群发送 " + result["bind_command"]
    elif logical_phase == 2:
        pending = call("run_lockbot", action="connections")["result"]["pending_enrollments"]
        finished = call("run_lockbot", action="finish-enrollment", enrollment_dir=pending[0]["enrollment_dir"])
        assert finished["result"]["connection_created"]
        assert call("run_lockbot", action="resources")["result"]["resources"]
        answer = "node-a"
    else:
        (record,) = call("run_lockbot", action="connections")["result"]["connections"]
        assert call("run_lockbot", action="resources", connection=record["connection"])["result"]["resources"]
        answer = "node-a"
    tools.stdin.close()
response = {"text": answer, "pid": os.getpid(), "turns": 3}
if "conversation_file" in request:
    history = json.loads(Path(request["conversation_file"]).read_text())
    assert history[-1]["role"] == "assistant"
    response["conversation_sha256"] = request["conversation_sha256"]
print(json.dumps(response))
"""


def run_matrix(tmp_path, monkeypatch, scenario, *models, timeout=15, adapter_source=ADAPTER):
    monkeypatch.setenv("PLATFORM_URL", ORIGIN)
    artifacts = tmp_path / "input"
    artifacts.mkdir()
    manifest = skill.skill_artifacts()[2]
    (artifacts / "manifest.json").write_text(json.dumps(manifest))
    (artifacts / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    adapter = tmp_path / "adapter.py"
    adapter.write_text(adapter_source)
    runner = tmp_path / "runner.json"
    runner.write_text(json.dumps([sys.executable, str(adapter), str(tmp_path)]))
    output = tmp_path / "evidence"
    command = [
        sys.executable,
        str(SCRIPT),
        "run",
        "--case",
        scenario,
        "--artifacts",
        str(artifacts),
        "--output",
        str(output),
        "--workers",
        "2",
        "--preflight-timeout",
        "1",
        "--timeout",
        str(timeout),
        "--max-turns",
        "28",
        "--output-tokens",
        "1024",
    ]
    for client, model in models:
        command.extend(("--runner", client, model, str(runner)))
    run = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "python"},
    )
    assert run.returncode == 1, (run.stdout, run.stderr)
    return output, json.loads((output / "matrix.json").read_text())


def test_concurrent_matrix_keeps_next_session_state_and_report_order(tmp_path, monkeypatch):
    output, report = run_matrix(
        tmp_path, monkeypatch, "cli:approved-enrollment", ("Client A", "ok"), ("Client B", "bad")
    )
    rows = report["results"]
    assert [(row["client"], row["phase"], row["status"], row.get("observable_pass")) for row in rows] == [
        ("Client A", 1, "completed", True),
        ("Client A", 2, "completed", True),
        ("Client B", 1, "completed", False),
        ("Client B", 2, "blocked_by_phase_1", None),
    ]
    assert report["limits"] == {"max_turns": 28, "output_tokens": 1024, "timeout": 15}
    assert report["simulation_only"]
    assert [row["status"] for row in report["preflight"]] == ["ready", "ready"]
    assert [row.get("model_turns") for row in rows] == [3, 3, None, None]
    assert [row["simulated_user_actions"] for row in rows] == [[], ["chat_binding_confirmation"], [], []]
    assert json.loads((output / "progress.jsonl").read_text().splitlines()[0])["client"] == "Client B"
    first, second, failed = [output / row["evidence"] for row in rows[:3]]
    assert (first / "workspace/private/binding/enrollment.json").exists()
    assert not (failed / "workspace/private/binding").exists()
    assert not (failed.parent / "phase-2").exists()
    assert (
        json.loads((first / "response.json").read_text())["pid"]
        != json.loads((second / "response.json").read_text())["pid"]
    )


def test_reset_matrix_requires_warning_acknowledgment_before_group_confirmation(tmp_path, monkeypatch):
    output, report = run_matrix(
        tmp_path, monkeypatch, "cli:reset-personal-key", ("Client A", "ok"), ("Client B", "bad")
    )
    passing, failing = report["results"][:4], report["results"][4:]
    assert [r["phase"] for r in passing] == [1, 2, 3, 4]
    assert all(r["observable_pass"] for r in passing)
    assert [r["simulated_user_actions"] for r in passing] == [
        [],
        ["reset_impact_acknowledgment"],
        ["chat_binding_confirmation"],
        [],
    ]
    assert len({json.loads((output / r["evidence"] / "response.json").read_text())["pid"] for r in passing}) == 4
    assert [r["status"] for r in failing] == [
        "completed",
        "blocked_by_phase_1",
        "blocked_by_phase_2",
        "blocked_by_phase_3",
    ]


def test_matrix_reports_adapter_timeout_and_bad_protocol_separately(tmp_path, monkeypatch):
    _, report = run_matrix(
        tmp_path, monkeypatch, "onboarding:body", ("Client A", "case-timeout"), ("Client B", "case-invalid"), timeout=1
    )
    assert [row["status"] for row in report["preflight"]] == ["ready", "ready"]
    assert [row["status"] for row in report["results"]] == ["timeout", "runner_protocol_error"]
    assert all("observable_pass" not in row for row in report["results"])


def test_matrix_skips_cases_for_a_blocked_preflight(tmp_path, monkeypatch):
    output, report = run_matrix(
        tmp_path,
        monkeypatch,
        "cli:approved-enrollment",
        ("Client A", "preflight-timeout"),
        ("Client B", "preflight-invalid"),
    )
    assert [row["status"] for row in report["preflight"]] == ["timeout", "runner_protocol_error"]
    rows = report["results"]
    assert [row["status"] for row in rows] == ["blocked_by_model_preflight"] * 2
    assert [row["preflight_status"] for row in rows] == ["timeout", "runner_protocol_error"]
    assert all("observable_pass" not in row for row in rows)
    assert not (output / "cli").exists()


def test_operations_answer_without_tool_calls_fails_observable_checks(tmp_path, monkeypatch):
    _, report = run_matrix(tmp_path, monkeypatch, "operations:alias", ("Client A", "bad"))
    (result,) = report["results"]
    assert result["status"] == "completed"
    assert not result["observable_pass"]
    assert not result["checks"]["exact_alias_commands"]


CONTEXT_ADAPTER = """
import json, sys
from pathlib import Path
request = json.load(sys.stdin)
root = Path.cwd()
if root.parent.name == "preflight":
    print(json.dumps({"text": "READY"}))
    raise SystemExit
if request["model"] == "bad":
    print(json.dumps({"text": "完成"}))
    raise SystemExit
config = json.loads(Path(request["mcp_config"]).read_text())["mcpServers"]["fixture"]
sys.path.insert(0, str(Path(config["args"][0]).parent))
import skill_cli_eval as cli
fixture = cli.Fixture(root)
scenario, phase = fixture.case["scenario"], fixture.case["phase"]
if scenario in cli.intent.INSTALL_FOLLOWUPS and phase == 1:
    assert fixture.call("install_lockbot", {"url": fixture.origin + "/api/agent-skill"})["installed"]
else:
    for group in cli.intent.GROUP_IDS:
        assert fixture.call("run_lockbot", {"action": "resources", "group": group})["result"]
expected = cli.intent.task_expectation(scenario, phase)
if expected:
    _, command, operation = expected
    args = {"action": "command", "group": cli.intent.MACHINES, "operation": operation,
            "nodes": [command.split()[1]], "whole_node": True}
    if operation != "release":
        args["duration"] = command.split()[-1]
    assert fixture.call("run_lockbot", args)["result"]["verified"]
    answer = command.split()[1] + " 操作完成。"
else:
    answer = "已安装。" if scenario in cli.intent.INSTALL_FOLLOWUPS and phase == 1 else "node-23 是空闲的 P800 整机。"
response = {"text": answer}
if phase == 2:
    history = json.loads(Path(request["conversation_file"]).read_text())
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"] in ("node-19 操作完成。", "node-23 是空闲的 P800 整机。", "已安装。")
    assert Path(request["prompt_file"]).read_text() == cli.intent.CONVERSATIONAL_PROMPTS[scenario][1]
    response["conversation_sha256"] = request["conversation_sha256"]
print(json.dumps(response))
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "intent-context-renew",
        "intent-context-release",
        "intent-context-lock-day",
        "intent-install-then-query",
        "intent-install-then-lock",
        "intent-install-then-how-use",
    ],
)
def test_matrix_carries_observed_turns_to_contextual_followup(tmp_path, monkeypatch, scenario):
    output, report = run_matrix(
        tmp_path,
        monkeypatch,
        "cli:" + scenario,
        ("Synthetic A", "ok"),
        ("Synthetic B", "bad"),
        adapter_source=CONTEXT_ADAPTER,
    )
    passing = [row for row in report["results"] if row["model"] == "ok"]
    assert [row["phase"] for row in passing] == [1, 2]
    assert all(row["observable_pass"] for row in passing), passing
    assert all(row["simulated_user_actions"] == [] for row in passing)
    followup = output / passing[1]["evidence"]
    history = json.loads((followup / "conversation.json").read_text())
    original = output / passing[0]["evidence"]
    assert history[1]["content"] == json.loads((original / "response.json").read_text())["text"]
    assert json.loads((followup / "response.json").read_text())["conversation_sha256"]
