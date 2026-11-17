import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools/trace-viewer"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("trace_merge", TOOLS / "merge_snapshots.py")
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n")


def baseline(tmp_path):
    data = {
        "schema": 1,
        "version": "a" * 16,
        "labels": {"link-only": "Historical label"},
        "groups": {"link-only": "Historical group"},
        "cases": {
            "link-only": [
                {
                    "case": "link-only",
                    "model": "Historical model",
                    "state": "fail",
                    "sessions": [[{"role": "assistant", "text": "Old reply"}]],
                }
            ]
        },
    }
    latest, raw = tmp_path / "base-latest.json", tmp_path / "base-raw.json"
    write(latest, data)
    write(raw, {**data, "visibility": "raw"})
    return raw, latest


def matrix(tmp_path, version="b" * 16, terminal=False):
    root = tmp_path / version
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": version}},
        "cases": ["cli:link-only"],
        "runners": [{"client": "training13-codex", "model": "official:gpt-6.1-sol"}],
    }
    write(root / "run.json", plan)
    (root / "progress.jsonl").write_text("")
    location = "cli/link-only/001/phase-1"
    directory = root / location
    directory.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    (directory / "subject.md").write_text(subject)
    write(directory / "case.json", {"scenario": "link-only", "phase": 1, "skill_sha256": digest})
    (directory / "prompt.txt").write_text("Actual user request")
    write(directory / "response.json", {"text": "Actual native reply"})
    (directory / "trace.jsonl").write_text(
        json.dumps(
            {
                "tool": "read_file",
                "args": {"path": "/fixture/SKILL.md"},
                "result": {"exit_code": 2, "error": "Synthetic failure"},
            }
        )
        + "\n"
    )
    report = {
        **plan,
        "results": [
            {
                **plan["runners"][0],
                "suite": "cli",
                "scenario": "link-only",
                "phase": 1,
                "status": "completed",
                "observable_pass": False,
                "evidence": location,
                "skill_sha256": digest,
            }
        ],
    }
    if terminal:
        write(root / "matrix.json", report)
    return root, report


def test_merge_keeps_all_prior_comment_digests_and_appends_only_terminal_results(tmp_path):
    raw, latest = baseline(tmp_path)
    source, report = matrix(tmp_path)
    output = tmp_path / "merged"
    first = merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    planned = merge.load(output / "raw.json")
    assert first["waiting_for_terminal_matrix"] == [str(source)]
    assert len(planned["cases"]) == len(merge.CASES)
    assert all(
        trial["state"] == "pending"
        for trials in planned["cases"].values()
        for trial in trials
        if trial.get("source_run", "").startswith("plan:")
    )
    assert planned["version"] == "a" * 16
    assert planned["labels"]["link-only"] == "Historical label"
    write(source / "matrix.json", report)
    second = merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    completed = merge.load(output / "raw.json")
    assert not second["waiting_for_terminal_matrix"]
    assert merge.assert_preserved(planned, completed, True) == first["raw.json"]["trials"]
    trial = completed["cases"]["link-only"][-1]
    assert trial["state"] == "fail" and trial["package_version"] == "b" * 16
    assert trial["model"] == "training13-codex · official:gpt-6.1-sol"
    assert trial["sessions"][0][-1]["text"] == "Actual native reply"
    assert trial["sessions"][0][1]["evidence"][0]["input"]["path"] == "/fixture/SKILL.md"
    before = (output / "raw.json").read_bytes()
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    assert (output / "raw.json").read_bytes() == before
    assert (output / "raw.json").stat().st_mode & 0o777 == 0o600


def test_multiple_package_revisions_remain_separate_and_anchor_compatible(tmp_path):
    raw, latest = baseline(tmp_path)
    first, _ = matrix(tmp_path, "b" * 16, True)
    second, _ = matrix(tmp_path, "c" * 16, True)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=first / "run.json", matrices=[first])
    previous = merge.load(output / "raw.json")
    merge.aggregate(raw, latest, output, plan=second / "run.json", matrices=[second, first])
    current = merge.load(output / "raw.json")
    merge.assert_preserved(previous, current, True)
    native = [trial for trial in current["cases"]["link-only"] if trial.get("source_run", "").startswith("matrix:")]
    assert [trial["package_version"] for trial in native] == ["b" * 16, "c" * 16]
    assert current["version"] == "a" * 16


def test_existing_anchor_mutation_and_invalid_or_oversized_schema_fail_closed(tmp_path, monkeypatch):
    raw, _ = baseline(tmp_path)
    data = merge.load(raw)
    changed = merge.load(raw)
    changed["version"] = "b" * 16
    with pytest.raises(ValueError, match="version"):
        merge.assert_preserved(data, changed, True)
    changed = merge.load(raw)
    changed["cases"]["link-only"][0]["sessions"][0][0]["text"] = "Changed reply"
    with pytest.raises(ValueError, match="anchor changed"):
        merge.assert_preserved(data, changed, True)
    changed["cases"]["link-only"][0]["state"] = "invented"
    with pytest.raises(ValueError, match="Invalid trace trial"):
        merge.validate(changed, True)
    monkeypatch.setattr(merge, "MAX_TRACE_BYTES", 1)
    with pytest.raises(ValueError, match="no raw evidence was truncated"):
        merge.validate(data, True)


def test_semantic_review_is_hash_bound_and_appended_without_changing_original(tmp_path):
    raw, latest = baseline(tmp_path)
    source, _ = matrix(tmp_path, terminal=True)
    response = source / "cli/link-only/001/phase-1/response.json"
    finding = {
        "case": "link-only",
        "run": "001",
        "semantic_status": "fail",
        "note": "Actual semantic error",
        "response_sha256": sha256(response.read_bytes()).hexdigest(),
    }
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    previous = merge.load(output / "raw.json")
    write(source / "manual-semantic-review.json", {"scope": "One response only", "findings": [finding]})
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    current = merge.load(output / "raw.json")
    merge.assert_preserved(previous, current, True)
    review = current["cases"]["link-only"][-1]
    assert review["state"] == "fail" and review["semantic_status"] == "fail"
    assert review["response_sha256"] == finding["response_sha256"]
    response.write_text("Changed evidence")
    with pytest.raises(ValueError, match="does not match"):
        merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    assert merge.load(output / "raw.json") == current
