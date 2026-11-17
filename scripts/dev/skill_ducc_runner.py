#!/usr/bin/env python3
"""Run native ducc for the Skill evaluation stdin/stdout protocol."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


class UnsupportedContext(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_write(path, text):
    path.write_text(text)
    path.chmod(0o600)


def previous_session(request, root, system):
    """Only resume verified native history; never turn prior answers into a prompt."""
    if "conversation_file" not in request:
        return None
    path = Path(request["conversation_file"]).resolve()
    raw = path.read_bytes()
    if digest(raw) != request.get("conversation_sha256"):
        raise UnsupportedContext("conversation_digest_mismatch")
    history = json.loads(raw)
    if (
        not isinstance(history, list)
        or len(history) != 2
        or not all(isinstance(row, dict) for row in history)
        or [row.get("role") for row in history] != ["user", "assistant"]
        or not all(isinstance(row.get("content"), str) for row in history)
    ):
        raise UnsupportedContext("native_two_turn_history_required")
    if root.name != "phase-2":
        raise UnsupportedContext("native_predecessor_not_identified")
    first = root.parent / "phase-1"
    try:
        state = json.loads((first / "native-session.json").read_text())
        response = json.loads((first / "response.json").read_text())
        original = (first / "prompt.txt").read_text()
        session = str(uuid.UUID(state["session_id"]))
    except (OSError, ValueError, KeyError, TypeError):
        raise UnsupportedContext("verified_native_predecessor_required") from None
    if (
        state.get("client") != "ducc"
        or state.get("model") != request["model"]
        or state.get("system_sha256") != digest(system.encode())
        or state.get("prompt_sha256") != digest(original.encode())
        or state.get("answer_sha256") != digest(response["text"].encode())
        or history != [{"role": "user", "content": original}, {"role": "assistant", "content": response["text"]}]
    ):
        raise UnsupportedContext("native_history_does_not_match_supplied_turns")
    return session


def ducc_command(executable, request, system, session):
    config = json.loads(Path(request["mcp_config"]).read_text())
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or any(not name or not name.replace("_", "").isalnum() for name in servers):
        raise ValueError("unsupported_mcp_server_name")
    allowed = ",".join("mcp__" + name + "__*" for name in servers)
    command = [
        executable,
        "-p",
        "--model",
        request["model"],
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        str(Path(request["mcp_config"]).resolve()),
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--settings",
        '{"disableAllHooks":true}',
        "--setting-sources",
        "user",
        "--disable-slash-commands",
        "--max-turns",
        str(request["max_turns"]),
        "--system-prompt",
        system,
        "--system-prompt-snapshot",
        "off",
    ]
    if allowed:
        command.extend(["--allowedTools", allowed])
    if session:
        command.extend(["--resume", session, "--fork-session"])
    return command


def parse_ducc(stdout):
    events = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            events.append(json.loads(line))
    results = [event for event in events if event.get("type") == "result"]
    if len(results) != 1:
        raise ValueError("native_final_result_required")
    result = results[0]
    if result.get("is_error"):
        return {"text": result.get("result", ""), "kind": result.get("subtype", "native_client_error")}, None
    if not isinstance(result.get("result"), str) or not result["result"].strip():
        raise ValueError("native_answer_missing")
    session = str(uuid.UUID(result["session_id"]))
    turns = result.get("num_turns")
    if type(turns) is not int or turns < 0:
        raise ValueError("native_turn_count_missing")
    return {"text": result["result"], "turns": turns, "native_client": "ducc", "native_session_id": session}, session


def run_ducc(request, executable):
    root = Path.cwd()
    prompt = Path(request["prompt_file"]).read_text()
    system = Path(request["system_file"]).read_text()
    session = previous_session(request, root, system)
    for key in ("max_turns", "output_tokens"):
        if type(request.get(key)) is not int or request[key] <= 0:
            raise ValueError("invalid_" + key)
    command = ducc_command(executable, request, system, session)
    # Native sessions are indexed by the original project directory. The
    # absolute MCP configuration still targets the new phase's fixture.
    native_cwd = root.parent / "phase-1" if session else root
    private_write(
        root / "native-invocation.json",
        json.dumps({"client": "ducc", "argv": command, "cwd": str(native_cwd)}, ensure_ascii=False),
    )
    environment = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(request["output_tokens"])}
    # Inherit the matrix's process group: its timeout must kill the client and MCP children together.
    result = subprocess.run(
        command, input=prompt, text=True, capture_output=True, env=environment, cwd=native_cwd, check=False
    )
    private_write(root / "native-ducc.jsonl", result.stdout)
    private_write(root / "native-ducc.stderr", result.stderr)
    if result.returncode:
        try:
            error, _ = parse_ducc(result.stdout)
        except (ValueError, KeyError, TypeError):
            error = {"kind": "native_client_failed"}
        return error, result.returncode
    response, created = parse_ducc(result.stdout)
    if not created:
        return response, 2
    if session and created == session:
        raise ValueError("native_session_was_not_forked")
    private_write(
        root / "native-session.json",
        json.dumps(
            {
                "client": "ducc",
                "session_id": created,
                "model": request["model"],
                "system_sha256": digest(system.encode()),
                "prompt_sha256": digest(prompt.encode()),
                "answer_sha256": digest(response["text"].encode()),
            }
        ),
    )
    response["native_evidence"] = "native-ducc.jsonl"
    if session:
        response["conversation_sha256"] = request["conversation_sha256"]
        response["native_context_mode"] = "fork_verified_session"
    return response, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ducc", default="ducc", help="Native executable; authentication remains client-managed")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        request = json.load(sys.stdin)
        result, status = run_ducc(request, args.ducc)
    except UnsupportedContext as error:
        result, status = {"text": "", "kind": "unsupported_context", "reason": str(error)}, 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        result, status = {"kind": "native_runner_error", "error_type": type(error).__name__}, 2
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
