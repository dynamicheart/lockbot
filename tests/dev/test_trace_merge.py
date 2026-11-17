import copy
import importlib.util
import json
import shutil
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


def matrix(tmp_path, version="b" * 16, terminal=False, scenario="link-only"):
    root = tmp_path / version
    subject = "Synthetic Skill"
    skill_hash = sha256(subject.encode()).hexdigest()
    archive = b"Synthetic frozen archive"
    archive_hash = sha256(archive).hexdigest()
    plan = {
        "simulation_only": True,
        "artifacts": {"current": {"package_version": version, "revision": version, "sha256": archive_hash}},
        "cases": [f"cli:{scenario}"],
        "runners": [{"client": "training13-codex", "model": "official:gpt-6.1-sol"}],
    }
    write(root / "run.json", plan)
    frozen = root / "artifacts/current"
    frozen.mkdir(parents=True)
    (frozen / "SKILL.md").write_text(subject)
    (frozen / "lockbot-agent.zip").write_bytes(archive)
    write(
        frozen / "manifest.json",
        {
            "package_version": version,
            "version": version,
            "skill_sha256": skill_hash,
            "installation": {"sha256": archive_hash},
        },
    )
    (root / "progress.jsonl").write_text("")
    location = f"cli/{scenario}/001/phase-1"
    directory = root / location
    directory.mkdir(parents=True)
    subject = "Synthetic Skill"
    digest = sha256(subject.encode()).hexdigest()
    (directory / "subject.md").write_text(subject)
    write(directory / "case.json", {"scenario": scenario, "phase": 1, "skill_sha256": digest})
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
                "scenario": scenario,
                "phase": 1,
                "status": "completed",
                "observable_pass": False,
                "evidence": location,
                "skill_sha256": digest,
                "artifact_revision": version,
            }
        ],
    }
    write(directory / "result.json", report["results"][0])
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


def test_uncertain_correction_sidecar_appends_without_mutating_published_review(tmp_path):
    raw, latest = baseline(tmp_path)
    source, _ = matrix(tmp_path, terminal=True)
    response = source / "cli/link-only/001/phase-1/response.json"
    finding = {
        "case": "link-only",
        "run": "001",
        "semantic_status": "fail",
        "note": "Prior classification",
        "response_sha256": sha256(response.read_bytes()).hexdigest(),
    }
    write(source / "manual-semantic-review.json", {"scope": "Original", "findings": [finding]})
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    before = merge.load(output / "raw.json")
    old = before["cases"]["link-only"][-1]
    corrected = {
        **finding,
        "semantic_status": "uncertain",
        "reason": "fixture_conflict",
        "note": "Fixture injected a field absent from the real API; do not attribute this to the model.",
    }
    write(
        source / "manual-semantic-review-correction.json",
        {"kind": "correction", "scope": "Fixture correction", "findings": [corrected]},
    )
    write(
        source / "manual-semantic-review-independent.json",
        {
            "scope": "Independent",
            "findings": [{**finding, "semantic_status": "uncertain", "note": "Independently uncertain"}],
        },
    )
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    after = merge.load(output / "raw.json")
    merge.assert_preserved(before, after, True)
    correction = next(
        t for t in after["cases"]["link-only"] if t.get("source_run", "").startswith("semantic-correction:")
    )
    assert correction["state"] == "pending"
    assert correction["semantic_status"] == "uncertain" and correction["semantic_reason"] == "fixture_conflict"
    assert correction["corrects_source_runs"] == [old["source_run"]]
    assert "不能据此归因于模型" in correction["note"]
    assert any(
        t.get("semantic_status") == "uncertain" and "Independently" in t.get("note", "")
        for t in after["cases"]["link-only"]
    )
    stable = (output / "raw.json").read_bytes()
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    assert (output / "raw.json").read_bytes() == stable


def test_independent_reviews_schema_binds_timeout_without_inventing_final_reply(tmp_path):
    raw, latest = baseline(tmp_path)
    source, _ = matrix(tmp_path, terminal=True)
    location = source / "cli/link-only/001/phase-1"
    (location / "response.json").unlink()
    write(location / "result.json", {"status": "timeout", "duration_seconds": 180})
    terminal = merge.load(source / "matrix.json")
    terminal["results"][0]["status"] = "timeout"
    write(source / "matrix.json", terminal)
    review = {
        "scope": "Independent",
        "reviews": [
            {
                "suite": "cli",
                "scenario": "link-only",
                "client": "training13-codex",
                "model": "official:gpt-6.1-sol",
                "status": "timeout",
                "evidence": str(location),
                "response_sha256": None,
                "semantic_status": "uncertain",
                "finding_code": "NO_FINAL_REPLY",
                "reason": "No final reply; semantic outcome unknown",
            }
        ],
    }
    write(source / "manual-semantic-review-independent.json", review)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    trial = merge.load(output / "raw.json")["cases"]["link-only"][-1]
    assert trial["state"] == "pending" and trial["semantic_status"] == "uncertain"
    assert trial["terminal_result_sha256"] == sha256((location / "result.json").read_bytes()).hexdigest()
    assert trial["sessions"][0][-1]["role"] == "missing"
    assert not any(event["role"] == "assistant" for event in trial["sessions"][0])
    write(location / "response.json", {"text": "Unexpected later response"})
    with pytest.raises(ValueError, match="absent response"):
        merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])


