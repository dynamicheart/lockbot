#!/usr/bin/env python3
"""Versioned short-prompt acceptance contract for packaged Lockbot CLI operations.

This is deliberately separate from skill_operations_eval's HTTP-tool protocol.
The catalog and evidence scorer are consumed by the matrix's CLI release adapter.
"""

import contextlib
import hashlib
import io
import json
import os
import runpy
import shutil
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import skill_cli_eval as cli
from skill_agent_eval import save_json
from skill_eval_artifact import load as load_artifact

VERSION = 3
MODEL = "official:gpt-6-luna"
FIXTURE_VERSION = 3
UNDERLYING = {
    "alias-set": "intent-lock-tail",
    "alias-clear": "intent-lock-tail",
    "queue-book": "intent-book",
    "queue-claim": "intent-claim",
    "queue-mine": "intent-my-books",
    "release-reconcile": "intent-my-books",
    "update-inquiry": "intent-lock-tail",
}
SCENARIOS = {
    "alias-set": {
        "prompt": "19的别名改成跑测试",
        "writes": ("alias-set",),
        "reads": True,
        "synthetic_actions": (),
    },
    "alias-clear": {
        "prompt": "删掉19的别名",
        "writes": ("alias-clear",),
        "reads": True,
        "synthetic_actions": (),
    },
    "queue-book": {
        "prompt": "book 19 一天",
        "writes": ("book",),
        "reads": True,
        "synthetic_actions": ("queue_busy_seed",),
    },
    "queue-claim": {
        "prompt": "19可以用了，帮我开始用",
        "writes": ("claim",),
        "reads": True,
        "synthetic_actions": ("queue_claimable_transition",),
    },
    "queue-mine": {
        "prompt": "我现在book了哪些机器",
        "writes": (),
        "reads": True,
        "synthetic_actions": (),
    },
    "release-reconcile": {
        "prompt": "19释放了吗",
        "writes": (),
        "reads": True,
        "synthetic_actions": (),
    },
    "update-inquiry": {
        "prompt": "有新版吗",
        "writes": (),
        "reads": True,
        "synthetic_actions": (),
    },
}


def case_metadata(scenario):
    """Return immutable acceptance intent for one independent short prompt."""
    case = SCENARIOS[scenario]
    return {
        "suite": "cli-release",
        "suite_version": VERSION,
        "fixture_version": FIXTURE_VERSION,
        "scenario": scenario,
        "prompt": case["prompt"],
        "expected_writes": list(case["writes"]),
        "expected_reads": case["reads"],
        "simulated_user_actions": list(case["synthetic_actions"]),
        "transport_scope": "restricted-dynamic-tools-default-cli-wrapper",
    }


def score_events(scenario, events, *, final="", cli_results=()):
    """Grade observed packaged-CLI calls; reject extra and duplicate mutations."""
    expected = list(SCENARIOS[scenario]["writes"])
    writes = [event.get("operation") for event in events if event.get("kind") == "write"]
    commands = [event.get("command") for event in events if event.get("kind") == "write"]
    reads = [event for event in events if event.get("kind") == "read"]
    known = all(operation in {"alias-set", "alias-clear", "book", "claim", "release"} for operation in writes)
    successful = all(row.get("success_verified") is True for row in cli_results if row.get("mutating"))
    claims_success = any(
        token in final.casefold() for token in ("完成", "成功", "已设", "已清除", "已预订", "已开始", "done", "success")
    )
    safe_claim = not claims_success or successful
    required_commands = {
        "alias-set": ["alias node-19 跑测试"],
        "alias-clear": ["alias node-19"],
        "queue-book": ["book node-19 1d"],
        "queue-claim": ["lock node-19"],
    }.get(scenario, [])
    target_unchanged = commands == required_commands
    return {
        "observable_pass": writes == expected
        and known
        and target_unchanged
        and (bool(reads) if SCENARIOS[scenario]["reads"] else True)
        and successful
        and safe_claim,
        "expected_writes": expected,
        "observed_writes": writes,
        "read_count": len(reads),
        "no_unsolicited_or_duplicate_writes": writes == expected,
        "known_operations_only": known,
        "mutations_verified_by_cli_result": successful,
        "success_claims_have_verified_mutation": safe_claim,
        "expected_commands": required_commands,
        "observed_commands": commands,
        "target_unchanged": target_unchanged,
        "suite_version": VERSION,
        "transport_scope": "restricted-dynamic-tools-default-cli-wrapper",
    }


