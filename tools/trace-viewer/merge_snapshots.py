#!/usr/bin/env python3
"""Append terminal synthetic runs to private board snapshots without moving comments."""

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from publish_snapshot import CASES, LEGACY_IDS, MAX_SNAPSHOT_BYTES, snapshot
from synthetic_transcript import (
    MAX_TRACE_BYTES,
    clean_prompt,
    clean_text,
    native_artifact_manifest,
    native_evidence,
    native_partial_replies,
)

STATES = {"pass", "fail", "pending", "blocked", "timeout"}


def load(path):
    return json.loads(path.read_text())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def trial_identity(data, case, index, raw):
    # Keep exactly the review backend's identity contract.
    return fingerprint(
        {
            "raw": raw,
            "version": data.get("version"),
            "case_id": case,
            "run_index": index,
            "trial": data["cases"][case][index],
        }
    )


def validate(data, raw):
    if data.get("schema") != 1 or not isinstance(data.get("cases"), dict) or not data["cases"]:
        raise ValueError("Expected a nonempty trace viewer v1 snapshot")
    for key in ("title", "version"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError("Invalid snapshot metadata")
    for key in ("labels", "criteria", "groups"):
        if key in data and (not isinstance(data[key], dict) or not all(isinstance(v, str) for v in data[key].values())):
            raise ValueError("Invalid snapshot label map")
    if raw and data.get("visibility") != "raw":
        raise ValueError("Raw exports must retain raw visibility")
    for trials in data["cases"].values():
        if not isinstance(trials, list) or not trials:
            raise ValueError("Every trace case needs at least one trial")
        for trial in trials:
            if any(trial.get(key) is not None and not isinstance(trial[key], str) for key in ("model", "note")):
                raise ValueError("Invalid trace trial text")
            if trial.get("state") not in STATES or not isinstance(trial.get("sessions"), list):
                raise ValueError("Invalid trace trial")
            for session in trial["sessions"]:
                if not isinstance(session, list):
                    raise ValueError("Invalid trace session")
                for event in session:
                    if event.get("role") == "tools":
                        if not isinstance(event.get("steps"), list) or any(
                            not isinstance(step, list) or len(step) != 4 or not all(isinstance(v, str) for v in step)
                            for step in event["steps"]
                        ):
                            raise ValueError("Invalid trace tool event")
                        if "evidence" in event and not isinstance(event["evidence"], list):
                            raise ValueError("Invalid trace tool evidence")
                    elif event.get("role") not in {"user", "assistant", "human", "missing"} or not isinstance(
                        event.get("text"), str
                    ):
                        raise ValueError("Invalid trace message")
    content = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    limit = MAX_TRACE_BYTES if raw else MAX_SNAPSHOT_BYTES
    if len(content) > limit:
        raise ValueError(f"Merged snapshot exceeds {limit} bytes; no raw evidence was truncated")
    return content


def assert_preserved(base, merged, raw):
    if merged.get("version") != base.get("version"):
        raise ValueError("Changing the board version invalidates existing comment anchors")
    checked = 0
    for case, trials in base["cases"].items():
        for index in range(len(trials)):
            try:
                old, new = trial_identity(base, case, index, raw), trial_identity(merged, case, index, raw)
            except (KeyError, IndexError) as error:
                raise ValueError("An existing comment anchor was removed") from error
            if old != new:
                raise ValueError("An existing comment anchor changed")
            checked += 1
    return checked


def append_trial(data, case, trial):
    trials = data["cases"].setdefault(case, [])
    if any(old.get("source_run") == trial["source_run"] for old in trials):
        return False
    trials.append(trial)
    return True


def add_plan(data, plan):
    version = plan["artifacts"]["current"]["package_version"]
    source = "plan:" + fingerprint({"package_version": version, "runners": plan["runners"], "cases": list(CASES)})
    clients = "、".join(f"{row['client']} · {row['model']}" for row in plan["runners"])
    for case, (group, label) in CASES.items():
        suite, scenario = case.split(":", 1)
        safe = LEGACY_IDS.get(scenario, scenario) if suite == "cli" else f"{suite}-{scenario}"
        data.setdefault("labels", {}).setdefault(safe, label)
        data.setdefault("groups", {}).setdefault(safe, group)
        append_trial(
            data,
            safe,
            {
                "case": safe,
                "model": "本轮评测计划",
                "state": "pending",
                "package_version": version,
                "source_run": source,
                "note": "计划占位，不代表已执行或通过；实际结果保存在单独运行记录。计划客户端：" + clients,
                "sessions": [],
            },
        )


def add_matrix(data, directory, raw, *, native_artifacts=None, compact_summary=False):
    matrix = load(directory / "matrix.json")
    if not isinstance(matrix.get("results"), list) or matrix.get("simulation_only") is not True:
        raise ValueError("Expected a terminal synthetic matrix with results")
    source = "matrix:" + fingerprint(matrix)
    if any(t.get("source_run", "").startswith(source + ":") for trials in data["cases"].values() for t in trials):
        return  # Already-published trials and their inline diagnostics stay untouched.
    projected = snapshot(
        directory, raw=raw, include_dialogue=not raw, include_planned=True, native_artifacts=native_artifacts
    )
    for key in ("labels", "groups", "criteria"):
        for case, value in projected.get(key, {}).items():
            data.setdefault(key, {}).setdefault(case, value)
    for case, trials in projected["cases"].items():
        for number, trial in enumerate(trials):
            trial = copy.deepcopy(trial)
            trial.update(source_run=f"{source}:{case}:{number}", package_version=projected["version"])
            trial["note"] = f"运行：{directory.name}；安装包：{projected['version']}\n" + trial.get("note", "")
            if compact_summary and not raw:
                trial["sessions"] = []
                trial["summary_only"] = True
                trial["note"] = "摘要；完整对话和工具输入输出保留在原始轨迹。\n" + trial["note"]
            append_trial(data, case, trial)


def add_probe(data, directory, raw, *, native_artifacts=None):
    summary = load(directory / "preflight-summary.json")
    if summary.get("status") in {None, "running", "pending"}:
        raise ValueError("Connectivity probe is not terminal")
    source = "probe:" + fingerprint({"summary": summary, "run": directory.name})
    if any(t.get("source_run") == source for t in data["cases"].get("model-preflight", [])):
        return
    location = (directory / summary["evidence"]).resolve()
    if not location.is_relative_to(directory.resolve()):
        raise ValueError("Probe evidence escaped its directory")
    prompt = (location / "prompt.txt").read_text()
    session = [{"role": "user", "text": prompt if raw else clean_prompt(prompt)}]
    if raw:
        session.extend(native_evidence(location, directory.resolve(), artifacts=native_artifacts))
    response = location / "response.json"
    if response.is_file():
        text = load(response)["text"]
        session.append({"role": "assistant", "text": text if raw else clean_text(text)})
    elif raw:
        session.extend(native_partial_replies(location, directory.resolve()))
    session.append({"role": "missing", "text": "连接预检状态：" + summary["status"]})
    case = "model-preflight"
    data.setdefault("labels", {}).setdefault(case, "模型连接预检")
    data.setdefault("groups", {}).setdefault(case, "运行环境")
    append_trial(
        data,
        case,
        {
            "case": case,
            "model": f"{summary['client']} · {summary['model']}",
            "state": "pass" if summary["status"] == "ready" else "blocked",
            "note": "仅连接预检，不是 Skill 行为通过；运行：" + directory.name,
            "source_run": source,
            "sessions": [session],
        },
    )


def add_semantic_reviews(data, directory, raw, *, compact_summary=False):
    canonical = directory / "manual-semantic-review.json"
    paths = ([canonical] if canonical.is_file() else []) + sorted(
        path for path in directory.glob("manual-semantic-review*.json") if path != canonical
    )
    for path in paths:
        add_semantic_review(data, directory, path, raw, compact_summary=compact_summary)


def semantic_findings(review):
    if "findings" in review:
        return review["findings"]
    findings = []
    for item in review["reviews"]:
        if not isinstance(item.get("evidence"), str):
            raise ValueError("Semantic review evidence must name its actual phase path")
        evidence = Path(item["evidence"])
        phase_match = re.fullmatch(r"phase-([1-4])", evidence.name)
        if not phase_match:
            raise ValueError("Semantic review evidence has no valid phase")
        phase = int(phase_match[1])
        if "phase" in item and (type(item["phase"]) is not int or item["phase"] != phase):
            raise ValueError("Semantic review phase does not match its evidence path")
        code = item.get("finding_code")
        finding = {
            "suite": item["suite"],
            "case": item["scenario"],
            "run": evidence.parent.name,
            "client": item["client"],
            "model": item["model"],
            "evidence": str(evidence),
            "response_sha256": item.get("response_sha256"),
            "semantic_status": item["semantic_status"],
            "status": item["status"],
            "note": item["reason"],
        }
        if "phase" in item or phase != 1:
            finding["phase"] = phase
        # Preserve the exact normalization/fingerprints of previously published
        # phase-one reviews that predate the explicit phase/hash schema.
        if "phase" in item or phase != 1 or "matrix_result_sha256" in item or "result_sha256" in item:
            for key in (
                "result_sha256",
                "matrix_result_sha256",
                "response_text_sha256",
                "overall_review_status",
                "action_safety_status",
            ):
                if key in item:
                    finding[key] = item[key]
        if "phase" in item and item.get("secondary_review_notes"):
            finding["note"] += "\n补充复核：" + "\n".join(item["secondary_review_notes"])
        if "phase" in item and item.get("overall_review_status"):
            finding["note"] += "\n整体复核：" + item["overall_review_status"]
            if item.get("action_safety_status"):
                finding["note"] += "；动作安全：" + item["action_safety_status"]
        if code:
            finding["finding_code"] = code
        if code == "FIXTURE_CONFLICT":
            finding["reason"] = "fixture_conflict"
        elif code == "NO_FINAL_REPLY":
            finding["reason"] = "no_final_reply"
        if item.get("exact_excerpt"):
            finding["note"] += "\n实际摘录：" + item["exact_excerpt"]
        findings.append(finding)
    return findings


def semantic_bytes(path, root):
    if path.resolve() != path or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Semantic evidence must be a regular in-directory path without symlinks")
    with path.open("rb") as source:
        content = source.read(MAX_TRACE_BYTES + 1)
    if len(content) > MAX_TRACE_BYTES:
        raise ValueError("Semantic evidence exceeds its file limit")
    return content


def matrix_row_digest(row):
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def bind_semantic_phase(directory, plan, review, finding, suite, scenario, pair, location, phase):
    matrix = load(directory / "matrix.json")
    if matrix.get("simulation_only") is not True or matrix.get("artifacts") != plan["artifacts"]:
        raise ValueError("Semantic review source artifact does not match the synthetic matrix")
    rows = [
        row
        for row in matrix["results"]
        if all(
            row.get(key) == value
            for key, value in {
                "suite": suite,
                "scenario": scenario,
                "client": pair["client"],
                "model": pair["model"],
                "phase": phase,
            }.items()
        )
    ]
    if len(rows) != 1:
        raise ValueError("Semantic review phase has no unique actual matrix row")
    row = rows[0]
    if "status" in finding and finding["status"] != row["status"]:
        raise ValueError("Semantic review status does not match its actual phase")
    if finding.get("matrix_result_sha256") and matrix_row_digest(row) != finding["matrix_result_sha256"]:
        raise ValueError("Semantic review matrix row digest does not match")
    if row.get("evidence") is not None and directory / row["evidence"] != location:
        raise ValueError("Actual matrix evidence does not match the reviewed phase")
    artifact_name = "panel" if (suite, scenario) == ("cli", "panel") else "current"
    artifact = plan["artifacts"][artifact_name]
    if row.get("artifact_revision") != artifact.get("revision"):
        raise ValueError("Semantic review matrix artifact revision does not match")
    frozen = directory / "artifacts" / artifact_name
    # Older canonical reviews can precede complete frozen-artifact exports.
    strict = "phase" in finding or "matrix_result_sha256" in finding
    if strict or (frozen / "manifest.json").exists():
        manifest = json.loads(semantic_bytes(frozen / "manifest.json", directory))
        skill_hash = hashlib.sha256(semantic_bytes(frozen / "SKILL.md", directory)).hexdigest()
        if (
            manifest.get("package_version") != artifact["package_version"]
            or manifest.get("version") != artifact.get("revision")
            or manifest.get("skill_sha256") != skill_hash
        ):
            raise ValueError("Semantic review frozen artifact does not match its source")
        if artifact.get("sha256") and (
            manifest.get("installation", {}).get("sha256") != artifact["sha256"]
            or hashlib.sha256(semantic_bytes(frozen / "lockbot-agent.zip", directory)).hexdigest() != artifact["sha256"]
        ):
            raise ValueError("Semantic review frozen archive digest does not match")
        declared = review.get("frozen_artifact", {})
        if artifact_name == "current" and declared:
            if (
                declared.get("package_version") != artifact["package_version"]
                or declared.get("skill_sha256") != skill_hash
            ):
                raise ValueError("Semantic review declared artifact does not match the frozen source")
            if "source" in declared and Path(declared["source"]) != frozen / "SKILL.md":
                raise ValueError("Semantic review declared artifact path does not match")
        if (location / "case.json").exists():
            case = json.loads(semantic_bytes(location / "case.json", directory))
            subject = semantic_bytes(location / "subject.md", directory)
            if (
                case.get("scenario") != scenario
                or case.get("phase", 1) != phase
                or case.get("skill_sha256") != skill_hash
                or row.get("skill_sha256", skill_hash) != skill_hash
                or hashlib.sha256(subject).hexdigest() != case.get("subject_sha256", skill_hash)
            ):
                raise ValueError("Semantic review fixture does not match its phase/source artifact")
    result_path = location / "result.json"
    result_digest = None
    if result_path.exists():
        content = semantic_bytes(result_path, directory)
        result_digest = hashlib.sha256(content).hexdigest()
        result = json.loads(content)
        if result.get("status") != row["status"] or any(
            result.get(key, row.get(key)) != row.get(key)
            for key in ("suite", "scenario", "phase", "client", "model", "artifact_revision", "skill_sha256")
        ):
            raise ValueError("Semantic review result does not match the matrix phase")
    if "result_sha256" in finding and finding["result_sha256"] != result_digest:
        raise ValueError("Semantic review result file digest does not match")
    if result_digest is None and not (
        finding.get("matrix_result_sha256") and row["status"] not in {"completed", "running", "pending", "ready"}
    ):
        raise ValueError("Absent phase evidence requires a hash-bound terminal matrix row")
    return row, result_digest


def add_semantic_review(data, directory, path, raw, *, compact_summary=False):
    directory = directory.resolve()
    review, plan = load(path), load(directory / "run.json")
    if "evidence_root" in review and Path(review["evidence_root"]) != directory:
        raise ValueError("Semantic review belongs to a different evidence root")
    version = plan["artifacts"]["current"]["package_version"]
    kind = review.get("kind", "review")
    if kind not in {"review", "correction"}:
        raise ValueError("Unknown semantic review kind")
    for finding in semantic_findings(review):
        matches = [
            (suite, scenario)
            for key in CASES
            for suite, scenario in [key.split(":", 1)]
            if scenario == finding["case"] and finding.get("suite", suite) == suite
        ]
        if len(matches) != 1 or not str(finding["run"]).isdigit():
            raise ValueError("Ambiguous semantic review case")
        suite, scenario = matches[0]
        number = int(finding["run"])
        if not 1 <= number <= len(plan["runners"]):
            raise ValueError("Unknown semantic review runner")
        phase = finding.get("phase", 1)
        if type(phase) is not int or not 1 <= phase <= 4:
            raise ValueError("Invalid semantic review phase")
        location = directory / suite / scenario / f"{number:03d}" / f"phase-{phase}"
        response = location / "response.json"
        case = LEGACY_IDS.get(scenario, scenario) if suite == "cli" else f"{suite}-{scenario}"
        pair = plan["runners"][number - 1]
        if any(finding.get(key, pair[key]) != pair[key] for key in ("client", "model")):
            raise ValueError("Semantic review runner does not match the observed client/model")
        if "evidence" in finding and (
            Path(finding["evidence"]) != location or Path(finding["evidence"]).resolve() != location
        ):
            raise ValueError("Semantic review evidence does not match its phase")
        if finding["semantic_status"] not in {"pass", "fail", "uncertain"}:
            raise ValueError("Unknown semantic review status")
        if finding.get("overall_review_status", "uncertain") not in {"pass", "fail", "uncertain"}:
            raise ValueError("Unknown overall review status")
        row, terminal_digest = bind_semantic_phase(
            directory, plan, review, finding, suite, scenario, pair, location, phase
        )
        session = []
        if (location / "prompt.txt").exists():
            prompt = semantic_bytes(location / "prompt.txt", directory).decode()
            session.append({"role": "user", "text": prompt if raw else clean_prompt(prompt)})
        if finding.get("response_sha256") is None:
            if response.exists() or finding["semantic_status"] != "uncertain" or row["status"] == "completed":
                raise ValueError("An absent response requires matching terminal evidence and an uncertain review")
            session.append(
                {
                    "role": "missing",
                    "text": f"阶段 {phase} 没有最终答复，不能推断语义通过或失败；状态：" + row["status"],
                }
            )
        else:
            if hashlib.sha256(semantic_bytes(response, directory)).hexdigest() != finding["response_sha256"]:
                raise ValueError("Semantic review does not match the actual response file")
            answer = json.loads(semantic_bytes(response, directory))["text"]
            if (
                finding.get("response_text_sha256")
                and hashlib.sha256(answer.encode()).hexdigest() != finding["response_text_sha256"]
            ):
                raise ValueError("Semantic review response text digest does not match")
            session.append({"role": "assistant", "text": answer if raw else clean_text(answer)})
        reason = finding.get("reason")
        if reason == "fixture_conflict" and finding["semantic_status"] != "uncertain":
            raise ValueError("Fixture conflicts must remain uncertain rather than blame the model")
        prefix = "semantic-correction:" if kind == "correction" else "semantic:"
        source = prefix + fingerprint({"finding": finding, "scope": review.get("scope")})
        corrections = [
            trial["source_run"]
            for trial in data["cases"].get(case, [])
            if kind == "correction"
            and trial.get("source_run", "").startswith("semantic:")
            and trial.get("response_sha256") == finding["response_sha256"]
        ]
        if kind == "correction" and not corrections:
            raise ValueError("Semantic correction has no matching prior reviewed response")
        note = f"仅复核阶段 {phase} 的独立记录；不补写其他阶段聊天，完整原始运行另列。\n"
        if kind == "correction":
            note = f"阶段 {phase} 追加更正，已有记录与评论保持不变。\n"
        if reason == "fixture_conflict":
            note += "夹具与真实接口存在冲突，结论待核对；不能据此归因于模型。\n"
        append_trial(
            data,
            case,
            {
                "case": case,
                "model": f"{pair['client']} · {pair['model']} · "
                + f"阶段 {phase} "
                + ("语义更正" if kind == "correction" else "语义复核"),
                "state": "fail"
                if (finding["semantic_status"] == "fail" or finding.get("overall_review_status") == "fail")
                else "pending",
                "source_run": source,
                **({"semantic_reason": reason} if reason else {}),
                **({"corrects_source_runs": corrections} if corrections else {}),
                "package_version": version,
                "response_sha256": finding.get("response_sha256"),
                **({"terminal_result_sha256": terminal_digest} if terminal_digest else {}),
                **({"finding_code": finding["finding_code"]} if finding.get("finding_code") else {}),
                "semantic_status": finding["semantic_status"],
                "review_phase": phase,
                **(
                    {"matrix_result_sha256": finding["matrix_result_sha256"]}
                    if finding.get("matrix_result_sha256")
                    else {}
                ),
                **(
                    {"overall_review_status": finding["overall_review_status"]}
                    if finding.get("overall_review_status")
                    else {}
                ),
                **(
                    {"action_safety_status": finding["action_safety_status"]}
                    if finding.get("action_safety_status")
                    else {}
                ),
                "note": ("摘要；完整对话和工具输入输出保留在原始轨迹。\n" if compact_summary and not raw else "")
                + note
                + (finding["note"] if raw else clean_text(finding["note"])),
                "sessions": [] if compact_summary and not raw else [session],
                **({"summary_only": True} if compact_summary and not raw else {}),
            },
        )


def atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def aggregate(
    base_raw,
    base_latest,
    output,
    *,
    plan,
    matrices=(),
    probes=(),
    external_native_evidence=False,
    compact_new_summary=False,
):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    terminal = [directory for directory in matrices if (directory / "matrix.json").is_file()]
    waiting = [str(directory) for directory in matrices if directory not in terminal]
    payloads, report = {}, {"waiting_for_terminal_matrix": waiting, "terminal_sources": [str(p) for p in terminal]}
    for raw, baseline, filename in ((True, base_raw, "raw.json"), (False, base_latest, "latest.json")):
        base = load(baseline)
        validate(base, raw)
        target = output / filename
        data = load(target) if target.exists() else copy.deepcopy(base)
        assert_preserved(base, data, raw)
        before = copy.deepcopy(data)
        add_plan(data, load(plan))
        for directory in terminal:
            add_matrix(
                data,
                directory,
                raw,
                native_artifacts=output.resolve() / "evidence" if raw and external_native_evidence else None,
                compact_summary=compact_new_summary,
            )
            add_semantic_reviews(data, directory, raw, compact_summary=compact_new_summary)
        for directory in probes:
            add_probe(
                data,
                directory,
                raw,
                native_artifacts=output.resolve() / "evidence" if raw and external_native_evidence else None,
            )
        report[filename] = {
            "preserved_base_anchors": assert_preserved(base, data, raw),
            "preserved_previous_anchors": assert_preserved(before, data, raw),
            "cases": len(data["cases"]),
            "trials": sum(map(len, data["cases"].values())),
        }
        payloads[filename] = validate(data, raw)
        report[filename].update(bytes=len(payloads[filename]), sha256=hashlib.sha256(payloads[filename]).hexdigest())
    report["local_diagnostic_artifacts"] = native_artifact_manifest(output.resolve() / "evidence")
    # Validate both complete files before replacing either local output.
    for filename, content in payloads.items():
        atomic_write(output / filename, content)
    atomic_write(output / "aggregation.json", json.dumps(report, ensure_ascii=False, indent=2).encode() + b"\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-raw", required=True, type=Path)
    parser.add_argument("--base-latest", required=True, type=Path)
    parser.add_argument(
        "--plan", required=True, type=Path, help="Current private run.json; all catalog cases remain planned"
    )
    parser.add_argument("--matrix", action="append", default=[], type=Path, help="Append only when matrix.json exists")
    parser.add_argument(
        "--probe", action="append", default=[], type=Path, help="Terminal preflight-summary.json directory"
    )
    parser.add_argument(
        "--external-native-evidence",
        action="store_true",
        help="Externalize only new raw native diagnostics into private immutable files",
    )
    parser.add_argument(
        "--compact-new-summary",
        action="store_true",
        help="Summarize only new summary trials; raw dialogue and all old anchors remain unchanged",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--follow", action="store_true", help="Refresh locally until all supplied matrices are terminal"
    )
    args = parser.parse_args()
    while True:
        report = aggregate(
            args.base_raw,
            args.base_latest,
            args.output,
            plan=args.plan,
            matrices=args.matrix,
            probes=args.probe,
            external_native_evidence=args.external_native_evidence,
            compact_new_summary=args.compact_new_summary,
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not args.follow or not report["waiting_for_terminal_matrix"]:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