def test_externalizing_new_diagnostics_keeps_published_inline_trials_and_fixture_io(tmp_path):
    raw, latest = baseline(tmp_path)
    old, _ = matrix(tmp_path, "b" * 16, True)
    first_phase = old / "cli/link-only/001/phase-1"
    record = {
        "direction": "receive",
        "message": {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Native answer"}}},
    }
    write(first_phase / "codex-native.jsonl", record)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=old / "run.json", matrices=[old])
    before = merge.load(output / "raw.json")
    new, _ = matrix(tmp_path, "c" * 16, True)
    second_phase = new / "cli/link-only/001/phase-1"
    write(second_phase / "codex-native.jsonl", record)
    source_bytes = (second_phase / "codex-native.jsonl").read_bytes()
    merge.aggregate(raw, latest, output, plan=new / "run.json", matrices=[old, new], external_native_evidence=True)
    after = merge.load(output / "raw.json")
    merge.assert_preserved(before, after, True)
    trial = after["cases"]["link-only"][-1]
    events = trial["sessions"][0]
    assert events[1]["evidence"][0]["input"] == {"path": "/fixture/SKILL.md"}
    assert events[1]["evidence"][0]["output"] == {"exit_code": 2, "error": "Synthetic failure"}
    reference = events[2]["evidence"][0]["output"]["artifact"]
    assert (output / "evidence" / reference["sha256"]).read_bytes() == source_bytes
    assert not any(
        "artifact" in e.get("output", {})
        for t in before["cases"]["link-only"]
        for s in t["sessions"]
        for event in s
        if event.get("role") == "tools"
        for e in event.get("evidence", [])
    )
    assert events[-1]["text"] == "Actual native reply"


def test_opt_in_compact_summary_only_affects_new_summary_trials(tmp_path):
    raw, latest = baseline(tmp_path)
    first, _ = matrix(tmp_path, "b" * 16, True)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=first / "run.json", matrices=[first])
    before = merge.load(output / "latest.json")
    second, _ = matrix(tmp_path, "c" * 16, True)
    merge.aggregate(raw, latest, output, plan=second / "run.json", matrices=[first, second], compact_new_summary=True)
    summary, complete = merge.load(output / "latest.json"), merge.load(output / "raw.json")
    merge.assert_preserved(before, summary, False)
    trial = summary["cases"]["link-only"][-1]
    assert trial["sessions"] == [] and trial["summary_only"] is True
    assert "摘要；完整对话和工具输入输出保留在原始轨迹" in trial["note"]
    assert complete["cases"]["link-only"][-1]["sessions"][0][-1]["text"] == "Actual native reply"
    assert complete["cases"]["link-only"][-1]["sessions"][0][1]["evidence"][0]["input"] == {"path": "/fixture/SKILL.md"}


