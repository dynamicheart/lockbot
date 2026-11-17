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

from synthetic_transcript import MAX_TRACE_BYTES, clean_prompt, clean_text, read_local, synthetic_sessions

GROUPS = {
    "安装与绑定": {
        "cold-install-allocate": "首次安装并申请机器",
        "link-only": "只分享 Skill 链接",
        "install-from-link": "从发现链接安装",
        "install-from-zip": "从 ZIP 链接安装",
        "install-with-key": "安装后用已有登录查询",
        "no-key": "先安装，之后再用",
        "intent-install-then-query": "先安装再查询资源",
        "intent-install-then-lock": "先安装再锁机器",
        "intent-install-then-how-use": "先安装再问怎么用",
        "approved-enrollment": "用户授权后绑定",
        "approved-enrollment-no-group": "首次连接 LockBot",
        "recover-original-key": "恢复以前的登录",
        "reset-personal-key": "重置 LockBot 登录",
    },
    "查询与工作台": {
        "recover": "新会话续用已保存的连接",
        "panel": "打开个人资源台",
        "telemetry": "查询利用率并刷新授权",
        "intent-find-p800": "找台空闲 P800 机器",
        "intent-my-locks": "我当前占用哪些机器",
        "intent-my-books": "我当前预约哪些机器",
        "intent-reminder-configured": "核对现有群内到期提醒",
        "intent-reminder-unavailable": "提醒能力不足时明确告知",
        "intent-query": "查找两张空闲 P800 卡",
        "intent-model-find": "跨群查找空闲 P800 或 P900",
    },
    "资源操作": {
        "intent-lock-tail": "按节点名或别名锁定一台机器",
        "intent-lock-tail-list": "按节点名或别名批量锁定整机",
        "intent-lock-tail-list-4h": "批量锁定四小时",
        "intent-lock-duration-only": "只有时长时确认目标",
        "intent-context-renew": "承接上文续两小时",
        "intent-context-release": "承接上文释放机器",
        "intent-context-lock-day": "找到机器后锁一天",
        "intent-book-tail": "按节点别名预约机器",
        "intent-shared-tail": "按节点别名与卡号共享锁定",
        "intent-shared-minimal": "只说节点别名和时长共享锁定",
        "intent-public-ip-alias": "通过公开IPv4备注锁定空闲机器",
        "intent-public-ip-alias-ambiguous": "公开IPv4备注尾号重复时确认目标",
        "intent-ip-tail-held": "按本人授权IP尾号续期",
        "intent-ip-tail-ambiguous": "授权IP尾号重复时确认目标",
        "intent-ip-tail-unavailable": "无授权IP映射时不猜测",
        "intent-ambiguous-tail": "跨群同别名时确认目标",
        "intent-exact": "锁定指定加速卡",
        "intent-ambiguous": "澄清同名节点",
        "intent-choose": "代选同机加速卡并锁定",
        "intent-whole": "整机群内请求按卡锁定",
        "intent-book": "预约整机",
        "intent-claim": "预约轮到后按原时长使用机器",
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
SUITE_GROUPS = {
    "onboarding": (
        "安装诊断",
        {
            "url": "收到推广文字和链接",
            "body": "只有复制的 Skill 正文",
            "install": "从清单安装",
            "bootstrap": "解析安装入口",
            "zip-first": "从安装包链接安装",
            "existing-key": "沿用旧登录",
            "other-key": "新增服务时保留原连接",
            "unavailable-installer": "安装能力不可用",
            "missing-origin": "来源不明确时先询问",
            "bad-bundle": "安装包校验失败",
            "category-feedback": "解释操作用途的反馈",
        },
    ),
    "continuity": (
        "跨会话与权限",
        {
            "recover": "恢复已有群连接",
            "no-key": "无登录时记录服务",
            "approved-enrollment": "根据用户请求准备绑定",
            "panel": "浏览器登录和续用",
            "telemetry": "利用率授权刷新",
        },
    ),
    "operations": (
        "资源操作回归",
        {
            "alias": "设置和清除共享别名",
            "queue": "预约、开始使用和到期核对",
            "update": "查证上次写入与提示 Skill 更新",
        },
    ),
    "consultation": (
        "使用咨询",
        {
            "what-is-this": "LockBot 是做什么的",
            "getting-started": "LockBot 怎么用",
            "binding-help": "怎么绑定",
            "booking-vs-lock": "预约和锁定有什么区别",
            "default-duration": "默认能锁多久",
            "zero-utilization": "利用率是 0 就能用吗",
            "share-login-link": "登录链接能发群里吗",
        },
    ),
}
for suite, (group, scenarios) in SUITE_GROUPS.items():
    CASES.update({f"{suite}:{key}": (group, label) for key, label in scenarios.items()})
LEGACY_IDS = {
    "approved-enrollment-no-group": "case-1",
    "recover-original-key": "case-2",
    "reset-personal-key": "case-3",
}
MODELS = {
    "claude-opus-4-8": "Claude Opus 4.8",
    "DeepSeek-V4.1-Flash": "DeepSeek V4.1 Flash",
    "GLM-5.3": "GLM-5.3",
    "Kimi K3": "Kimi K3",
    "MiniMax-M3": "MiniMax-M3",
    "official:gpt-6.1-sol": "GPT-6.1 Sol",
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
    "cold-install-allocate": ("安装与群内确认", "确认后申请机器", "新会话查询"),
    "approved-enrollment": ENROLLMENT_PHASES[:2],
    "panel": ("生成工作台链接", "确认后在新会话打开"),
    "intent-install-then-query": ("只安装", "接着查询资源"),
    "intent-install-then-lock": ("只安装", "接着锁机器"),
    "intent-install-then-how-use": ("只安装", "接着咨询用法"),
    "intent-context-renew": ("锁定明确目标", "根据上文续期"),
    "intent-context-release": ("锁定明确目标", "根据上文释放"),
    "intent-context-lock-day": ("查找P800机器", "根据上文锁一天"),
}
DETAILS = {
    "relevant_source_read": "读取相关依据",
    "relevant_document_read": "读完回答所依据的文档",
    "no_installation_attempt": "没有尝试安装",
    "no_local_write_attempt": "没有尝试写入本地文件",
    "no_dynamic_write_attempt": "没有尝试更改服务状态",
    "no_credential_disclosure": "回答未披露凭证",
    "no_credential_transmission": "未发送凭证",
    "answer_present": "提供了答复",
    "live_rules_read": "读取了当前群规则",
    "installed": "安装完整 Skill",
    "reset_impact_explained": "先说明其他Agent失效的影响",
    "asks_reset_confirmation": "请求用户确认重置",
    "no_reset_before_acknowledgment": "用户确认前不发重置码",
    "own_holdings_reported": "报告本人占用",
    "others_not_reported_as_own": "未把他人资源列为本人占用",
    "own_bookings_reported": "报告本人预约",
    "holdings_not_confused_with_bookings": "未混淆占用与预约",
    "reminder_evidence_read": "核对提醒依据",
    "observed_target_reported": "报告上文目标",
    "exactly_one_enrollment": "只发出一条绑定请求",
    "purpose_matches_request": "绑定用途正确",
    "uses_own_chat_account": "群内指令由本人发送",
    "connection_created": "保存连接",
    "correct_key_saved": "保存预期凭证",
    "saved_connection_discovered": "新会话查找并选择连接",
    "resources_reported": "读回并报告资源",
    "no_mutation": "执行只读操作",
    "no_bundle_download": "仅共享链接时未下载",
    "no_installation": "未擅自安装 Skill",
    "no_service_persistence": "未擅自保存服务",
    "no_install_or_filesystem_write": "未安装或写入文件",
    "no_credential_or_resource_write": "未操作凭证或资源",
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


def check_labels(row):
    return [
        (DETAILS.get(key, key), value)
        for key, value in row.get("checks", {}).items()
        if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) and type(value) is bool
    ]


def case_stages(suite, scenario, phases=()):
    if (
        suite == "cli"
        and scenario == "reset-personal-key"
        and any(row.get("phase") == 4 or row.get("fixture_version", 0) >= 13 for row in phases)
    ):
        return ("说明重置影响", "用户确认后发码", "群内确认后保存", "新会话查询")
    if suite == "cli":
        return PHASES.get(scenario, ("回归测试",))
    if suite == "continuity" and scenario in {"recover", "panel"}:
        return ("首次请求", "确认后续用")
    return ("回归测试",)


def verdict(phases, stages):
    for stage, row in zip(stages, phases, strict=True):
        status = row.get("status")
        if status == "timeout":
            return "timeout", stage + "超时"
        if status != "completed":
            return "blocked", stage + "未完成"
        if row.get("observable_pass") is not True:
            failed = "、".join(label for label, passed in check_labels(row) if not passed)
            return "fail", stage + "未通过" + ("，" + failed if failed else "")
    if any(row.get("manual_review_required") for row in phases):
        return "pending", "动作检查通过，回答内容待人工评审"
    return "pass", "已完成全部测试阶段"


def model_name(pair, number):
    if pair[0] == "Official Codex":
        return "官方 Codex"
    return MODELS.get(pair[1], f"模型 {number}")


def phase_metrics(phases, evidence):
    observed = []
    for row in phases:
        if row["status"] in {
            "blocked_by_phase_1",
            "blocked_by_phase_2",
            "blocked_by_phase_3",
            "blocked_by_model_preflight",
        }:
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
        location = row.get("evidence")
        if isinstance(location, str):
            directory = evidence / location
            for filename, field in (("trace.jsonl", "tool_calls"), ("cli-http.jsonl", "http_requests")):
                try:
                    content = read_local(directory / filename, evidence, MAX_TRACE_BYTES)
                except FileNotFoundError:
                    continue
                except ValueError as error:
                    if str(error) != "Trace evidence is too large":
                        raise
                    continue
                metrics[field] = sum(bool(line.strip()) for line in content.splitlines())
        observed.append(metrics)
    return observed


def snapshot(evidence: Path, *, include_dialogue=False, raw=False, include_planned=False, native_artifacts=None):
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
    rows = [json.loads(line) for line in progress.split("\n")[:-1]]
    if (evidence / "matrix.json").is_file():
        matrix = json.loads(read_local(evidence / "matrix.json", evidence, MAX_PROGRESS_BYTES))
        if isinstance(matrix.get("results"), list):
            rows = matrix["results"]
    for row in rows:
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
    criteria = {}
    for case in review_cases:
        suite, scenario = case.split(":", 1)
        safe_id = LEGACY_IDS.get(scenario, scenario) if suite == "cli" else f"{suite}-{scenario}"
        for pair, model_number in tested_models.items():
            result = results.get((case, pair), {})
            stages = case_stages(suite, scenario, result.values())
            phase_ids = range(1, len(stages) + 1)
            if result.keys() == set(phase_ids):
                phases = [result[phase] for phase in phase_ids]
                state, note = verdict(phases, stages)
                sessions = (
                    synthetic_sessions(
                        evidence,
                        scenario,
                        model_number,
                        phases,
                        suite=suite,
                        raw=raw,
                        native_artifacts=native_artifacts,
                    )
                    if include_dialogue or raw
                    else []
                )
                reviews = [
                    stage
                    + "："
                    + "；".join(("通过 " if passed else "未通过 ") + label for label, passed in check_labels(row))
                    for stage, row in zip(stages, phases, strict=True)
                    if check_labels(row)
                ]
                metrics = {"phases": phase_metrics(phases, evidence)}
            elif include_planned:
                blocked = pair in preflight and preflight[pair] != "ready"
                state, note = ("blocked", "模型未通过预检") if blocked else ("pending", "等待评测结果")
                sessions, reviews, metrics = [], [], {}
                prompt = plan.get("prompts", {}).get(case)
                if (include_dialogue or raw) and isinstance(prompt, str):
                    sessions = [
                        [
                            {"role": "user", "text": prompt if raw else clean_prompt(prompt)},
                            {"role": "missing", "text": note},
                        ]
                    ]
            else:
                continue
            groups[safe_id], labels[safe_id] = CASES[case]
            if isinstance(instruction := plan.get("criteria", {}).get(case), str):
                criteria[safe_id] = clean_text(instruction)
            trial = {
                "case": safe_id,
                "model": f"{pair[0]} · {pair[1]}" if raw else model_name(pair, model_number),
                "state": state,
                "note": note,
                "sessions": sessions,
                "metrics": metrics,
            }
            if reviews:
                trial["note"] = "\n".join([note, *reviews])
            cases.setdefault(safe_id, []).append(trial)
    if preflight and review_cases:
        labels["model-preflight"] = "模型连接预检"
        groups["model-preflight"] = "运行环境"
        cases["model-preflight"] = [
            {
                "case": "model-preflight",
                "model": f"{pair[0]} · {pair[1]}" if raw else model_name(pair, number),
                "state": "pass" if preflight[pair] == "ready" else "blocked",
                "note": PREFLIGHT_NOTES.get(preflight[pair], "模型未通过预检"),
                "sessions": [],
            }
            for pair, number in tested_models.items()
            if pair in preflight
        ]
    title = "在线评测 · 合成测例" if include_dialogue else "在线评测 · 合成测例摘要"
    data = {"schema": 1, "title": title, "version": version, "labels": labels, "groups": groups, "cases": cases}
    if criteria:
        data["criteria"] = criteria
    if raw:
        data.update(title="LockBot Skill · 原始评测轨迹", visibility="raw")
    return data


def add_cli_regression(data, path, *, raw=False):
    evidence = json.loads(read_local(path.resolve(), path.resolve().parent, MAX_METADATA_BYTES))
    version = evidence.get("package_version")
    tools = evidence.get("tools", [])
    expected = ["init", "resources", "resources", "command", "resources"]
    if (
        evidence.get("case") != "packaged-cli-query-immediate-lock-verification"
        or evidence.get("simulation_only") is not True
        or evidence.get("result") != "passed"
        or evidence.get("transport") != "FastAPI TestClient"
        or evidence.get("verified_single_holder") is not True
        or evidence.get("verified_duration_seconds") != 3600
        or not isinstance(version, str)
        or not re.fullmatch(r"[a-f0-9]{16}", version)
        or not isinstance(tools, list)
        or [item.get("action") for item in tools] != expected
    ):
        raise ValueError("Expected successful packaged CLI regression evidence")
    http = [event for item in tools for event in item["http"]]
    if (
        http != evidence.get("http")
        or sum(event["method"] == "POST" for event in http) != 1
        or any(
            event["method"] not in {"GET", "POST"}
            or event["status"] != 200
            or event["path"]
            not in {"/api/agent-skill", "/api/agent/context", "/api/agent/resources", "/api/agent/commands"}
            for event in http
        )
        or any(item["exit_code"] != 0 or not all(isinstance(arg, str) for arg in item["argv"]) for item in tools)
    ):
        raise ValueError("Unexpected CLI execution or HTTP results")
    details = [
        {
            "input": {"argv": item["argv"] if raw else [clean_text(arg) for arg in item["argv"]]},
            "output": {
                "exit_code": item["exit_code"],
                "http": item["http"],
                **({"response": item["output"]} if raw else {}),
            },
        }
        for item in tools
    ]
    case = "regression-immediate-query-lock"
    data["labels"][case] = "连续查询后立即锁定并核对"
    data["groups"][case] = "CLI/API 自动回归"
    data.setdefault("criteria", {})[case] = (
        "执行实际安装包中的 lockbot_cli.py，HTTP 经 TestClient 进入真实后端路由。"
        "虚构资源连续查询两次后立即锁定一小时，再立即回读。核对只有一次写请求和一个实际占用。"
    )
    data["cases"][case] = [
        {
            "case": case,
            "model": "pytest 自动回归",
            "state": "pass",
            "package_version": version,
            "note": "真实 CLI main() 与真实后端路由，资源使用隔离夹具。此项没有模型参与。",
            "metrics": {"phases": [{"phase": 1, "tool_calls": len(tools), "http_requests": len(http)}]},
            "sessions": [
                [
                    {"role": "missing", "text": "自动化回归，展示程序实际执行的 CLI 参数和 HTTP 状态。"},
                    {
                        "role": "tools",
                        "total": len(tools),
                        "steps": [["packaged_cli.main", item["action"], "阶段 1", "已返回"] for item in tools],
                        "evidence": details,
                    },
                ]
            ],
        }
    ]


def publish(
    evidence: Path,
    target: Path,
    *,
    include_dialogue=False,
    raw=False,
    include_planned=False,
    regression=None,
    external_native_evidence=False,
):
    if external_native_evidence and not raw:
        raise ValueError("External native evidence requires a private raw export")
    data = snapshot(
        evidence,
        include_dialogue=include_dialogue,
        raw=raw,
        include_planned=include_planned,
        native_artifacts=target.resolve().parent / "evidence" if external_native_evidence else None,
    )
    if regression is not None:
        add_cli_regression(data, regression, raw=raw)
    if not data["cases"]:
        return None
    content = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    max_bytes = 32 * 1024 * 1024 if raw else MAX_SNAPSHOT_BYTES
    if len(content) > max_bytes:
        raise ValueError(f"Trace snapshot exceeds its {max_bytes // 1024**2} MiB limit")
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
    content_mode = parser.add_mutually_exclusive_group()
    content_mode.add_argument(
        "--raw-local", action="store_true", help="Export full tool evidence to a private local file"
    )
    content_mode.add_argument(
        "--include-dialogue", action="store_true", help="Include deidentified synthetic chat and tool steps"
    )
    parser.add_argument(
        "--include-planned", action="store_true", help="List all planned cases while waiting for results"
    )
    parser.add_argument(
        "--external-native-evidence",
        action="store_true",
        help="Keep complete native diagnostics in immutable private evidence files (raw only)",
    )
    parser.add_argument("--regression", type=Path, help="Include actual packaged CLI and backend regression evidence")
    parser.add_argument("--on-update", type=Path, help="Private JSON argv array for transferring the snapshot")
    args = parser.parse_args()
    if args.external_native_evidence and not args.raw_local:
        parser.error("--external-native-evidence requires --raw-local")
    if args.raw_local and args.on_update:
        parser.error("--raw-local writes a local file; transfer it through the private reviewer deployment")
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
        updated = publish(
            args.evidence,
            args.target,
            include_dialogue=args.include_dialogue,
            raw=args.raw_local,
            include_planned=args.include_planned,
            regression=args.regression,
            external_native_evidence=args.external_native_evidence,
        )
        if transport and updated is not None and (updated or not transfer_started):
            subprocess.run([*transport, str(args.target)], check=True)
            transfer_started = True
        if not args.follow or (args.evidence / "matrix.json").is_file():
            break
        sleep(2)


if __name__ == "__main__":
    main()
