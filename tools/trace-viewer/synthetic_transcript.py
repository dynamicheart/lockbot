"""Project deidentified dialogue from the local, synthetic CLI test harness."""

import json
import re
from hashlib import sha256
from pathlib import Path

TOOL_LABELS = {
    "install_lockbot": "安装完整 Skill",
    "read_lockbot_skill": "读取 Skill",
    "run_lockbot": "运行 LockBot CLI",
}
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


def read_local(path: Path, root: Path, limit: int) -> str:
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Trace evidence must be a regular in-directory path")
    with resolved.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Trace evidence is too large")
    return content.decode("utf-8")


def tool_steps(lines: str, phase: int):
    steps, evidence = [], []
    for line in lines.splitlines()[:20]:
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("Invalid tool event")
        name = event.get("tool")
        if name not in TOOL_LABELS:
            continue
        params = event.get("args", {})
        result = event.get("result", {})
        if not isinstance(params, dict) or not isinstance(result, dict):
            raise ValueError("Invalid tool event fields")
        action = params.get("action")
        arguments = {"action": action} if action in ACTIONS else {}
        purpose = params.get("purpose")
        if purpose in {"recover", "reset"}:
            arguments["purpose"] = purpose
        failed = bool(result.get("error"))
        status = "失败" if failed else "已返回"
        steps.append([name, arguments.get("action", TOOL_LABELS[name]), f"会话 {phase}", status])
        evidence.append({"input": arguments, "output": {"status": status}})
    return {"role": "tools", "steps": steps, "evidence": evidence}


def synthetic_sessions(root: Path, case: str, runner: int, rows: list[dict]) -> list[list[dict]]:
    """Only use the expected CLI evidence files. Never project raw tool inputs or outputs."""
    root = root.resolve()
    sessions = []
    try:
        for row in rows:
            phase = row.get("phase")
            if phase not in {1, 2, 3} or "evidence" not in row:
                continue
            location = Path("cli", case, f"{runner:03d}", f"phase-{phase}")
            if row["evidence"] != location.as_posix():
                raise ValueError("Unexpected fixture evidence path")
            directory = root / location
            meta = json.loads(read_local(directory / "case.json", root, 8192))
            subject = read_local(directory / "subject.md", root, 65_536)
            if (
                meta["scenario"] != case
                or meta["phase"] != phase
                or meta["skill_sha256"] != row["skill_sha256"]
                or sha256(subject.encode()).hexdigest() != meta["skill_sha256"]
            ):
                raise ValueError("Unexpected synthetic fixture")
            session = [{"role": "user", "text": clean_text(read_local(directory / "prompt.txt", root, 32_768))}]
            trace = directory / "trace.jsonl"
            if trace.is_file():
                calls = tool_steps(read_local(trace, root, 524_288), phase)
                if calls["steps"]:
                    session.append(calls)
            response = directory / "response.json"
            if response.is_file():
                final = json.loads(read_local(response, root, 65_536))["text"]
                session.append({"role": "assistant", "text": clean_text(final)})
            if phase == 1 and any(
                "chat_binding_confirmation" in other.get("simulated_user_actions", []) for other in rows
            ):
                session.append({"role": "human", "text": "测试程序模拟一次群内确认。"})
            sessions.append(session)
    except (OSError, KeyError, TypeError, ValueError):
        # Retain the summary if an optional detail cannot be projected safely.
        return []
    return sessions
