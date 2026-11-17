"""Native-client subprocess contract; no model or external service is called."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev/skill_ducc_runner.py"
SPEC = importlib.util.spec_from_file_location("skill_ducc_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SESSION = "b335b64d-df43-4b21-81e4-8b604f8f3333"
FAKE = """#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(sys.argv[sys.argv.index("--mcp-config")+1]).parent
(root/"observed.json").write_text(json.dumps({"argv":sys.argv[1:],"prompt":sys.stdin.read(),"output_tokens":os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],"cwd":str(Path.cwd())}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-1","name":"mcp__fixture__run_lockbot","input":{"action":"resources"}}]}}))
session="b335b64d-df43-4b21-81e4-8b604f8f4444" if "--resume" in sys.argv else "b335b64d-df43-4b21-81e4-8b604f8f3333"
print(json.dumps({"type":"result","result":"node-19操作完成。","session_id":session,"num_turns":3,"is_error":False}))
"""


def prepare(tmp_path, name="phase-1"):
    root = tmp_path / name
    root.mkdir()
    (root / "prompt.txt").write_text("lock 19")
    (root / "system.txt").write_text("Use fixture tools only.")
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {"command": "synthetic-fixture"}}}))
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "apiKeyHelper": "synthetic-helper",
                "env": {"ANTHROPIC_BASE_URL": "https://model.example.test", "SYNTHETIC_TOKEN": "not-a-real-token"},
                "hooks": {"SessionStart": [{"command": "must-not-run"}]},
            }
        )
    )
    executable = tmp_path / "bin/fake-ducc"
    executable.write_text(FAKE)
    executable.chmod(0o700)
    request = {
        "model": "SyntheticModel",
        "prompt_file": str(root / "prompt.txt"),
        "system_file": str(root / "system.txt"),
        "mcp_config": str(root / "mcp.json"),
        "max_turns": 8,
        "output_tokens": 256,
    }
    return root, executable, request


def execute(root, executable, request):
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--ducc", str(executable)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        cwd=root,
        timeout=5,
    )
    return process.returncode, json.loads(process.stdout)


def test_native_client_receives_only_current_prompt_and_explicit_fixture_tools(tmp_path):
    root, executable, request = prepare(tmp_path)
    code, result = execute(root, executable, request)
    assert code == 0
    assert result["text"] == "node-19操作完成。" and result["turns"] == 3
    observed = json.loads((root / "observed.json").read_text())
    assert observed["prompt"] == "lock 19"
    args = observed["argv"]
    assert args[args.index("--model") + 1] == "SyntheticModel"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--allowedTools") + 1] == "mcp__fixture__*"
    assert args[args.index("--max-turns") + 1] == "8"
    assert observed["output_tokens"] == "256"
    assert "--strict-mcp-config" in args and "--resume" not in args
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert settings["apiKeyHelper"] == "synthetic-helper"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://model.example.test"
    assert settings["disableAllHooks"] and settings["hooks"] == {}
    assert "not-a-real-token" not in (root / "native-invocation.json").read_text()
    assert "SessionStart" in json.loads((tmp_path / "settings.json").read_text())["hooks"]
    events = (root / "native-ducc.jsonl").read_text()
    assert '"tool_use"' in events and '"action": "resources"' in events
    for name in (
        "native-ducc.jsonl",
        "native-ducc.stderr",
        "native-session.json",
        "native-invocation.json",
        "native-ducc-settings.json",
    ):
        assert (root / name).stat().st_mode & 0o777 == 0o600


def test_context_uses_verified_native_session_fork_instead_of_prompt_concatenation(tmp_path):
    first, executable, request = prepare(tmp_path)
    _, response = execute(first, executable, request)
    (first / "response.json").write_text(json.dumps(response))
    second, _, followup = prepare(tmp_path, "phase-2")
    (second / "prompt.txt").write_text("再续两小时")
    history = [{"role": "user", "content": "lock 19"}, {"role": "assistant", "content": response["text"]}]
    path = second / "conversation.json"
    path.write_text(json.dumps(history, ensure_ascii=False))
    followup.update(conversation_file=str(path), conversation_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    code, result = execute(second, executable, followup)
    assert code == 0 and result["conversation_sha256"] == followup["conversation_sha256"]
    observed = json.loads((second / "observed.json").read_text())
    assert observed["prompt"] == "再续两小时"
    args = observed["argv"]
    assert args[args.index("--resume") + 1] == SESSION and "--fork-session" in args
    assert observed["cwd"] == str(first)
    assert result["native_session_id"] != SESSION
    assert result["native_context_mode"] == "fork_verified_session"


@pytest.mark.parametrize("change", ["missing", "tampered-history", "different-model", "different-system"])
def test_unverified_history_is_unsupported_without_starting_a_client(tmp_path, change):
    first, executable, request = prepare(tmp_path)
    _, response = execute(first, executable, request)
    (first / "response.json").write_text(json.dumps(response))
    second, _, followup = prepare(tmp_path, "phase-2")
    history = [{"role": "user", "content": "lock 19"}, {"role": "assistant", "content": response["text"]}]
    if change == "missing":
        (first / "native-session.json").unlink()
    if change == "tampered-history":
        history[1]["content"] = "node-99操作完成。"
    if change == "different-model":
        followup["model"] = "OtherModel"
    if change == "different-system":
        (second / "system.txt").write_text("Other instructions")
    path = second / "conversation.json"
    path.write_text(json.dumps(history))
    followup.update(conversation_file=str(path), conversation_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    code, result = execute(second, executable, followup)
    assert code == 0 and result["kind"] == "unsupported_context"
    assert "conversation_sha256" not in result and not (second / "observed.json").exists()


def test_budget_error_is_not_reported_as_model_success():
    response, session = runner.parse_ducc(
        json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"})
    )
    assert response["kind"] == "error_max_turns" and session is None


def test_missing_or_unusable_native_evidence_is_rejected():
    with pytest.raises(ValueError):
        runner.parse_ducc('{"type":"assistant"}')
    with pytest.raises(ValueError):
        runner.parse_ducc(json.dumps({"type": "result", "result": "done", "session_id": "bad", "num_turns": 1}))


def test_explicit_managed_template_is_preserved_and_only_private_copy_disables_hooks(tmp_path):
    root = tmp_path / "phase"
    root.mkdir()
    source = tmp_path / "custom-settings.json"
    original = {
        "apiKeyHelper": "managed-helper",
        "env": {"CUSTOM_PROVIDER": "preserved"},
        "permissions": {"allow": ["fixture-tool"]},
        "hooks": {"SessionStart": ["skip"]},
    }
    source.write_text(json.dumps(original))
    before = source.read_bytes()
    generated = runner.invocation_settings("unused-native-executable", root, source)
    copied = json.loads(generated.read_text())
    assert copied["apiKeyHelper"] == original["apiKeyHelper"]
    assert copied["env"] == original["env"] and copied["permissions"] == original["permissions"]
    assert copied["disableAllHooks"] is True and copied["hooks"] == {}
    assert source.read_bytes() == before and generated.stat().st_mode & 0o777 == 0o600


def test_missing_managed_configuration_never_starts_native_client(tmp_path):
    root, executable, request = prepare(tmp_path)
    (tmp_path / "settings.json").unlink()
    code, result = execute(root, executable, request)
    assert code != 0 and result["kind"] == "native_runner_error"
    assert not (root / "observed.json").exists()
