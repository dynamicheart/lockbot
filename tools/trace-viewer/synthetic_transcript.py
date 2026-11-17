"""Project dialogue from the local, synthetic CLI test harness."""

import json
import re
from hashlib import sha256
from pathlib import Path

TOOL_LABELS = {
    "discover_local_state": "查找已安装 Skill 与本地连接",
    "http_request": "HTTP 请求",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "inspect_archive": "检查安装包",
    "install_skill": "安装 Skill",
    "read_consultation_skill": "读取 Skill 文档",
    "read_consultation_context": "读取群规则",
    "inspect_lockbot": "检查 Skill 链接",
    "install_lockbot": "安装完整 Skill",
    "read_lockbot_skill": "读取 Skill",
    "run_lockbot": "运行 LockBot CLI",
    "deliver_private_link": "私下交付登录链接",
    "browser_home": "打开资源台固定入口",
}
CONFIRMATIONS = {
    "chat_binding_confirmation": "测试程序模拟一次群内确认。",
    "browser_consent": "测试程序模拟一次浏览器确认。",
}
SKILL_REFERENCES = {
    "references/connections.md",
    "references/resource-operations.md",
    "references/workbench-and-metrics.md",
}
MAX_DOCUMENT_CHARACTERS = 65_536
MAX_TRACE_BYTES = 32 * 1024 * 1024
ACTIONS = {
    "local-state",
    "connections",
    "discover",
    "enroll",
    "key-challenge",
    "enrollment-status",
    "finish-enrollment",
    "init",
    "recover",
    "resources",
    "panel-link",
    "command",
}
SECRET_FIELDS = re.compile(
    r"(?i)(\b(?:authorization|api[_ -]?key|access[_ -]?token|password|credential|proof|ticket)\s*[:=]\s*)"
    r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;`，。]+)"
)
PRIVATE_TEXT = re.compile(
    r"(?i)(?:https?|file)://[^\s`<>()\[\]\"，。；]+"
    r"|(?<![\w/])(?:[A-Z]:[\\/]|/)(?:[\w.~@+ -]+[\\/])+[^\s`\"，。；:]*"
    r"|\b(?:lbr|lbp|lbk|lbc1)[_.][\w.-]{12,}\b"
    r"|\b(?:eyJ[\w-]+\.[\w-]+\.[\w-]+|sk-[\w-]{12,})\b"
    r"|\bBearer\s+[\w.-]{12,}\b"
    r"|\b(?:REC|RST)-[A-Z0-9]{8}\b"
    r"|(?<=\bbind )[A-Z0-9]{8}\b"
    r"|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"
)


def clean_text(value: str) -> str:
    redacted = PRIVATE_TEXT.sub("[已隐藏]", SECRET_FIELDS.sub(r"\1[已隐藏]", value))
    encoded = redacted.encode()
    if len(encoded) > 2200:
        return encoded[:2200].decode(errors="ignore") + "…（完整内容见本地轨迹）"
    return redacted


SYNTHETIC_SKILL_LINK = re.compile(
    r"http://192[.]0[.]2[.]10:8155(/api/agent-skill(?:/SKILL[.]md|/lockbot-(?:resource-)?agent[.]zip)?)"
    r"(?=$|[\s`，。)])"
)


def clean_prompt(value: str) -> str:
    parts = SYNTHETIC_SKILL_LINK.split(value)
    return "".join(
        f"https://lockbot.example.test{part}" if index % 2 else clean_text(part) for index, part in enumerate(parts)
    )


def read_local(path: Path, root: Path, limit: int) -> str:
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Trace evidence must be a regular in-directory path")
    with resolved.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Trace evidence is too large")
    return content.decode("utf-8")


def skill_page_metadata(result):
    metadata = {}
    content = result.get("content")
    if isinstance(content, str) and len(content) <= 6000:
        metadata["returned_characters"] = len(content)
    total = result.get("total_characters")
    if type(total) is int and 0 <= total <= MAX_DOCUMENT_CHARACTERS:
        metadata["total_characters"] = total
    if "next_offset" in result:
        offset = result["next_offset"]
        if offset is None or (type(offset) is int and 0 <= offset <= MAX_DOCUMENT_CHARACTERS):
            metadata["next_offset"] = offset
    return metadata


def tool_steps(lines: str, phase: int, *, raw=False):
    steps, evidence = [], []
    records = lines.splitlines()
    for line in records if raw or len(records) <= 20 else records[:10] + records[-10:]:
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("Invalid tool event")
        name = event.get("tool")
        if name not in TOOL_LABELS and not raw:
            continue
        params = event.get("args", {})
        result = event.get("result", {})
        if not isinstance(params, dict) or not isinstance(result, dict):
            raise ValueError("Invalid tool event fields")
        failed = (
            bool(result.get("error"))
            or result.get("isError") is True
            or (type(result.get("exit_code")) is int and result["exit_code"] != 0)
            or (type(result.get("status")) is int and result["status"] >= 400)
        )
        status = "失败" if failed else "已返回"
        arguments = {}
        output = {"status": status}
        label = TOOL_LABELS.get(name, name)
        if raw:
            action = params.get("action")
            if isinstance(action, str):
                label = action
            details = {"input": params, "output": result}
            if "argv" in event:
                details["argv"] = event["argv"]
            steps.append([name, label, f"会话 {phase}", status])
            evidence.append(details)
            continue
        if name in ("read_lockbot_skill", "read_consultation_skill"):
            reference = params.get("document") if name == "read_consultation_skill" else params.get("reference")
            if reference in (None, "SKILL.md"):
                arguments["document"] = "SKILL.md"
            elif isinstance(reference, str) and reference in SKILL_REFERENCES:
                arguments["document"] = reference
            offset = params.get("offset")
            if type(offset) is int and 0 <= offset <= MAX_DOCUMENT_CHARACTERS:
                arguments["offset"] = offset
            if not failed:
                output.update(skill_page_metadata(result))
        elif name == "http_request":
            method = params.get("method", "GET")
            if method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
                arguments["method"] = method
            if type(result.get("status")) is int and 100 <= result["status"] <= 599:
                output["http_status"] = result["status"]
        elif name == "install_lockbot":
            if not failed and result.get("installed") is True:
                output.update(skill_page_metadata(result))
                version = result.get("version")
                if isinstance(version, str) and re.fullmatch("[a-f0-9]{16}", version):
                    output["version"] = version
        elif name == "run_lockbot":
            action = params.get("action")
            if isinstance(action, str) and action in ACTIONS:
                arguments["action"] = label = action
            purpose = params.get("purpose")
            if purpose in ("recover", "reset"):
                arguments["purpose"] = purpose
        steps.append([name, label, f"会话 {phase}", status])
        evidence.append({"input": arguments, "output": output})
    return {"role": "tools", "steps": steps, "evidence": evidence, "total": len(records)}


