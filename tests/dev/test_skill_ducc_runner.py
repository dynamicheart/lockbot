"""Native-client subprocess contract; no model or external service is called."""

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
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
(root/"observed.json").write_text(json.dumps({
    "argv":sys.argv[1:],"prompt":sys.stdin.read(),
    "output_tokens":os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],"cwd":str(Path.cwd()),
    "env":{key:os.environ.get(key) for key in ("HOME","CODEX_HOME","HTTPS_PROXY","NO_PROXY")}
}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-1","name":"mcp__fixture__run_lockbot","input":{"action":"resources"}}]}}))
session="b335b64d-df43-4b21-81e4-8b604f8f4444" if "--resume" in sys.argv else "b335b64d-df43-4b21-81e4-8b604f8f3333"
print(json.dumps({"type":"result","result":"node-19操作完成。","session_id":session,"num_turns":3,"is_error":False}))
"""
STREAM_FAKE = """#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[sys.argv.index("--mcp-config")+1]).parent
sys.stdin.read()
print(json.dumps({"type":"system","subtype":"init","session_id":"b335b64d-df43-4b21-81e4-8b604f8f3333"}),flush=True)
sys.stderr.write("early native diagnostic without newline")
sys.stderr.flush()
(root/"native-ready").write_text(str(os.getpid()))
while not (root/"release-native").exists():
    time.sleep(0.01)
print(json.dumps({"type":"result","result":"done","session_id":"b335b64d-df43-4b21-81e4-8b604f8f3333","num_turns":1,"is_error":False,"usage":{"input_tokens":11,"output_tokens":7}}),flush=True)
"""


@pytest.fixture(autouse=True)
def isolated_native_temp_directory(monkeypatch):
    # Retain cwd directories across phases, then remove only this test's native
    # directories. No real client, auth, global home or session index is used.
    with tempfile.TemporaryDirectory(prefix="native-runner-test-") as directory:
        monkeypatch.setenv("TMPDIR", directory)
        yield


def prepare(tmp_path, name="phase-1"):
    root = tmp_path / name
    root.mkdir(parents=True)
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


def start_streaming(root, executable, request):
    executable.write_text(STREAM_FAKE)
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--ducc", str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        start_new_session=True,
    )
    process.stdin.write(json.dumps(request))
    process.stdin.close()
    process.stdin = None
    return process


def wait_for_native_diagnostics(root, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        output, errors = root / "native-ducc.jsonl", root / "native-ducc.stderr"
        if (
            (root / "native-ready").exists()
            and output.exists()
            and errors.exists()
            and output.read_bytes().endswith(b"\n")
            and errors.read_bytes() == b"early native diagnostic without newline"
        ):
            assert process.poll() is None, "native diagnostics must be visible before exit"
            return
        assert process.poll() is None, "runner exited before early diagnostics became visible"
        time.sleep(0.01)
    pytest.fail("native diagnostics were buffered until process completion")


def stop_streaming(process):
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=5)


def test_native_diagnostics_are_visible_before_exit_and_success_contract_is_preserved(tmp_path):
    root, executable, request = prepare(tmp_path)
    process = start_streaming(root, executable, request)
    try:
        wait_for_native_diagnostics(root, process)
        for name in ("native-ducc.jsonl", "native-ducc.stderr"):
            assert (root / name).stat().st_mode & 0o777 == 0o600
        init = json.loads((root / "native-ducc.jsonl").read_text())
        assert init["subtype"] == "init" and init["session_id"] == SESSION
        assert not (root / "native-session.json").exists()
        (root / "release-native").touch()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and not stderr
        result = json.loads(stdout)
        assert result["text"] == "done" and result["turns"] == 1
        assert result["native_session_id"] == SESSION and result["native_evidence"] == "native-ducc.jsonl"
        assert "early native diagnostic" not in stdout
        events = [json.loads(line) for line in (root / "native-ducc.jsonl").read_text().splitlines()]
        assert events[-1]["usage"] == {"input_tokens": 11, "output_tokens": 7}
        state = json.loads((root / "native-session.json").read_text())
        assert state["session_id"] == SESSION and state["answer_sha256"] == runner.digest(b"done")
    finally:
        stop_streaming(process)


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
def test_native_diagnostics_survive_external_process_group_termination(tmp_path, termination):
    root, executable, request = prepare(tmp_path)
    process = start_streaming(root, executable, request)
    try:
        wait_for_native_diagnostics(root, process)
        saved = {name: (root / name).read_bytes() for name in ("native-ducc.jsonl", "native-ducc.stderr")}
        os.killpg(process.pid, termination)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == -termination and not stdout and not stderr
        assert saved == {name: (root / name).read_bytes() for name in saved}
        assert json.loads(saved["native-ducc.jsonl"])["session_id"] == SESSION
        assert not (root / "native-session.json").exists()
    finally:
        stop_streaming(process)


@pytest.mark.parametrize("final_error", [False, True])
def test_native_nonzero_exit_retains_diagnostics_without_forwarding_stderr(tmp_path, final_error):
    root, executable, request = prepare(tmp_path)
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        "sys.stdin.read()\n"
        'print(json.dumps({"type":"system","subtype":"init"}),flush=True)\n'
        'sys.stderr.write("private native failure")\n'
        + (
            'print(json.dumps({"type":"result","is_error":True,"subtype":"synthetic_failure","result":"failed"}),flush=True)\n'
            if final_error
            else ""
        )
        + "sys.exit(7)\n"
    )
    code, result = execute(root, executable, request)
    assert code == 7
    assert result["kind"] == ("synthetic_failure" if final_error else "native_client_failed")
    assert "private native failure" not in json.dumps(result)
    assert (root / "native-ducc.stderr").read_text() == "private native failure"
    assert '"subtype": "init"' in (root / "native-ducc.jsonl").read_text()
    assert not (root / "native-session.json").exists()


@pytest.mark.parametrize("output", ["native-ducc.jsonl", "native-ducc.stderr"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo"])
def test_unsafe_native_output_path_cannot_start_client_or_modify_target(tmp_path, output, unsafe):
    root, executable, request = prepare(tmp_path)
    target = tmp_path / "private-target"
    target.write_text("untouched")
    path = root / output
    if unsafe == "symlink":
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    code, result = execute(root, executable, request)
    assert code == 2 and result["kind"] == "native_runner_error"
    assert target.read_text() == "untouched" and not (root / "observed.json").exists()


def continuation(tmp_path):
    first, executable, request = prepare(tmp_path)
    code, response = execute(first, executable, request)
    assert code == 0
    (first / "response.json").write_text(json.dumps(response))
    second, _, followup = prepare(tmp_path, "phase-2")
    (second / "prompt.txt").write_text("再续两小时")
    history = [{"role": "user", "content": "lock 19"}, {"role": "assistant", "content": response["text"]}]
    path = second / "conversation.json"
    path.write_text(json.dumps(history, ensure_ascii=False))
    followup.update(conversation_file=str(path), conversation_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return first, second, executable, followup


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
    assert args[args.index("--system-prompt") + 1] == "Use fixture tools only."
    assert args[args.index("--mcp-config") + 1] == str((root / "mcp.json").resolve())
    assert observed["output_tokens"] == "256"
    assert "--strict-mcp-config" in args and "--resume" not in args
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert settings["apiKeyHelper"] == "synthetic-helper"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://model.example.test"
    assert settings["disableAllHooks"] and settings["hooks"] == {}
    assert "not-a-real-token" not in (root / "native-invocation.json").read_text()
    native_cwd = Path(observed["cwd"])
    assert native_cwd != root and root.parent not in native_cwd.parents
    assert native_cwd.stat().st_mode & 0o777 == 0o700
    state = json.loads((root / "native-session.json").read_text())
    assert state["native_cwd"] == str(native_cwd) and state["native_cwd_version"] == 1
    assert state["native_cwd_scope"] == result["native_cwd_scope"] == "opaque_private_directory"
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
    first, second, executable, followup = continuation(tmp_path)
    code, result = execute(second, executable, followup)
    assert code == 0 and result["conversation_sha256"] == followup["conversation_sha256"]
    observed = json.loads((second / "observed.json").read_text())
    assert observed["prompt"] == "再续两小时"
    args = observed["argv"]
    assert args[args.index("--resume") + 1] == SESSION and "--fork-session" in args
    first_cwd = json.loads((first / "observed.json").read_text())["cwd"]
    assert observed["cwd"] == first_cwd and observed["cwd"] != str(first)
    assert result["native_session_id"] != SESSION
    assert result["native_context_mode"] == "fork_verified_session"
    assert result["native_cwd_scope"] == "opaque_private_directory"


def test_native_cwd_exposes_no_case_label_and_preserves_inherited_runtime_settings(tmp_path, monkeypatch):
    inherited = {
        "HOME": "/synthetic/home",
        "CODEX_HOME": "/synthetic/codex",
        "HTTPS_PROXY": "http://proxy.example.test:1234",
        "NO_PROXY": "192.0.2.10",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    root, executable, request = prepare(tmp_path, "run/cli/intent-my-locks/001/phase-1")
    original = {name: (root / name).read_bytes() for name in ("prompt.txt", "system.txt", "mcp.json")}
    managed = (tmp_path / "settings.json").read_bytes()
    code, result = execute(root, executable, request)
    assert code == 0 and result["native_cwd_scope"] == "opaque_private_directory"
    observed = json.loads((root / "observed.json").read_text())
    assert "intent-my-locks" not in observed["cwd"] and "phase-1" not in observed["cwd"]
    assert tmp_path not in Path(observed["cwd"]).parents
    assert observed["env"] == inherited
    assert original == {name: (root / name).read_bytes() for name in original}
    assert managed == (tmp_path / "settings.json").read_bytes()
    assert (root / "native-ducc-settings.json").is_file() and (root / "native-ducc.jsonl").is_file()


@pytest.mark.parametrize(
    "change",
    [
        "state-symlink",
        "invocation-symlink",
        "cwd-symlink",
        "cwd-permission",
        "cwd-replaced",
        "unknown-cwd",
        "unknown-version",
        "downgrade",
        "missing-directory",
        "metadata-public",
        "invocation-public",
    ],
)
def test_changed_or_unproven_native_cwd_cannot_start_a_continuation(tmp_path, change):
    first, second, executable, followup = continuation(tmp_path)
    state_file = first / "native-session.json"
    state = json.loads(state_file.read_text())
    native_cwd = Path(state["native_cwd"])
    if change in {"state-symlink", "invocation-symlink"}:
        path = state_file if change == "state-symlink" else first / "native-invocation.json"
        target = path.with_suffix(".original")
        path.rename(target)
        path.symlink_to(target)
    elif change in {"metadata-public", "invocation-public"}:
        (state_file if change == "metadata-public" else first / "native-invocation.json").chmod(0o644)
    elif change == "cwd-permission":
        native_cwd.chmod(0o755)
    elif change in {"cwd-symlink", "cwd-replaced"}:
        target = native_cwd.with_name(native_cwd.name + "-original")
        native_cwd.rename(target)
        if change == "cwd-symlink":
            native_cwd.symlink_to(target, target_is_directory=True)
        else:
            native_cwd.mkdir(mode=0o700)
    elif change == "missing-directory":
        native_cwd.rmdir()
    else:
        if change == "unknown-cwd":
            state["native_cwd"] = str(tmp_path)
        elif change == "unknown-version":
            state["native_cwd_version"] = 999
        elif change == "downgrade":
            state = {key: value for key, value in state.items() if not key.startswith("native_cwd")}
        state_file.write_text(json.dumps(state))
    code, result = execute(second, executable, followup)
    assert code == 0 and result["kind"] == "unsupported_context"
    assert not (second / "observed.json").exists()


def legacy_session(first):
    for name in ("native-session.json", "native-invocation.json"):
        path = first / name
        metadata = {
            key: value for key, value in json.loads(path.read_text()).items() if not key.startswith("native_cwd")
        }
        if name == "native-invocation.json":
            metadata["cwd"] = str(first)
        path.write_text(json.dumps(metadata))


def test_proven_legacy_phase_one_directory_can_resume_with_explicit_legacy_scope(tmp_path):
    first, second, executable, followup = continuation(tmp_path)
    legacy_session(first)
    code, result = execute(second, executable, followup)
    assert code == 0 and result["native_context_mode"] == "fork_verified_session"
    assert result["native_cwd_scope"] == "legacy_named_evidence_directory"
    assert json.loads((second / "observed.json").read_text())["cwd"] == str(first)
    state = json.loads((second / "native-session.json").read_text())
    assert state["native_cwd_scope"] == "legacy_named_evidence_directory"


@pytest.mark.parametrize("change", ["missing", "wrong-cwd", "wrong-client"])
def test_legacy_cwd_is_not_guessed_without_original_invocation_proof(tmp_path, change):
    first, second, executable, followup = continuation(tmp_path)
    legacy_session(first)
    path = first / "native-invocation.json"
    if change == "missing":
        path.unlink()
    else:
        invocation = json.loads(path.read_text())
        invocation["cwd" if change == "wrong-cwd" else "client"] = str(tmp_path) if change == "wrong-cwd" else "other"
        path.write_text(json.dumps(invocation))
    code, result = execute(second, executable, followup)
    assert code == 0 and result["kind"] == "unsupported_context"
    assert not (second / "observed.json").exists()


def test_temp_directory_inside_named_evidence_is_rejected(tmp_path, monkeypatch):
    root, executable, request = prepare(tmp_path, "run/cli/intent-my-locks/001/phase-1")
    unsafe_temp = root.parents[3] / "native-temp"
    unsafe_temp.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(unsafe_temp))
    code, result = execute(root, executable, request)
    assert code == 0 and result["kind"] == "unsupported_context"
    assert result["reason"] == "native_temp_directory_inside_evidence"
    assert not (root / "observed.json").exists()


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
