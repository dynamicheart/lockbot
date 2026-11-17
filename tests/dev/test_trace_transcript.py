import json
import runpy
from hashlib import sha256
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "tools/trace-viewer/synthetic_transcript.py"
projection = runpy.run_path(str(SOURCE))
clean_text = projection["clean_text"]
synthetic_sessions = projection["synthetic_sessions"]
tool_steps = projection["tool_steps"]


def test_redacts_credentials_urls_and_absolute_paths():
    hidden = [
        "lbr_12345678901234567890",
        "REC-ABCD1234",
        "https://private.example.test/path",
        "file:///private/tmp/notes.txt",
        "/Users/example/project/keys",
        "someone@private.example.test",
        "10.20.30.40:8002",
        "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.signature",
    ]
    text = "请检查绑定状态。" + " ".join(hidden)
    redacted = clean_text(text)
    assert redacted.startswith("请检查绑定状态。")
    assert all(value not in redacted for value in hidden)
    assert "abcdefghi1234" not in clean_text("Authorization: Bearer abcdefghi1234")
    assert len(clean_text("文" * 2000).encode()) < 2300


def test_synthetic_projection_uses_only_matching_evidence_and_known_fields(tmp_path):
    location = "cli/reset-personal-key/001/phase-1"
    folder = tmp_path / location
    folder.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    (folder / "subject.md").write_text(subject)
    (folder / "case.json").write_text(
        json.dumps({"scenario": "reset-personal-key", "phase": 1, "skill_sha256": digest})
    )
    (folder / "prompt.txt").write_text("我确认重置这个测试 Key。")
    (folder / "response.json").write_text(json.dumps({"text": "请在群里发送 bind RST-ABCD1234。"}))
    (folder / "trace.jsonl").write_text(
        json.dumps(
            {
                "tool": "run_lockbot",
                "args": {"action": "key-challenge", "purpose": "reset", "private": "Never share"},
                "result": {"private": "Never share"},
            }
        )
        + "\n"
    )
    phases = [
        {"phase": 1, "evidence": location, "skill_sha256": digest},
        {"phase": 2, "simulated_user_actions": ["chat_binding_confirmation"]},
    ]
    (session,) = synthetic_sessions(tmp_path, "reset-personal-key", 1, phases)
    assert [event["role"] for event in session] == ["user", "tools", "assistant", "human"]
    assert session[1]["evidence"] == [
        {"input": {"action": "key-challenge", "purpose": "reset"}, "output": {"status": "已返回"}}
    ]
    assert "RST-ABCD1234" not in session[2]["text"]
    assert synthetic_sessions(tmp_path, "reset-personal-key", 2, phases) == []
    (folder / "prompt.txt").unlink()
    another_file = tmp_path / "another-file.txt"
    another_file.write_text("Sensitive unrelated content")
    (folder / "prompt.txt").symlink_to(another_file)
    assert synthetic_sessions(tmp_path, "reset-personal-key", 1, phases) == []


def test_skill_tool_steps_project_page_metadata_without_document_contents():
    raw = "lbr_12345678901234567890 https://private.example.test/path"
    records = [
        {
            "tool": "read_lockbot_skill",
            "args": {"offset": 0, "url": raw},
            "result": {"content": raw, "total_characters": 9000, "next_offset": 6000},
        },
        {
            "tool": "read_lockbot_skill",
            "args": {"reference": "references/connections.md", "offset": 240},
            "result": {"content": "中文", "total_characters": 242, "next_offset": None},
        },
        {
            "tool": "read_lockbot_skill",
            "args": {"reference": "/private/unknown.md", "offset": True},
            "result": {"error": raw, "total_characters": 100_000, "next_offset": -1},
        },
        {
            "tool": "install_lockbot",
            "args": {"url": raw},
            "result": {
                "installed": True,
                "version": "0123456789abcdef",
                "path": "/private/installation",
                "content": raw,
                "total_characters": 9000,
                "next_offset": 6000,
            },
        },
        {
            "tool": "install_lockbot",
            "args": {"url": raw},
            "result": {"installed": True, "version": raw, "total_characters": raw},
        },
    ]
    projected = tool_steps("\n".join(map(json.dumps, records)), 1)
    assert projected["evidence"] == [
        {
            "input": {"document": "SKILL.md", "offset": 0},
            "output": {
                "status": "已返回",
                "returned_characters": len(raw),
                "total_characters": 9000,
                "next_offset": 6000,
            },
        },
        {
            "input": {"document": "references/connections.md", "offset": 240},
            "output": {"status": "已返回", "returned_characters": 2, "total_characters": 242, "next_offset": None},
        },
        {"input": {}, "output": {"status": "失败"}},
        {
            "input": {},
            "output": {
                "status": "已返回",
                "returned_characters": len(raw),
                "total_characters": 9000,
                "next_offset": 6000,
                "version": "0123456789abcdef",
            },
        },
        {"input": {}, "output": {"status": "已返回"}},
    ]
    serialized = json.dumps(projected)
    for secret in ("lbr_", "private.example.test", "/private/"):
        assert secret not in serialized


def test_failed_native_phase_retains_partial_reply_complete_wire_and_phase_four(tmp_path):
    location = "cli/reset-personal-key/001/phase-4"
    folder = tmp_path / location
    folder.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    (folder / "subject.md").write_text(subject)
    (folder / "case.json").write_text(
        json.dumps({"scenario": "reset-personal-key", "phase": 4, "skill_sha256": digest})
    )
    (folder / "prompt.txt").write_text("Actual phase four")
    record = {
        "direction": "receive",
        "message": {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "Actual partial native reply /private/path"}},
        },
    }
    (folder / "codex-native.jsonl").write_text(json.dumps(record) + "\n")
    rows = [
        {"phase": 3, "evidence": "unavailable"},
        {"phase": 4, "evidence": location, "skill_sha256": digest, "status": "runner_error"},
    ]
    sessions = synthetic_sessions(tmp_path, "reset-personal-key", 1, rows, raw=True)
    assert len(sessions) == 1
    assert sessions[0][0]["text"] == "Actual phase four"
    assert sessions[0][1]["evidence"][0]["output"]["events"] == [record]
    assert sessions[0][2]["text"] == "Actual partial native reply /private/path"
    assert "runner_error" in sessions[0][-1]["text"]


def test_nonzero_tool_exit_is_exported_as_failure():
    record = {"tool": "run_lockbot", "args": {"action": "resources"}, "result": {"exit_code": 2}}
    assert tool_steps(json.dumps(record), 1, raw=True)["steps"][0][-1] == "失败"


def test_raw_native_diagnostics_preserve_actual_error_text(tmp_path):
    error = "Synthetic authentication failure /private/client/path"
    (tmp_path / "native-ducc.stderr").write_text(error)
    evidence = projection["native_evidence"](tmp_path, tmp_path.resolve())
    assert evidence[0]["evidence"][0]["output"]["text"] == error
