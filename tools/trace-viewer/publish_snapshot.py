#!/usr/bin/env python3
"""Project synthetic matrix progress into a shareable viewer snapshot."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from time import sleep

from synthetic_transcript import synthetic_sessions

CASES = {
    "cli:recover-original-key": "找回原 Key",
    "cli:approved-enrollment-no-group": "首次创建 Key",
    "cli:reset-personal-key": "明确要求重置 Key",
}
MODELS = {
    "claude-opus-4-8": "Claude Opus 4.8",
    "Claude Sonnet 5.5": "Claude Sonnet 5.5",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "glm-5.3": "GLM-5.3",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "Grok 4.7": "Grok 4.7",
    "kimi-k3": "Kimi K3",
    "minimax-m3": "MiniMax-M3",
}
PHASES = {1: "安装与发码", 2: "确认后保存", 3: "新会话查询"}
DETAILS = {
    "installed": "安装完整 Skill",
    "exactly_one_enrollment": "只发出一条绑定请求",
    "purpose_matches_request": "绑定用途正确",
    "uses_own_chat_account": "群内指令由本人发送",
    "connection_created": "保存连接",
    "correct_key_saved": "保存预期凭证",
    "saved_connection_discovered": "新会话查找并选择连接",
    "resources_reported": "读回并报告资源",
    "no_mutation": "执行只读操作",
}


def verdict(phases):
    for row in phases:
        stage = PHASES.get(row.get("phase"), "回归测试")
        status = row.get("status")
        if status == "blocked_by_model_preflight":
            return "blocked", "模型未通过预检"
        if status == "timeout":
            return "timeout", stage + "超时"
        if status != "completed":
            return "blocked", stage + "未完成"
        if row.get("observable_pass") is not True:
            checks = row.get("checks", {})
            failed = "、".join(label for key, label in DETAILS.items() if checks.get(key) is False)
            return "fail", stage + "未通过" + ("，" + failed if failed else "")
    return "pass", "已完成全部测试阶段"


def snapshot(evidence: Path, *, include_dialogue=False):
    plan = json.loads((evidence / "run.json").read_text())
    if plan.get("simulation_only") is not True:
        raise ValueError("Only synthetic matrix exports can be published")
    version = plan["artifacts"]["current"]["package_version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{16}", version):
        raise ValueError("Expected a packaged Skill revision")

    # Ignore a line still being written. The matrix appends one case's completed stages together.
    progress = (evidence / "progress.jsonl").read_bytes()
    rows = progress.split(b"\n")[:-1]
    review_cases = [case for case in plan["cases"] if case in CASES]
    original_cases = {case.split(":", 1)[1]: case for case in review_cases}
    tested_models = {(row["client"], row["model"]): index for index, row in enumerate(plan["runners"], 1)}
    results = {}
    for line in rows:
        row = json.loads(line)
        case = original_cases.get(row.get("scenario"))
        pair = (row.get("client"), row.get("model"))
        if case is not None and pair in tested_models:
            results.setdefault((case, pair), []).append(row)
    labels = {}
    cases = {}
    for index, case in enumerate(review_cases, 1):
        safe_id = f"case-{index}"
        for pair, model_number in tested_models.items():
            phases = results.get((case, pair), [])
            # A terminal preflight row stands alone. Wait for all phase rows when the final line is partial.
            if not phases or (len(phases) < 3 and phases[0]["status"] != "blocked_by_model_preflight"):
                continue
            state, note = verdict(phases)
            model = MODELS.get(pair[1], f"模型 {model_number}")
            if pair[0] == "Official Codex":
                model = "官方 Codex"
            observed = [
                {
                    "phase": row["phase"],
                    **({"turns": row["model_turns"]} if type(row.get("model_turns")) is int else {}),
                    **(
                        {"duration_seconds": row["duration_seconds"]}
                        if type(row.get("duration_seconds")) in (int, float)
                        else {}
                    ),
                }
                for row in phases
                if row.get("phase") in PHASES and row.get("status") in {"completed", "timeout"}
            ]
            labels[safe_id] = CASES[case]
            cases.setdefault(safe_id, []).append(
                {
                    "case": safe_id,
                    "model": model,
                    "state": state,
                    "note": note,
                    "sessions": (
                        synthetic_sessions(evidence, case.split(":", 1)[1], model_number, phases)
                        if include_dialogue
                        else []
                    ),
                    "metrics": {"phases": observed},
                }
            )
    title = "在线评测 · 合成测例" if include_dialogue else "在线评测 · 合成测例摘要"
    return {"schema": 1, "title": title, "version": version, "labels": labels, "cases": cases}


def publish(evidence: Path, target: Path, *, include_dialogue=False):
    data = snapshot(evidence, include_dialogue=include_dialogue)
    if not data["cases"]:
        return None
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
    if len(content) > 1_048_576:
        raise ValueError("Online trace snapshot exceeds its 1 MiB limit")
    if target.exists() and target.read_bytes() == content:
        return False
    fd, name = tempfile.mkstemp(dir=target.parent)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(path, target)
    finally:
        path.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--follow", action="store_true", help="Refresh the snapshot every 2 seconds until matrix.json")
    parser.add_argument(
        "--include-dialogue", action="store_true", help="Include deidentified synthetic chat and tool steps"
    )
    parser.add_argument("--on-update", type=Path, help="Private JSON argv array for transferring the snapshot")
    args = parser.parse_args()
    if args.on_update:
        transport = json.loads(args.on_update.read_text())
        if (
            not isinstance(transport, list)
            or not transport
            or not all(isinstance(value, str) and value for value in transport)
        ):
            parser.error("--on-update must contain a nonempty JSON argv array")
    else:
        transport = None
    transfer_started = False
    while True:
        if args.follow and not (args.evidence / "progress.jsonl").is_file():
            sleep(2)
            continue
        updated = publish(args.evidence, args.target, include_dialogue=args.include_dialogue)
        if transport and updated is not None and (updated or not transfer_started):
            subprocess.run([*transport, str(args.target)], check=True)
            transfer_started = True
        if not args.follow or (args.evidence / "matrix.json").is_file():
            break
        sleep(2)


if __name__ == "__main__":
    main()
