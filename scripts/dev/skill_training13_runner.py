#!/usr/bin/env python3
"""Bridge the isolated Skill MCP fixture to a native training13 Codex app-server.

No model APIs are called directly. SSH connection details live in a private JSON
file supplied with --connection. Native history is forked only from this runner's
verified preceding phase; arbitrary conversation imports are unsupported.
"""

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROVIDER = "skill_eval_mixin"
REMOTE_PREFIX = "/tmp/lockbot-skill-codex-"
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "hooks",
    "memories",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "view_image",
    "skill_search",
    "skill_mcp_dependency_install",
    "sleep_tool",
    "workspace_dependencies",
    "goals",
)


class RunnerError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def digest(data):
    return hashlib.sha256(data).hexdigest()


def model_identity(requested):
    """Translate only Mixin's official panel namespace to its native wire slug.

    Mixin 0.7.5 routes gpt-* to its official OAuth transport, while panel model
    IDs prepend official:. Custom catalog slugs must remain byte-for-byte intact.
    """
    if requested.startswith("official:"):
        transport = requested.removeprefix("official:")
        if not re.fullmatch(r"gpt-[A-Za-z0-9_.-]+", transport):
            raise ValueError("Unsupported official model identity")
        return transport, "official"
    return requested, "gateway"


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class JsonProcess:
    """Line-delimited JSON RPC with bounded reads and preserved wire evidence."""

    def __init__(self, argv, evidence, name, deadline, env=None):
        self.deadline = deadline
        self.inbox = queue.Queue()
        self.sequence = 0
        self.backlog = []
        self.trace = (evidence / f"{name}.jsonl").open("x", encoding="utf-8")
        (evidence / f"{name}.jsonl").chmod(0o600)
        self.stderr = (evidence / f"{name}.stderr").open("x", encoding="utf-8")
        (evidence / f"{name}.stderr").chmod(0o600)
        save(evidence / f"{name}-argv.json", argv)
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
            env=env,
            bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    self.inbox.put(RunnerError("native_protocol_error", "Non-JSON process output"))
        finally:
            self.inbox.put(RunnerError("native_process_exit", "Native process closed stdout"))

    def send(self, message):
        self.trace.write(json.dumps({"direction": "send", "message": message}, ensure_ascii=False) + "\n")
        self.trace.flush()
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError("timeout", "Native adapter deadline exceeded")
        try:
            message = self.inbox.get(timeout=remaining)
        except queue.Empty as error:
            raise RunnerError("timeout", "Native adapter deadline exceeded") from error
        if isinstance(message, Exception):
            raise message
        self.trace.write(json.dumps({"direction": "receive", "message": message}, ensure_ascii=False) + "\n")
        self.trace.flush()
        return message

    def call(self, method, params):
        self.sequence += 1
        number = self.sequence
        self.send({"id": number, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == number and "method" not in message:
                if "error" in message:
                    raise RunnerError("native_rpc_error", json.dumps(message["error"]))
                return message.get("result", {})
            # Notifications arriving before the RPC reply must survive until turn processing.
            self.backlog.append(message)

    def close(self):
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.trace.close()
        self.stderr.close()


def connection_argv(connection, remote_home):
    """Use only an explicitly configured container entry point and strict host verification."""
    required = ("host", "user", "identity_file", "known_hosts")
    if any(not isinstance(connection.get(key), str) or not connection[key] for key in required):
        raise ValueError("Connection requires host, user, identity_file and known_hosts")
    if any(connection[key].startswith("-") or "\0" in connection[key] for key in required):
        raise ValueError("Invalid SSH connection field")
    if not remote_home.startswith(REMOTE_PREFIX) or not remote_home[len(REMOTE_PREFIX) :].isalnum():
        raise ValueError("Invalid isolated remote directory")
    # Authentication is handled by the trusted loopback Mixin gateway. Never copy
    # or link the user's real Codex auth.json or config into this private home.
    config = {
        "model_provider": PROVIDER,
        f"model_providers.{PROVIDER}.name": "Skill evaluation loopback gateway",
        f"model_providers.{PROVIDER}.base_url": connection.get("base_url", "http://127.0.0.1:8582/v1"),
        f"model_providers.{PROVIDER}.wire_api": "responses",
        f"model_providers.{PROVIDER}.requires_openai_auth": False,
        "project_doc_max_bytes": 0,
        "web_search": "disabled",
        "features.skip_host_skill_discovery": True,
        "mcp_servers": {},
    }
    if config[f"model_providers.{PROVIDER}.base_url"] != "http://127.0.0.1:8582/v1":
        raise ValueError("Only the container loopback Mixin gateway is supported")
    config.update({f"features.{feature}": False for feature in DISABLED_FEATURES})
    codex = connection.get("codex", "/opt/codex/bin/codex")
    argv = [codex, "app-server", "--stdio"]
    for key, value in config.items():
        # JSON strings, booleans and the empty table are also TOML expressions.
        argv.extend(["-c", key + "=" + ("{}" if value == {} else json.dumps(value))])
    launcher = (
        "import os,sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.mkdir(mode=0o700,exist_ok=True); "
        "assert p.resolve()==p and p.stat().st_uid==os.geteuid() and p.stat().st_mode & 0o777==0o700; "
        "c=p/'workspace'; c.mkdir(mode=0o700,exist_ok=True); "
        "assert c.resolve()==c and c.stat().st_uid==os.geteuid() and c.stat().st_mode & 0o777==0o700; os.chdir(c); "
        "os.environ['CODEX_HOME']=str(p); "
        "[os.environ.pop(k,None) for k in list(os.environ) "
        "if k.startswith(('OPENAI_','CODEX_')) and k!='CODEX_HOME']; "
        "os.execv(sys.argv[2],sys.argv[2:])"
    )
    remote = shlex.join(["python3", "-c", launcher, remote_home, *argv])
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + connection["known_hosts"],
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-i",
        connection["identity_file"],
        "-p",
        str(int(connection.get("port", 22))),
        connection["user"] + "@" + connection["host"],
        remote,
    ]


def context_source(request, root, system, connection_hash):
    if "conversation_file" not in request:
        return None
    raw = Path(request["conversation_file"]).read_bytes()
    if digest(raw) != request.get("conversation_sha256"):
        raise RunnerError("unsupported_context", "Conversation digest mismatch")
    try:
        history = json.loads(raw)
        previous = root.parent / "phase-1"
        metadata = json.loads((previous / "native-session.json").read_text())
        response = json.loads((previous / "response.json").read_text())
        expected = [
            {"role": "user", "content": (previous / "prompt.txt").read_text()},
            {"role": "assistant", "content": response["text"]},
        ]
        if history != expected or len(history) != 2:
            raise ValueError("Conversation does not match the observed preceding phase")
        for key, value in {
            "adapter": "training13-codex-app-server-v1",
            "model": request["model"],
            "transport_model": model_identity(request["model"])[0],
            "model_source": model_identity(request["model"])[1],
            "system_sha256": digest(system.encode()),
            "connection_sha256": connection_hash,
            "prompt_sha256": digest(expected[0]["content"].encode()),
            "answer_sha256": digest(expected[1]["content"].encode()),
        }.items():
            if metadata.get(key) != value:
                raise ValueError("Previous native session identity or content does not match")
        if not metadata.get("thread_id") or not metadata.get("turn_id"):
            raise ValueError("Previous native thread identity is missing")
        return metadata, history
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RunnerError("unsupported_context", str(error)) from error


def verify_native_history(thread, history, turn_id):
    turns = thread.get("turns", [])
    if len(turns) != 1 or turns[0].get("id") != turn_id or turns[0].get("status") != "completed":
        raise RunnerError("unsupported_context", "Native preceding turn is not the completed observed turn")
    items = turns[0].get("items", [])
    users = [item for item in items if item.get("type") == "userMessage"]
    answers = [item for item in items if item.get("type") == "agentMessage"]
    if len(users) != 1 or not answers:
        raise RunnerError("unsupported_context", "Native user/assistant history is unavailable")
    text = "".join(part.get("text", "") for part in users[0].get("content", []) if part.get("type") == "text")
    if text != history[0]["content"] or answers[-1].get("text") != history[1]["content"]:
        raise RunnerError("unsupported_context", "Native preceding messages differ from conversation.json")


def fixture_client(request, root, deadline):
    servers = json.loads(Path(request["mcp_config"]).read_text()).get("mcpServers")
    if servers == {}:
        return None, []
    if not isinstance(servers, dict) or set(servers) != {"fixture"}:
        raise ValueError("Only the supplied fixture MCP server is supported")
    server = servers["fixture"]
    if set(server) - {"command", "args", "env"}:
        raise ValueError("Only a local stdio fixture MCP server is supported")
    client = JsonProcess(
        [server["command"], *server.get("args", [])],
        root,
        "fixture-native",
        deadline,
        env={**os.environ, **server.get("env", {})},
    )
    try:
        client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "skill-training13-runner", "version": "1"},
            },
        )
        client.send({"method": "notifications/initialized"})
        result = client.call("tools/list", {})
        if result.get("nextCursor"):
            raise ValueError("Paginated fixture tool lists are unsupported")
        tools = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool["inputSchema"],
            }
            for tool in result["tools"]
        ]
        if len({tool["name"] for tool in tools}) != len(tools):
            raise ValueError("Duplicate fixture tools")
        return client, tools
    except BaseException:
        client.close()
        raise