def phase_review(tmp_path, phase, *, absent=False, blocked=False):
    scenario = "recover-original-key" if phase == 2 else "reset-personal-key"
    source, report = matrix(tmp_path, terminal=True, scenario=scenario)
    initial = source / f"cli/{scenario}/001/phase-1"
    first = report["results"][0]
    rows = []
    total = 3 if phase == 2 else 4
    for current in range(1, total + 1):
        location = source / f"cli/{scenario}/001/phase-{current}"
        row = {**first, "phase": current, "evidence": str(location.relative_to(source)), "fixture_version": 13}
        if blocked and current == phase:
            row.update(status="blocked_by_phase_3", observable_pass=None)
            del row["evidence"]
        else:
            if current > 1:
                shutil.copytree(initial, location)
            case = merge.load(location / "case.json")
            case["phase"] = current
            write(location / "case.json", case)
            (location / "prompt.txt").write_text(f"Actual user request for phase {current}")
            write(location / "response.json", {"text": f"Actual reply for phase {current}"})
            if absent and current == phase:
                (location / "response.json").unlink()
                row.update(status="runner_error", observable_pass=None)
            write(location / "result.json", row)
        rows.append(row)
    report["results"] = rows
    write(source / "matrix.json", report)
    row = next(row for row in rows if row["phase"] == phase)
    location = source / f"cli/{scenario}/001/phase-{phase}"
    response, result = location / "response.json", location / "result.json"
    item = {
        "suite": "cli",
        "scenario": scenario,
        "phase": phase,
        "client": report["runners"][0]["client"],
        "model": report["runners"][0]["model"],
        "status": row["status"],
        "evidence": str(location),
        "response_sha256": sha256(response.read_bytes()).hexdigest() if response.exists() else None,
        "result_sha256": sha256(result.read_bytes()).hexdigest() if result.exists() else None,
        "matrix_result_sha256": merge.matrix_row_digest(row),
        "semantic_status": "uncertain" if absent or blocked else "pass",
        "reason": "Actual phase-specific review",
        "finding_code": "NO_FINAL_REPLY" if absent or blocked else "SUPPORTED",
    }
    version = report["artifacts"]["current"]["package_version"]
    skill = source / "artifacts/current/SKILL.md"
    review = {
        "scope": "Phase-aware review",
        "evidence_root": str(source),
        "reviews": [item],
        "frozen_artifact": {
            "package_version": version,
            "skill_sha256": sha256(skill.read_bytes()).hexdigest(),
            "source": str(skill),
        },
    }
    return source, review, location


@pytest.mark.parametrize("phase", [2, 4])
def test_phase_specific_review_keeps_original_run_and_only_actual_single_phase(tmp_path, phase):
    raw, latest = baseline(tmp_path)
    source, review, location = phase_review(tmp_path, phase)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    before = merge.load(output / "raw.json")
    write(source / "manual-semantic-review-independent.json", review)
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    after = merge.load(output / "raw.json")
    merge.assert_preserved(before, after, True)
    case = "case-2" if phase == 2 else "case-3"
    trial = after["cases"][case][-1]
    assert trial["review_phase"] == phase and f"阶段 {phase}" in trial["note"]
    assert trial["sessions"] == [
        [
            {"role": "user", "text": (location / "prompt.txt").read_text()},
            {"role": "assistant", "text": merge.load(location / "response.json")["text"]},
        ]
    ]
    original = next(t for t in before["cases"][case] if t.get("source_run", "").startswith("matrix:"))
    assert len(original["sessions"]) == (3 if phase == 2 else 4)
    assert trial["matrix_result_sha256"] == review["reviews"][0]["matrix_result_sha256"]


@pytest.mark.parametrize(
    "tamper", ["phase", "path", "client", "model", "response", "result", "matrix", "artifact", "symlink"]
)
def test_phase_review_rejects_forged_identity_path_and_digests(tmp_path, tamper):
    raw, latest = baseline(tmp_path)
    source, review, location = phase_review(tmp_path, 4)
    item = review["reviews"][0]
    if tamper == "phase":
        item["phase"] = 2
    elif tamper == "path":
        item["evidence"] = str(tmp_path / "forged" / location.parent.name / "phase-4")
    elif tamper in {"client", "model"}:
        item[tamper] = "forged identity"
    elif tamper in {"response", "result", "matrix"}:
        item[{"response": "response_sha256", "result": "result_sha256", "matrix": "matrix_result_sha256"}[tamper]] = (
            "0" * 64
        )
    elif tamper == "artifact":
        review["frozen_artifact"]["package_version"] = "f" * 16
    else:
        actual = location / "response.json"
        target = tmp_path / "same-bytes.json"
        target.write_bytes(actual.read_bytes())
        actual.unlink()
        actual.symlink_to(target)
    write(source / "manual-semantic-review-independent.json", review)
    output = tmp_path / "merged"
    with pytest.raises(ValueError):
        merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    assert not (output / "raw.json").exists()


@pytest.mark.parametrize("blocked", [False, True])
def test_absent_phase_response_uses_result_or_matrix_hash_without_invented_chat(tmp_path, blocked):
    raw, latest = baseline(tmp_path)
    source, review, location = phase_review(tmp_path, 4, absent=not blocked, blocked=blocked)
    write(source / "manual-semantic-review-independent.json", review)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    trial = merge.load(output / "raw.json")["cases"]["case-3"][-1]
    assert trial["review_phase"] == 4 and trial["state"] == "pending"
    assert not any(e["role"] == "assistant" for e in trial["sessions"][0])
    if blocked:
        assert [e["role"] for e in trial["sessions"][0]] == ["missing"]
        assert "terminal_result_sha256" not in trial
    else:
        assert [e["role"] for e in trial["sessions"][0]] == ["user", "missing"]
        assert trial["terminal_result_sha256"] == sha256((location / "result.json").read_bytes()).hexdigest()
    assert "阶段 4" in trial["sessions"][0][-1]["text"]
    old = (output / "raw.json").read_bytes()
    location.mkdir(parents=True, exist_ok=True)
    write(location / "response.json", {"text": "Unexpected final reply"})
    with pytest.raises(ValueError):
        merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    assert (output / "raw.json").read_bytes() == old


