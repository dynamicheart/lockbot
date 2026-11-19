"""Project dialogue from the local, synthetic CLI test harness."""

import json
import os
import re
import stat
import tempfile
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
MAX_NATIVE_EVIDENCE_BYTES = 16 * 1024 * 1024
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


def has_packaged_cli_host(events):
    """Recognize the versioned host, including local invocations with no HTTP."""
    return any(
        isinstance(event, dict)
        and (
            event.get("source") == "packaged_cli"
            or (
                event.get("tool") == "run_lockbot"
                and isinstance(args := event.get("args"), dict)
                and isinstance(args.get("script"), str)
                and isinstance(args.get("argv"), list)
            )
        )
        for event in events
    )


def tool_steps(lines: str, phase: int, *, raw=False):
    steps, evidence = [], []
    records = lines.splitlines()
    # The versioned host marks internal HTTP and uses script/argv requests.
    # Keep action-based legacy projections and positional anchors unchanged.
    packaged = has_packaged_cli_host(json.loads(line) for line in records)
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
        internal = event.get("source") == "packaged_cli"
        scope = "CLI 内部 HTTP" if internal else f"会话 {phase}"
        if raw:
            action = params.get("action")
            if isinstance(action, str):
                label = action
            details = {"input": params, "output": result}
            if "argv" in event:
                details["argv"] = event["argv"]
            if packaged:
                # Preserve actual raw_result/stdout/stderr and any later host
                # execution diagnostics, not merely the model-visible result.
                details.update({key: value for key, value in event.items() if key not in {"tool", "args", "result"}})
            steps.append([name, label, scope, status])
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
        steps.append([name, label, scope, status])
        evidence.append({"input": arguments, "output": output, **({"source": "packaged_cli"} if internal else {})})
    projected = {"role": "tools", "steps": steps, "evidence": evidence, "total": len(records)}
    if packaged:
        events = [json.loads(line) for line in records]
        projected["model_tool_calls"] = sum(event.get("source") != "packaged_cli" for event in events)
        projected["cli_http_requests"] = sum(
            event.get("source") == "packaged_cli" and event.get("tool") == "http_request" for event in events
        )
    return projected


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


class NativeArtifactError(ValueError):
    pass


def read_native_bytes(path, root):
    if path.resolve(strict=True) != path or not path.is_relative_to(root):
        raise ValueError("Native evidence must be a regular in-directory file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_NATIVE_EVIDENCE_BYTES:
            raise ValueError("Native evidence is not regular or exceeds its 16 MiB limit")
        content = source.read(MAX_NATIVE_EVIDENCE_BYTES + 1)
    if len(content) > MAX_NATIVE_EVIDENCE_BYTES:
        raise ValueError("Native evidence exceeds its 16 MiB limit")
    return content


def store_native_artifact(path, root, artifacts):
    content = read_native_bytes(path, root)
    digest = sha256(content).hexdigest()
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    if artifacts.resolve() != artifacts or artifacts.stat().st_mode & 0o777 != 0o700:
        raise ValueError("Native artifact directory must be private and contain no symlinks")
    target = artifacts / digest
    descriptor, temporary = tempfile.mkstemp(dir=artifacts)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if (
                target.is_symlink()
                or target.stat().st_mode & 0o777 != 0o600
                or read_native_bytes(target, artifacts) != content
            ):
                raise ValueError("Immutable native evidence was modified") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"sha256": digest, "bytes": len(content), "filename": path.name}


def native_artifact_manifest(artifacts):
    files = []
    if artifacts.is_symlink():
        raise ValueError("Native artifact directory must not be a symlink")
    if artifacts.exists():
        if artifacts.resolve() != artifacts or artifacts.stat().st_mode & 0o777 != 0o700:
            raise ValueError("Native artifact directory must be private and contain no symlinks")
        for path in sorted(artifacts.iterdir()):
            if (
                not re.fullmatch(r"[a-f0-9]{64}", path.name)
                or path.is_symlink()
                or path.stat().st_mode & 0o777 != 0o600
            ):
                raise ValueError("Unsafe native artifact entry")
            content = read_native_bytes(path, artifacts)
            if sha256(content).hexdigest() != path.name:
                raise ValueError("Immutable native evidence was modified")
            files.append({"sha256": path.name, "bytes": len(content)})
    return {
        "directory": "evidence",
        "max_file_bytes": MAX_NATIVE_EVIDENCE_BYTES,
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }


def external_native_evidence(directory, root, artifacts):
    events = []
    for name in (
        "codex-native.jsonl",
        "native-ducc.jsonl",
        "codex-native.stderr",
        "native-ducc.stderr",
        "runner.stderr",
    ):
        path = directory / name
        if not path.is_file() or not path.stat().st_size:
            continue
        try:
            reference = store_native_artifact(path, root, artifacts)
        except (OSError, ValueError) as error:
            raise NativeArtifactError(str(error)) from error
        events.append(
            {
                "role": "tools",
                "total": 1,
                "steps": [["native_client_diagnostic", name, "完整诊断独立保存", "可下载"]],
                "evidence": [{"input": {"file": name}, "output": {"artifact": reference}}],
            }
        )
    return events


def native_evidence(directory, root, *, artifacts=None):
    """Keep complete diagnostics inline by default, or losslessly in private blobs."""
    if artifacts is not None:
        return external_native_evidence(directory, root, artifacts)
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


def fixture_scenario(meta, suite):
    """Resolve only the explicitly versioned wrappers around existing CLI fixtures."""
    if suite == "resource-cli":
        version, field = "resource_cli_version", "resource_cli_scenario"
        underlying = dict.fromkeys(
            ("find-p800", "list-resources", "all-resources", "node-detail", "device-detail", "metrics"),
            "intent-find-p800",
        )
        expected_versions = {1, 2}
    elif suite == "cli-release":
        version, field = "release_fixture_version", "release_scenario"
        underlying = {
            "alias-set": "intent-lock-tail",
            "alias-clear": "intent-lock-tail",
            "queue-book": "intent-book",
            "queue-claim": "intent-claim",
            "queue-mine": "intent-my-books",
            "release-reconcile": "intent-my-books",
            "update-inquiry": "intent-lock-tail",
        }
        expected_versions = {3, 4, 5}
    else:
        return meta["scenario"]
    scenario = meta.get(field)
    if (
        meta.get("fixture_family") != suite
        or type(meta.get(version)) is not int
        or meta[version] not in expected_versions
        or not isinstance(scenario, str)
        or scenario not in underlying
        or meta.get("scenario") != underlying[scenario]
    ):
        raise ValueError("Unexpected versioned synthetic fixture")
    return scenario


def synthetic_sessions(
    root: Path, case: str, runner: int, rows: list[dict], *, suite="cli", raw=False, native_artifacts=None
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
                fixture_scenario(meta, suite) != case
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
                        noun = "条调用与 HTTP 记录" if "model_tool_calls" in calls else "次工具调用"
                        session.append(
                            {"role": "missing", "text": f"已展示 {len(calls['steps'])} / {calls['total']} {noun}。"}
                        )
            if raw:
                session.extend(native_evidence(directory, root, artifacts=native_artifacts))
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
        except (OSError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, NativeArtifactError):
                raise
            # One unavailable phase must not erase earlier verified dialogue.
            continue
    return sessions
