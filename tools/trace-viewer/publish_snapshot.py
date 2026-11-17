#!/usr/bin/env python3
"""Project synthetic matrix progress into a shareable viewer snapshot."""

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from time import sleep

from synthetic_transcript import read_local, synthetic_sessions

GROUPS = {
    "安装与绑定": {
        "link-only": "只分享 Skill 链接",
        "install-from-link": "从发现链接安装",
        "install-from-zip": "从 ZIP 链接安装",
        "install-with-key": "安装并使用已有 Key",
        "no-key": "没有 Key 时保存服务",
        "approved-enrollment": "用户授权后绑定",
        "approved-enrollment-no-group": "首次创建 Key",
        "recover-original-key": "找回原 Key",
        "reset-personal-key": "明确要求重置 Key",
    },
    "查询与工作台": {
        "recover": "新会话续用已保存的连接",
        "panel": "打开个人资源台",
        "telemetry": "查询利用率并刷新授权",
        "intent-query": "查找两张空闲 P800 卡",
        "intent-model-find": "跨群查找空闲 P800 或 P900",
    },
    "资源操作": {
        "intent-exact": "锁定指定加速卡",
        "intent-ambiguous": "澄清同名节点",
        "intent-choose": "代选同机加速卡并锁定",
        "intent-whole": "整机群内请求单卡",
        "intent-book": "预约整机",
        "intent-claim": "按原时长领取预约",
        "intent-model-allocate": "跨群按型号选卡并锁定",
        "intent-model-unknown-node": "缺少 P900 型号证据时停下",
        "intent-model-p800-unknown-node": "缺少 P800 型号证据时停下",
        "intent-model-whole-machine": "按 P900 型号锁定整机",
        "intent-model-p800-whole-machine": "按 P800 型号锁定整机",
        "intent-renew": "续期原有占用",
        "intent-release": "只释放指定卡",
        "intent-write-unknown": "写入结果不明时查证",
        "intent-stale": "快照过期时重新核对",
    },
}
CASES = {f"cli:{key}": (group, label) for group, labels in GROUPS.items() for key, label in labels.items()}
LEGACY_IDS = {
    "approved-enrollment-no-group": "case-1",
    "recover-original-key": "case-2",
    "reset-personal-key": "case-3",
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
ENROLLMENT_PHASES = ("安装与发码", "确认后保存", "新会话查询")
PHASES = {
    **dict.fromkeys(LEGACY_IDS, ENROLLMENT_PHASES),
    "approved-enrollment": ENROLLMENT_PHASES[:2],
    "panel": ("生成工作台链接", "确认后在新会话打开"),
}
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
    "no_bundle_download": "仅共享链接时未下载",
    "installation_verified": "校验安装包",
    "service_saved": "保存已验证服务",
    "no_authenticated_calls": "未擅自使用凭证",
    "resource_read": "实际查询资源",
    "original_key_preserved": "保留已有 Key",
    "browser_only_fixed_home": "浏览器使用固定入口",
    "credentials_scoped": "凭证使用范围正确",
    "one_retry_with_same_selector": "仅重试同一利用率查询一次",
    "no_resource_mutation": "未改动资源状态",
    "reports_utilization_and_sample_age": "报告利用率及采样时间",
    "single_issuance": "只生成一次工作台链接",
    "delivered_once_privately": "私下交付工作台链接",
    "pending_human_confirmation": "等待用户在浏览器确认",
    "no_ticket_reissued": "续用时未重新生成链接",
    "fixed_home_restored": "固定入口恢复浏览器会话",
    "both_candidate_groups_examined": "检查两个候选资源群",
    "exactly_one_correct_cli_write": "通过 CLI 准确提交一次操作",
    "verified_before_and_after": "操作前后核对实际占用",
    "zero_write_attempts": "保持只读",
    "asks_which_target": "澄清具体资源",
    "unknown_outcome_reported": "如实说明操作结果仍未确认",
    "uncertain_availability_reported": "提示资源状态已过期",
}
PREFLIGHT_NOTES = {
    "ready": "模型可进入测例",
    "timeout": "模型连接预检超时",
    "empty_response": "模型连接预检无响应内容",
    "runner_error": "运行工具预检失败",
    "runner_protocol_error": "运行工具响应格式异常",
    "budget_exhausted": "模型连接预检达到限制",
}
MAX_METADATA_BYTES = 262_144
MAX_PROGRESS_BYTES = 8_388_608
MAX_SNAPSHOT_BYTES = 1_048_576


def verdict(phases, stages):
    for stage, row in zip(stages, phases, strict=True):
        status = row.get("status")
        if status == "timeout":
            return "timeout", stage + "超时"
        if status != "completed":
            return "blocked", stage + "未完成"
        if row.get("observable_pass") is not True:
            checks = row.get("checks", {})
            failed = "、".join(label for key, label in DETAILS.items() if checks.get(key) is False)
            return "fail", stage + "未通过" + ("，" + failed if failed else "")
    return "pass", "已完成全部测试阶段"