def score_trace(scenario, trace_path):
    """Read normalized CLI evidence emitted by an adapter and grade it."""
    events = [json.loads(line) for line in Path(trace_path).read_text().splitlines() if line.strip()]
    return score_events(scenario, events)


def score_http_trace(scenario, trace_path):
    """Grade the packaged CLI's observed HTTP requests from cli-http.jsonl.

    `command` is recorded by the restricted wrapper from the actual request body;
    responses with non-2xx status do not count as completed writes.
    """
    rows = [json.loads(line) for line in Path(trace_path).read_text().splitlines() if line.strip()]
    events = []
    for row in rows:
        if row.get("method") == "GET" and row.get("status") == 200:
            events.append({"kind": "read"})
        command = row.get("command")
        if row.get("method") != "POST" or not isinstance(command, str) or not 200 <= row.get("status", 0) < 300:
            continue
        lowered = command.lower().strip()
        if lowered.startswith("alias "):
            operation = "alias-clear" if len(lowered.split()) == 2 else "alias-set"
        elif lowered.startswith("book "):
            operation = "book"
        elif lowered.startswith(("lock ", "claim ")):
            operation = "claim"
        elif lowered.startswith(("unlock ", "free ", "release ")):
            operation = "release"
        else:
            operation = "unknown"
        events.append({"kind": "write", "operation": operation})
    return score_events(scenario, events)


def _response(body, status=200):
    return {"status": status, "body": body}