def test_published_legacy_review_normalization_and_source_id_remain_verbatim():
    item = {
        "suite": "cli",
        "scenario": "link-only",
        "client": "ducc",
        "model": "example",
        "status": "completed",
        "evidence": "/private/run/cli/link-only/001/phase-1",
        "response_sha256": "a" * 64,
        "response_text_sha256": "b" * 64,
        "semantic_status": "pass",
        "reason": "Prior note",
    }
    normalized = merge.semantic_findings({"reviews": [item]})[0]
    expected = {
        "suite": "cli",
        "case": "link-only",
        "run": "001",
        "client": "ducc",
        "model": "example",
        "evidence": item["evidence"],
        "response_sha256": "a" * 64,
        "semantic_status": "pass",
        "status": "completed",
        "note": "Prior note",
    }
    assert normalized == expected
    assert merge.fingerprint({"finding": normalized, "scope": "Historical"}) == merge.fingerprint(
        {"finding": expected, "scope": "Historical"}
    )


def test_phase_review_retains_secondary_action_safety_failure_even_if_semantics_pass(tmp_path):
    raw, latest = baseline(tmp_path)
    source, review, _ = phase_review(tmp_path, 4)
    review["reviews"][0].update(
        overall_review_status="fail",
        action_safety_status="write_repeated",
        secondary_review_notes=["Observed an unsafe repeated write in this phase"],
    )
    write(source / "manual-semantic-review-independent.json", review)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    trial = merge.load(output / "raw.json")["cases"]["case-3"][-1]
    assert trial["semantic_status"] == "pass" and trial["state"] == "fail"
    assert trial["overall_review_status"] == "fail" and trial["action_safety_status"] == "write_repeated"
    assert "Observed an unsafe repeated write" in trial["note"]


def test_compact_summary_applies_only_to_new_phase_reviews_while_raw_chat_stays_complete(tmp_path):
    raw, latest = baseline(tmp_path)
    source, review, location = phase_review(tmp_path, 2)
    path = source / "manual-semantic-review-independent.json"
    write(path, review)
    output = tmp_path / "merged"
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source])
    before = merge.load(output / "latest.json")
    review["scope"] = "A separate later review"
    write(source / "manual-semantic-review-later.json", review)
    merge.aggregate(raw, latest, output, plan=source / "run.json", matrices=[source], compact_new_summary=True)
    summary, complete = merge.load(output / "latest.json"), merge.load(output / "raw.json")
    merge.assert_preserved(before, summary, False)
    trial = summary["cases"]["case-2"][-1]
    assert trial["summary_only"] is True and trial["sessions"] == [] and trial["review_phase"] == 2
    assert "摘要；完整对话和工具输入输出保留在原始轨迹" in trial["note"]
    assert complete["cases"]["case-2"][-1]["sessions"][0][-1]["text"] == merge.load(location / "response.json")["text"]


def test_catalog_plans_do_not_repeat_for_new_packages_or_runner_mixes(tmp_path, monkeypatch):
    raw, _ = baseline(tmp_path)
    source, _ = matrix(tmp_path)
    data = merge.load(raw)
    plan = merge.load(source / "run.json")
    merge.add_plan(data, plan)
    before = copy.deepcopy(data)
    plan["artifacts"]["current"]["package_version"] = "c" * 16
    plan["runners"] = [{"client": "different-client", "model": "different-model"}]
    merge.add_plan(data, plan)
    assert data == before
    monkeypatch.setitem(merge.CASES, "consultation:future-case", ("Future group", "Future case"))
    merge.add_plan(data, plan)
    assert sum(map(len, data["cases"].values())) == sum(map(len, before["cases"].values())) + 1
    merge.assert_preserved(before, data, True)
    trial = data["cases"]["consultation-future-case"][0]
    assert trial["state"] == "pending" and trial["selected_for_run"] is False
    assert "本次运行未选择此测例" in trial["note"]
    assert all(data["cases"][key] == value for key, value in before["cases"].items())