def native_records(directory, root):
    for name in ("codex-native.jsonl", "native-ducc.jsonl"):
        path = directory / name
        if path.is_file():
            records = []
            for line in read_local(path, root, MAX_TRACE_BYTES).splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    # An interrupted final wire line is evidence too, not a reason
                    # to discard the completed preceding events or the phase.
                    records.append({"unparsed_native_line": line})
            yield name, records


def native_evidence(directory, root):
    """Keep the complete native wire records in private raw exports only."""
    events = [
        {
            "role": "tools",
            "total": 1,
            "steps": [["native_client_protocol", name, "原生客户端证据", "已记录"]],
            "evidence": [{"input": {"file": name}, "output": {"events": records}}],
        }
        for name, records in native_records(directory, root)
    ]
    for name in ("codex-native.stderr", "native-ducc.stderr", "runner.stderr"):
        path = directory / name
        if path.is_file() and path.stat().st_size:
            events.append(
                {
                    "role": "tools",
                    "total": 1,
                    "steps": [["native_client_stderr", name, "原生客户端诊断", "已记录"]],
                    "evidence": [
                        {"input": {"file": name}, "output": {"text": read_local(path, root, MAX_TRACE_BYTES)}}
                    ],
                }
            )
    return events


def native_partial_replies(directory, root):
    replies = []
    for name, records in native_records(directory, root):
        for record in records:
            if name == "codex-native.jsonl":
                message = record.get("message", {})
                if record.get("direction") != "receive" or message.get("method") != "item/completed":
                    continue
                item = message.get("params", {}).get("item", {})
                text = item.get("text") if item.get("type") == "agentMessage" else None
            else:
                message = record.get("message", {})
                if record.get("type") != "assistant":
                    continue
                text = "".join(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")
            if isinstance(text, str) and text:
                replies.append({"role": "assistant", "text": text})
    return replies


def synthetic_sessions(
    root: Path, case: str, runner: int, rows: list[dict], *, suite="cli", raw=False
) -> list[list[dict]]:
    """Use the expected synthetic evidence files and redact by default."""
    root = root.resolve()
    sessions = []
    for index, row in enumerate(rows):
        try:
            phase = row.get("phase")
            if type(phase) is not int or not 1 <= phase <= 4 or "evidence" not in row:
                continue
            location = Path(suite, case, f"{runner:03d}", f"phase-{phase}")
            if row["evidence"] != location.as_posix():
                raise ValueError("Unexpected fixture evidence path")
            directory = root / location
            meta = json.loads(read_local(directory / "case.json", root, 8192))
            subject = read_local(directory / "subject.md", root, 65_536)
            if (
                meta["scenario"] != case
                or meta.get("phase", 1) != phase
                or meta["skill_sha256"] != row["skill_sha256"]
                or sha256(subject.encode()).hexdigest() != meta.get("subject_sha256", meta["skill_sha256"])
            ):
                raise ValueError("Unexpected synthetic fixture")
            prompt = read_local(directory / "prompt.txt", root, 32_768)
            session = [{"role": "user", "text": prompt if raw else clean_prompt(prompt)}]
            trace = directory / "trace.jsonl"
            if trace.is_file():
                try:
                    calls = tool_steps(read_local(trace, root, MAX_TRACE_BYTES), phase, raw=raw)
                except ValueError as error:
                    if str(error) != "Trace evidence is too large":
                        raise
                    session.append({"role": "missing", "text": "工具轨迹超过 32 MB，完整内容保留在本地证据中。"})
                else:
                    if calls["steps"]:
                        session.append(calls)
                    if calls["total"] > len(calls["steps"]):
                        session.append(
                            {"role": "missing", "text": f"已展示 {len(calls['steps'])} / {calls['total']} 次工具调用。"}
                        )
            if raw:
                session.extend(native_evidence(directory, root))
            response = directory / "response.json"
            if response.is_file():
                final = json.loads(read_local(response, root, 65_536))["text"]
                session.append({"role": "assistant", "text": final if raw else clean_text(final)})
            elif raw:
                session.extend(native_partial_replies(directory, root))
                session.append(
                    {"role": "missing", "text": "未记录最终答复；阶段状态：" + str(row.get("status", "unknown"))}
                )
            if index + 1 < len(rows):
                actions = rows[index + 1].get("simulated_user_actions", [])
                if isinstance(actions, list):
                    session.extend(
                        {"role": "human", "text": text} for action, text in CONFIRMATIONS.items() if action in actions
                    )
            sessions.append(session)
        except (OSError, KeyError, TypeError, ValueError):
            # One unavailable phase must not erase earlier verified dialogue.
            continue
    return sessions