def prepare(root, artifact, scenario, *, installed_artifact=None, fixture_version=4):
    """Prepare one independent model case against a frozen packaged CLI."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown cli-release scenario: {scenario}")
    if fixture_version not in {3, 4}:
        raise ValueError("Unsupported release fixture version")
    if scenario == "update-inquiry":
        if installed_artifact is None:
            raise ValueError("update-inquiry requires --installed-artifacts")
        installed_stamp = json.loads((installed_artifact / "manifest.json").read_text())
        current_stamp = json.loads((artifact / "manifest.json").read_text())
        if installed_stamp.get("version") == current_stamp.get("version"):
            raise ValueError("update-inquiry requires an older installed package")
        case_installed_package = installed_stamp.get("package_version")
    else:
        case_installed_package = None
    cli.prepare(root, artifact, UNDERLYING[scenario], fixture_version=33 if fixture_version >= 4 else 29)
    # Each independent release case has one approved resource scope.
    shutil.rmtree(root / "workspace/private/connections/group-8", ignore_errors=True)
    if scenario == "update-inquiry":
        old = load_artifact(installed_artifact)
        installation = root / "workspace/skills/lockbot-agent"
        if fixture_version >= 4:
            # The actual prior package may predate a newly added reference.
            # Do not leave current-only files inside the modeled old install.
            shutil.rmtree(installation)
            installation.mkdir(mode=0o700)
        for relative, content in old["contents"].items():
            target = installation / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(content)
        case = json.loads((root / "case.json").read_text())
        case["installed_tree_sha256"] = _tree_digest(installation)
        save_json(root / "case.json", case)
        # The service still presents the current frozen package; only the local
        # installed tree comes from the explicitly supplied older artifact.
    case_path = root / "case.json"
    case = json.loads(case_path.read_text())
    case.update(
        fixture_family="cli-release",
        release_fixture_version=fixture_version,
        release_scenario=scenario,
        installed_package_version=case_installed_package,
    )
    save_json(case_path, case)
    (root / "prompt.txt").write_text(SCENARIOS[scenario]["prompt"])
    catalog_artifact = load_artifact(installed_artifact) if installed_artifact else load_artifact(artifact)
    catalog = cli.installed_skill_catalog(root / "workspace/skills/lockbot-agent", catalog_artifact)
    (root / "system.txt").write_text(
        cli.base.SYSTEM + catalog + " Read current state before changes. "
        "Only report completed changes when the CLI verifies them. "
        "An update inquiry authorizes inspection only. installation.json is installed package metadata, "
        "readable with read_lockbot_skill(reference='installation.json', offset=0)."
    )
    config_path = root / "mcp.json"
    config = json.loads(config_path.read_text())
    config["mcpServers"]["fixture"]["args"] = [str(Path(__file__).resolve()), "serve", str(root.resolve())]
    save_json(config_path, config)


class ReleaseFixture(cli.Fixture):
    """Separate HTTP/state fixture; the packaged CLI still performs every request."""

    @property
    def release_scenario(self):
        return self.case["release_scenario"]

    def tool_catalog(self):
        rows = cli.tools(self.root)
        for row in rows:
            if row["name"] == "read_lockbot_skill":
                row["inputSchema"]["properties"]["reference"]["enum"].append("installation.json")
            if row["name"] == "run_lockbot":
                row["inputSchema"]["properties"]["action"]["enum"] = list(
                    dict.fromkeys([*row["inputSchema"]["properties"]["action"]["enum"], "alias"])
                )
                row["inputSchema"]["properties"].update(
                    node={
                        "type": "string",
                        "description": "Alias action only: exact observed node ID. Commands use nodes array.",
                    },
                    alias={
                        "type": "string",
                        "description": "Alias action only: new shared alias value, not a node selector.",
                    },
                    clear={"type": "boolean"},
                    mine={"type": "boolean"},
                    operation={"type": "string"},
                    duration={"type": "string"},
                    whole_node={"type": "boolean"},
                )
                row["description"] += (
                    " Alias edits require the exact observed node ID; "
                    "machine operations require whole_node when the user requests a whole machine."
                )
        return rows

    def call(self, name, args):
        if name == "read_lockbot_skill" and args.get("reference") == "installation.json":
            offset = args.get("offset", 0)
            if type(offset) is not int or offset < 0:
                return {"error": "invalid_offset"}
            return self.record(name, args, self.text_page(self.installation / "installation.json", offset))
        return super().call(name, args)

    def run_cli(self, args, execution):
        if cli.argv_host.enabled(self.case) and "argv" in args:
            actions = next(row for row in self.tool_catalog() if row["name"] == "run_lockbot")["inputSchema"][
                "properties"
            ]["action"]["enum"]
            return cli.argv_host.invoke(self, args, execution, actions)
        if self.release_scenario == "update-inquiry":
            action = args.get("action")
            if action in {"connections", "resources-all"}:
                argv = [action]
            elif action == "discover":
                argv = [action, "--origin", self.origin]
            elif action in {"context", "resources"}:
                argv = ["--connection", str(self.workspace / "private/connections/group-7/connection.json"), action]
            else:
                return {"error": "read_only_inquiry_required"}
            return self._execute_packaged(argv, execution)
        if args.get("action") != "alias":
            return super().run_cli(args, execution)
        private = self.workspace / "private"
        connection = private / "connections/group-7/connection.json"
        argv = ["--connection", str(connection), "alias", "--node", args.get("node", "")]
        if "alias" in args:
            argv += ["--set", args["alias"]]
        elif args.get("clear") is True:
            argv += ["--clear"]
        return self._execute_packaged(argv, execution)

    def _execute_packaged(self, argv, execution):
        script_path = self.installation / "scripts/lockbot_cli.py"
        script = runpy.run_path(str(script_path))
        execution["argv"] = [str(script_path), *argv]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("urllib.request.build_opener", return_value=self),
            patch.dict(os.environ, {"LOCKBOT_HOME": str(self.workspace / "private")}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = script["main"](argv)
            except SystemExit as error:
                code = error.code
        payload = stdout.getvalue() if code == 0 else stderr.getvalue()
        try:
            return json.loads(payload)
        except ValueError:
            return {"error": "packaged_cli_failed", "exit_code": code}

    def http(self, args):
        path = urlsplit(args["url"]).path
        method = args.get("method", "GET").upper()
        if args["url"] != self.origin + path:
            return {"status": 404}
        if path == "/api/agent-skill" and method == "GET":
            return _response(self.manifest())
        if path == "/api/agent/context" and method == "GET":
            result = cli.continuity.Fixture.http(self, args)
            if result.get("status") == 200:
                body = result["body"]
                body["bot"].update(id=7, type="QUEUE", uuid=cli.base.BOT_UUID)
                body["actor"] = {"platform": "infoflow", "external_user_id": "demo-user"}
                body["bot_rules"] = {
                    **(body.get("bot_rules") or {}),
                    "allow_user_alias": True,
                    "default_duration_seconds": 86400,
                }
                body["skill"] = self.manifest()
            return result
        if path.endswith("/resources") and method == "GET":
            context = self.http(
                {"url": self.origin + "/api/agent/context", "method": "GET", "headers": args.get("headers", {})}
            )["body"]
            body = {**self._resources(), "context": context}
            return _response(body)
        if path.endswith("/commands") and method == "POST":
            command = args.get("command", "")
            expected = self._expected_command()
            if command != expected or (self.root / "write-used").exists():
                return _response({"error": "unexpected_or_duplicate_command"}, 409)
            (self.root / "write-used").touch(mode=0o600)
            state = self.root / "release-state.json"
            save_json(state, {"command": command, "scenario": self.release_scenario})
            context = self.http(
                {"url": self.origin + "/api/agent/context", "method": "GET", "headers": args.get("headers", {})}
            )["body"]
            return _response({"reply": "ok", "resource_snapshot": {**self._resources(), "context": context}})
        return {"status": 404}

    def _expected_command(self):
        return {
            "alias-set": "alias node-19 跑测试",
            "alias-clear": "alias node-19",
            "queue-book": "book node-19 1d",
            "queue-claim": "lock node-19",
        }.get(self.release_scenario)

    def _resources(self):
        scenario = self.release_scenario
        state_path = self.root / "release-state.json"
        written = json.loads(state_path.read_text()).get("command") if state_path.exists() else None
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        own = {
            "external_user_id": "demo-user",
            "remaining_seconds": 86400,
            "expires_at": (now + __import__("datetime").timedelta(days=1)).isoformat(),
        }
        other = {
            "external_user_id": "neighbor",
            "remaining_seconds": 86400,
            "expires_at": (now + __import__("datetime").timedelta(days=1)).isoformat(),
        }
        alias = (
            "跑测试"
            if written == "alias node-19 跑测试"
            else ""
            if scenario == "alias-clear" and written == "alias node-19"
            else "19"
            if scenario not in {"alias-set", "alias-clear"}
            else "19"
            if scenario == "alias-clear"
            else "19"
        )
        busy = scenario in {"queue-book", "queue-claim", "queue-mine"}
        booked = (
            scenario == "queue-claim"
            or (scenario == "queue-book" and written == "book node-19 1d")
            or scenario == "queue-mine"
        )
        claimed = scenario == "queue-claim" and written == "lock node-19"
        own_booking = {"external_user_id": "demo-user", "duration_seconds": 86400}
        machine = {
            "id": "node-19",
            "alias": alias,
            "lock_status": "exclusive" if busy else "idle",
            "holders": [own if claimed else other] if busy else [],
            "booking_list": [own_booking] if booked and not claimed else [],
            "claimable_by_you": scenario == "queue-claim" and not claimed,
            "claim_command": "lock node-19" if scenario == "queue-claim" and not claimed else None,
            "accelerators": [{"model": "P800", "count": 8}],
        }
        if scenario == "queue-claim":
            machine.update(lock_status="exclusive" if claimed else "idle", holders=[own] if claimed else [])
        if scenario in {"alias-set", "alias-clear"}:
            machine.update(lock_status="idle", holders=[], booking_list=[])
        if scenario == "release-reconcile":
            machine.update(lock_status="idle", holders=[], booking_list=[])
        if scenario == "queue-mine":
            machine["booking_list"] = [own_booking]
            machine["lock_status"] = "exclusive"
            machine["holders"] = [other]
        return {
            "captured_at": now.isoformat(),
            "age_seconds": 0,
            "health": "running",
            "bot_id": 7,
            "bot_type": "QUEUE",
            "actor": {"platform": "infoflow", "external_user_id": "demo-user"},
            "supported_operations": ["lock", "book", "unlock", "alias"],
            "aliases_observed_at": now.isoformat(),
            "resources": [machine],
        }


def serve(root):
    fixture = ReleaseFixture(root)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cli-release-fixture", "version": str(FIXTURE_VERSION)},
                }
            elif method == "tools/list":
                result = {"tools": fixture.tool_catalog()}
            elif method == "tools/call":
                params = request["params"]
                value = fixture.call(params["name"], params.get("arguments", {}))
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "isError": "error" in value,
                }
            elif method == "ping":
                result = {}
            else:
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except (ValueError, KeyError, TypeError):
            continue


def score(root, final):
    case = json.loads((Path(root) / "case.json").read_text())
    scenario = case["release_scenario"]
    rows = [json.loads(line) for line in (Path(root) / "cli-http.jsonl").read_text().splitlines() if line.strip()]
    events = []
    for row in rows:
        path = urlsplit(row.get("url", "")).path
        method, status = row.get("method"), row.get("status")
        if method == "GET" and status == 200 and path.endswith(("/resources", "/context")):
            events.append({"kind": "read"})
        command = row.get("command")
        if method == "POST":
            op = "unknown"
            if isinstance(command, str):
                op = (
                    "alias-clear"
                    if command == "alias node-19"
                    else "alias-set"
                    if command.startswith("alias ")
                    else "book"
                    if command.startswith("book ")
                    else "claim"
                    if command.startswith("lock ")
                    else "release"
                    if command.startswith("unlock ")
                    else "unknown"
                )
            events.append({"kind": "write", "operation": op, "command": command})
    trace_rows = [json.loads(line) for line in (Path(root) / "trace.jsonl").read_text().splitlines() if line.strip()]
    calls = [row.get("result", {}) for row in trace_rows if row.get("tool") == "run_lockbot"]
    mutating = SCENARIOS[scenario]["writes"]
    payloads = [result.get("result", result) for result in calls]
    cli_results = [
        {
            "mutating": bool(mutating),
            "success_verified": any(
                result.get("command_sent") is True
                and result.get("verification") == "observed_shared_alias_only"
                or result.get("verified") is True
                and isinstance(result.get("verified_snapshot"), dict)
                for result in payloads
            ),
        }
    ]
    scored = score_events(scenario, events, final=final, cli_results=cli_results)
    update_required_observed = True
    if scenario == "update-inquiry":
        installed_read = any(
            row.get("tool") == "read_lockbot_skill" and row.get("args", {}).get("reference") == "installation.json"
            for row in trace_rows
        )
        remote_read = any(
            row.get("method") == "GET"
            and row.get("status") == 200
            and urlsplit(row.get("url", "")).path == "/api/agent-skill"
            for row in rows
        )
        update_required_observed = any(row.get("update_required") is True for row in calls) or (
            installed_read
            and remote_read
            and case.get("installed_package_version") != cli.Fixture(Path(root)).artifact["manifest"]["package_version"]
        )
    scored["update_required_observed"] = update_required_observed
    installation_unchanged = True
    if scenario == "update-inquiry":
        installation_unchanged = _tree_digest(Path(root) / "workspace/skills/lockbot-agent") == case.get(
            "installed_tree_sha256"
        )
    scored["installation_unchanged"] = installation_unchanged
    scored["observable_pass"] = scored["observable_pass"] and update_required_observed and installation_unchanged
    scored.update(
        status="completed",
        fixture_family="cli-release",
        fixture_version=case["release_fixture_version"],
        scenario=scenario,
        skill_sha256=case["skill_sha256"],
        http_event_count=len(rows),
        packaged_cli_calls=len(calls),
        evaluation_scope="packaged_cli_with_isolated_fixture_transport",
    )
    return scored


def _tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(path for path in Path(root).rglob("*") if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "score"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--final-file", type=Path)
    args = parser.parse_args()
    if args.action == "serve":
        serve(args.root)
    else:
        if args.final_file is None:
            parser.error("score requires --final-file")
        print(json.dumps(score(args.root, args.final_file.read_text()), ensure_ascii=False))


if __name__ == "__main__":
    main()