def model_name(pair, number):
    if pair[0] == "Official Codex":
        return "官方 Codex"
    return MODELS.get(pair[1], f"模型 {number}")


def phase_metrics(phases):
    observed = []
    for row in phases:
        if row["status"] not in {"completed", "timeout"}:
            continue
        metrics = {"phase": row["phase"]}
        turns = row.get("model_turns")
        if type(turns) is int and 0 <= turns <= 1000:
            metrics["turns"] = turns
        elapsed = row.get("duration_seconds")
        if type(elapsed) in (int, float) and 0 <= elapsed <= 86_400 and math.isfinite(elapsed):
            metrics["duration_seconds"] = elapsed
        actions = row.get("simulated_user_actions", [])
        if isinstance(actions, list):
            action_count = sum(action in ("chat_binding_confirmation", "browser_consent") for action in actions)
            if action_count:
                metrics["simulated_user_actions"] = action_count
        observed.append(metrics)
    return observed


def snapshot(evidence: Path, *, include_dialogue=False):
    evidence = evidence.resolve()
    plan = json.loads(read_local(evidence / "run.json", evidence, MAX_METADATA_BYTES))
    if plan.get("simulation_only") is not True:
        raise ValueError("Only synthetic matrix exports can be published")
    artifacts = plan["artifacts"]
    version = artifacts["current"]["package_version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{16}", version):
        raise ValueError("Expected a packaged Skill revision")

    # Ignore a line still being written. The matrix appends one case's completed stages together.
    progress = read_local(evidence / "progress.jsonl", evidence, MAX_PROGRESS_BYTES)
    review_cases = dict.fromkeys(case for case in plan["cases"] if case in CASES)
    tested_models = {(row["client"], row["model"]): index for index, row in enumerate(plan["runners"], 1)}
    results = {}
    preflight = {}
    for line in progress.split("\n")[:-1]:
        row = json.loads(line)
        case = f"{row.get('suite')}:{row.get('scenario')}"
        pair = (row.get("client"), row.get("model"))
        if case not in review_cases or pair not in tested_models:
            continue
        if row.get("status") == "blocked_by_model_preflight":
            preflight[pair] = row.get("preflight_status")
            continue
        artifact = artifacts.get("panel", {}) if case == "cli:panel" else artifacts["current"]
        if artifact.get("revision") and row.get("artifact_revision") != artifact["revision"]:
            continue
        result = results.setdefault((case, pair), {})
        phase = row.get("phase")
        if type(phase) is int:
            if phase in result:
                raise ValueError("Duplicate synthetic matrix phase")
            result[phase] = row
    try:
        probe_text = read_local(evidence / "preflight.json", evidence, MAX_METADATA_BYTES)
    except FileNotFoundError:
        probe_text = ""
    probes = json.loads(probe_text) if probe_text.endswith("\n") else []
    for row in probes:
        pair = (row.get("client"), row.get("model"))
        if pair in tested_models:
            preflight[pair] = row.get("status")

    labels = {}
    groups = {}
    cases = {}
    for case in review_cases:
        scenario = case.split(":", 1)[1]
        safe_id = LEGACY_IDS.get(scenario, scenario)
        stages = PHASES.get(scenario, ("回归测试",))
        phase_ids = range(1, len(stages) + 1)
        for pair, model_number in tested_models.items():
            result = results.get((case, pair), {})
            # The last phase row may still be getting appended.
            if result.keys() != set(phase_ids):
                continue
            phases = [result[phase] for phase in phase_ids]
            state, note = verdict(phases, stages)
            groups[safe_id], labels[safe_id] = CASES[case]
            cases.setdefault(safe_id, []).append(
                {
                    "case": safe_id,
                    "model": model_name(pair, model_number),
                    "state": state,
                    "note": note,
                    "sessions": (
                        synthetic_sessions(evidence, scenario, model_number, phases) if include_dialogue else []
                    ),
                    "metrics": {"phases": phase_metrics(phases)},
                }
            )
    if preflight and review_cases:
        labels["model-preflight"] = "模型连接预检"
        groups["model-preflight"] = "运行环境"
        cases["model-preflight"] = [
            {
                "case": "model-preflight",
                "model": model_name(pair, number),
                "state": "pass" if preflight[pair] == "ready" else "blocked",
                "note": PREFLIGHT_NOTES.get(preflight[pair], "模型未通过预检"),
                "sessions": [],
            }
            for pair, number in tested_models.items()
            if pair in preflight
        ]
    title = "在线评测 · 合成测例" if include_dialogue else "在线评测 · 合成测例摘要"
    return {"schema": 1, "title": title, "version": version, "labels": labels, "groups": groups, "cases": cases}


def publish(evidence: Path, target: Path, *, include_dialogue=False):
    data = snapshot(evidence, include_dialogue=include_dialogue)
    if not data["cases"]:
        return None
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
    if len(content) > MAX_SNAPSHOT_BYTES:
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
