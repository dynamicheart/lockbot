import json
import runpy
from hashlib import sha256
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "tools/trace-viewer/synthetic_transcript.py"
projection = runpy.run_path(str(SOURCE))
clean_text = projection["clean_text"]
synthetic_sessions = projection["synthetic_sessions"]


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
