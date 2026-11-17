#!/usr/bin/env python3
"""Append terminal synthetic runs to private board snapshots without moving comments."""

import argparse
import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from publish_snapshot import CASES, LEGACY_IDS, MAX_SNAPSHOT_BYTES, snapshot
from synthetic_transcript import MAX_TRACE_BYTES, clean_prompt, clean_text, native_evidence, native_partial_replies

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


def add_matrix(data, directory, raw):
    matrix = load(directory / "matrix.json")
    if not isinstance(matrix.get("results"), list) or matrix.get("simulation_only") is not True:
        raise ValueError("Expected a terminal synthetic matrix with results")
    projected = snapshot(directory, raw=raw, include_dialogue=not raw, include_planned=True)
    source = "matrix:" + fingerprint(matrix)
    for key in ("labels", "groups", "criteria"):
        for case, value in projected.get(key, {}).items():
            data.setdefault(key, {}).setdefault(case, value)
    for case, trials in projected["cases"].items():
        for number, trial in enumerate(trials):
            trial = copy.deepcopy(trial)
            trial.update(source_run=f"{source}:{case}:{number}", package_version=projected["version"])
            trial["note"] = f"运行：{directory.name}；安装包：{projected['version']}\n" + trial.get("note", "")
            append_trial(data, case, trial)


def add_probe(data, directory, raw):
    summary = load(directory / "preflight-summary.json")
    if summary.get("status") in {None, "running", "pending"}:
        raise ValueError("Connectivity probe is not terminal")
    location = (directory / summary["evidence"]).resolve()
    if not location.is_relative_to(directory.resolve()):
        raise ValueError("Probe evidence escaped its directory")
    prompt = (location / "prompt.txt").read_text()
    session = [{"role": "user", "text": prompt if raw else clean_prompt(prompt)}]
    if raw:
        session.extend(native_evidence(location, directory.resolve()))
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
            "source_run": "probe:" + fingerprint({"summary": summary, "run": directory.name}),
            "sessions": [session],
        },
    )


def add_semantic_reviews(data, directory, raw):
    path = directory / "manual-semantic-review.json"
    if not path.is_file():
        return
    review, plan = load(path), load(directory / "run.json")
    version = plan["artifacts"]["current"]["package_version"]
    for finding in review["findings"]:
        matches = [
            (suite, scenario) for key in CASES for suite, scenario in [key.split(":", 1)] if scenario == finding["case"]
        ]
        if len(matches) != 1 or not str(finding["run"]).isdigit():
            raise ValueError("Ambiguous semantic review case")
        suite, scenario = matches[0]
        number = int(finding["run"])
        if not 1 <= number <= len(plan["runners"]):
            raise ValueError("Unknown semantic review runner")
        location = directory / suite / scenario / f"{number:03d}" / "phase-1"
        response = location / "response.json"
        if hashlib.sha256(response.read_bytes()).hexdigest() != finding["response_sha256"]:
            raise ValueError("Semantic review does not match the actual response file")
        case = LEGACY_IDS.get(scenario, scenario) if suite == "cli" else f"{suite}-{scenario}"
        pair = plan["runners"][number - 1]
        prompt, answer = (location / "prompt.txt").read_text(), load(response)["text"]
        if finding["semantic_status"] not in {"pass", "fail"}:
            raise ValueError("Unknown semantic review status")
        append_trial(
            data,
            case,
            {
                "case": case,
                "model": f"{pair['client']} · {pair['model']} · 语义复核",
                "state": "fail" if finding["semantic_status"] == "fail" else "pending",
                "source_run": "semantic:" + fingerprint({"finding": finding, "scope": review.get("scope")}),
                "package_version": version,
                "response_sha256": finding["response_sha256"],
                "semantic_status": finding["semantic_status"],
                "note": "仅复核这条实际答复，不代表整轮通过。原始运行记录保持不变。\n"
                + (finding["note"] if raw else clean_text(finding["note"])),
                "sessions": [
                    [
                        {"role": "user", "text": prompt if raw else clean_prompt(prompt)},
                        {"role": "assistant", "text": answer if raw else clean_text(answer)},
                    ]
                ],
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


def aggregate(base_raw, base_latest, output, *, plan, matrices=(), probes=()):
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
            add_matrix(data, directory, raw)
            add_semantic_reviews(data, directory, raw)
        for directory in probes:
            add_probe(data, directory, raw)
        report[filename] = {
            "preserved_base_anchors": assert_preserved(base, data, raw),
            "preserved_previous_anchors": assert_preserved(before, data, raw),
            "cases": len(data["cases"]),
            "trials": sum(map(len, data["cases"].values())),
        }
        payloads[filename] = validate(data, raw)
        report[filename].update(bytes=len(payloads[filename]), sha256=hashlib.sha256(payloads[filename]).hexdigest())
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--follow", action="store_true", help="Refresh locally until all supplied matrices are terminal"
    )
    args = parser.parse_args()
    while True:
        report = aggregate(
            args.base_raw, args.base_latest, args.output, plan=args.plan, matrices=args.matrix, probes=args.probe
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not args.follow or not report["waiting_for_terminal_matrix"]:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