def run(request, connection, timeout):
    root = Path(request["prompt_file"]).resolve().parent
    prompt = Path(request["prompt_file"]).read_text()
    system = Path(request["system_file"]).read_text()
    if not isinstance(request["model"], str) or not request["model"]:
        raise ValueError("An explicit model is required")
    transport_model, model_source = model_identity(request["model"])
    if any(type(request.get(key)) is not int or request[key] < 1 for key in ("max_turns", "output_tokens")):
        raise ValueError("max_turns and output_tokens must be positive integers")
    connection_hash = digest(json.dumps(connection, sort_keys=True).encode())
    context = context_source(request, root, system, connection_hash)
    remote_home = context[0]["remote_home"] if context else REMOTE_PREFIX + uuid.uuid4().hex
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    fixture = native = None
    try:
        fixture, tools = fixture_client(request, root, deadline)
        tool_hash = digest(json.dumps(tools, sort_keys=True).encode())
        if context and context[0].get("tools_sha256") != tool_hash:
            raise RunnerError("unsupported_context", "Fixture tool catalog changed across native fork")
        native = JsonProcess(connection_argv(connection, remote_home), root, "codex-native", deadline)
        native.call(
            "initialize",
            {"clientInfo": {"name": "lockbot_skill_eval", "version": "1"}, "capabilities": {"experimentalApi": True}},
        )
        native.send({"method": "initialized"})
        common = {
            "model": transport_model,
            "modelProvider": PROVIDER,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "cwd": remote_home + "/workspace",
            "baseInstructions": system,
            "developerInstructions": "Use only the supplied fixture tools. Do not delegate or access host resources.",
        }
        if context:
            old, history = context
            snapshot = native.call("thread/read", {"threadId": old["thread_id"], "includeTurns": True})
            verify_native_history(snapshot["thread"], history, old["turn_id"])
            started = native.call(
                "thread/fork",
                {**common, "threadId": old["thread_id"], "lastTurnId": old["turn_id"], "ephemeral": False},
            )
            if (
                started["thread"].get("forkedFromId") != old["thread_id"]
                or started["thread"].get("id") == old["thread_id"]
            ):
                raise RunnerError("unsupported_context", "Native fork did not identify its source")
            verify_native_history(started["thread"], history, old["turn_id"])
        else:
            started = native.call(
                "thread/start",
                {
                    **common,
                    "dynamicTools": tools,
                    "ephemeral": False,
                    "environments": [],
                    "allowProviderModelFallback": False,
                },
            )
        if started.get("model") != transport_model or started.get("modelProvider") != PROVIDER:
            raise RunnerError("native_model_mismatch", "Native client changed the requested model/provider")
        if started.get("instructionSources"):
            raise RunnerError("native_isolation_failure", "Ambient instruction sources were loaded")
        thread_id = started["thread"]["id"]
        turn = native.call(
            "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}], "environments": []}
        )["turn"]
        turn_id = turn["id"]
        callbacks = output_bytes = 0
        usage = {}
        usage_total = {}
        previous_usage = context[0].get("usage_total", {}) if context else {}
        limits = {
            "max_tool_continuations": request["max_turns"] - 1,
            "streamed_output_bytes": request["output_tokens"],
            "output_tokens_native_hard_cap": False,
            "hidden_reasoning_hard_cap": False,
            "timeout_seconds": timeout,
        }
        answers = []
        allowed = {tool["name"] for tool in tools}
        while True:
            message = native.backlog.pop(0) if native.backlog else native.receive()
            method, params = message.get("method"), message.get("params", {})
            if params.get("threadId", thread_id) != thread_id:
                raise RunnerError("native_isolation_failure", "Unexpected native thread event")
            if "id" in message and method:
                if (
                    method != "item/tool/call"
                    or params.get("tool") not in allowed
                    or fixture is None
                    or params.get("namespace") is not None
                    or params.get("turnId") != turn_id
                ):
                    native.send({"id": message["id"], "error": {"code": -32601, "message": "Tool not allowed"}})
                    raise RunnerError("native_isolation_failure", "Native client requested an unapproved tool")
                callbacks += 1
                if callbacks >= request["max_turns"]:
                    raise RunnerError("error_max_turns", "Tool continuation limit reached")
                result = fixture.call("tools/call", {"name": params["tool"], "arguments": params["arguments"]})
                # Preserve the full MCP envelope, including errors and structuredContent.
                native.send(
                    {
                        "id": message["id"],
                        "result": {
                            "success": not result.get("isError", False),
                            "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}],
                        },
                    }
                )
            elif method and method.endswith("/delta") and isinstance(params.get("delta"), str):
                output_bytes += len(params["delta"].encode())
                if output_bytes > request["output_tokens"]:
                    raise RunnerError("error_output_limit", "Conservative streamed output byte limit reached")
            elif method == "thread/tokenUsage/updated":
                usage_total = params["tokenUsage"]["total"]
                if any(value < previous_usage.get(key, 0) for key, value in usage_total.items()):
                    previous_usage = {}  # Some native versions reset counters on fork.
                usage = {key: value - previous_usage.get(key, 0) for key, value in usage_total.items()}
                if usage.get("outputTokens", 0) > request["output_tokens"]:
                    raise RunnerError("error_output_limit", "Observed output token limit exceeded")
            elif method == "item/started":
                if params.get("item", {}).get("type") not in {
                    "userMessage",
                    "agentMessage",
                    "reasoning",
                    "dynamicToolCall",
                }:
                    raise RunnerError("native_isolation_failure", "Native client started an unapproved built-in tool")
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    answers.append(item["text"])
                elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall"}:
                    raise RunnerError("native_isolation_failure", "Unexpected built-in tool execution")
            elif method == "turn/completed":
                if params["turn"].get("id") != turn_id or params["turn"].get("status") != "completed":
                    raise RunnerError("native_turn_failed", json.dumps(params["turn"].get("error")))
                break
        if not answers:
            raise RunnerError("native_protocol_error", "No final assistant message was received")
        answer = answers[-1]
        if len(answer.encode()) > request["output_tokens"]:
            raise RunnerError("error_output_limit", "Final response exceeds conservative output byte limit")
        save(
            root / "native-session.json",
            {
                "adapter": "training13-codex-app-server-v1",
                "model": request["model"],
                "transport_model": transport_model,
                "model_source": model_source,
                "provider": PROVIDER,
                "remote_home": remote_home,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "parent_thread_id": context[0]["thread_id"] if context else None,
                "system_sha256": digest(system.encode()),
                "connection_sha256": connection_hash,
                "prompt_sha256": digest(prompt.encode()),
                "answer_sha256": digest(answer.encode()),
                "tools_sha256": tool_hash,
                "dynamic_tool_calls": callbacks,
                "usage_total": usage_total,
                "limits": limits,
            },
        )
        response = {
            "text": answer,
            "turns": None,
            "usage": {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens")},
            "limits_enforcement": limits,
            "native_thread_id": thread_id,
            "model": request["model"],
            "transport_model": started["model"],
            "model_source": model_source,
            "provider": started["modelProvider"],
        }
        if context:
            response["conversation_sha256"] = request["conversation_sha256"]
        return response
    except BaseException:
        if native and "thread_id" in locals() and "turn_id" in locals():
            with contextlib.suppress(Exception):
                native.send(
                    {
                        "id": "interrupt",
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    }
                )
        raise
    finally:
        for client in (native, fixture):
            if client:
                client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True, type=Path, help="Private SSH/container connection JSON")
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    try:
        response = run(json.load(sys.stdin), json.loads(args.connection.read_text()), args.timeout)
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except (RunnerError, ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"kind": getattr(error, "kind", "runner_configuration_error"), "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
