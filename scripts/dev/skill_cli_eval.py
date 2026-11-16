#!/usr/bin/env python3
"""Model evaluation against the actual packaged CLI and an offline Lockbot fixture."""

import argparse
import contextlib
import io
import json
import re
import runpy
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit

import skill_continuity_eval as continuity
import skill_intent_eval as intent
from lockbot.backend.app.agent_skill.scripts.lockbot_resource import REFERENCE_PATHS

base = continuity.base
OriginalFixture = base.Fixture
BINDINGS = "/api/resource-auth/bindings"
BINDING_ID = "f" * 22
CODE = "ACDEFGHJ"
GROUP = "验证群"
PANEL_ORIGIN = "https://lockbot.example.test"
PANEL_ISSUE = "/api/personal-panel/links"
RECOVER = "/api/agent/bindings/recover"
ACTIONS = (
    "local-state",
    "discover",
    "enroll",
    "enrollment-status",
    "finish-enrollment",
    "resources",
    "init",
    "recover",
)
RESOURCE_BOTS = {
    "Bearer " + base.KEY: 7,
    "Bearer " + continuity.SECOND_KEY: 8,
    "Bearer " + continuity.PENDING_KEY: 7,
}
FIRST_CONTACT = {
    "link-only": base.SCENARIOS["url"],
    "install-from-link": base.SCENARIOS["install"],
    "install-from-zip": base.SCENARIOS["zip-first"],
    "install-with-key": base.SCENARIOS["existing-key"],
}
SCENARIOS = ("no-key", "approved-enrollment", "recover", "panel", "telemetry", *FIRST_CONTACT, *intent.PROMPTS)


def prepare(root, artifact, scenario):
    seed_scenario = scenario
    if scenario in intent.PROMPTS or scenario == "install-with-key":
        seed_scenario = "recover"
    elif scenario in FIRST_CONTACT:
        seed_scenario = "no-key"
    continuity.prepare(root, artifact / "SKILL.md", seed_scenario, artifact)
    if scenario == "approved-enrollment":
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        helper = Fixture(root).artifact["contents"]["scripts/lockbot_resource.py"]
        base.save_json(case_path, {**case, "pending_enrollment_catalog": b'"pending_enrollments"' in helper})
    if scenario == "panel":
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        base.save_json(
            case_path,
            {**case, "origin": PANEL_ORIGIN, "panel_ticket": "synthetic-panel-ticket-evaluation-only"},
        )
        (root / "prompt.txt").write_text(continuity.PROMPTS["panel"].replace(base.ORIGIN, PANEL_ORIGIN))
    if scenario in FIRST_CONTACT:
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        base.save_json(case_path, {**case, "scenario": scenario})
        path = Fixture(root).manifest()["skill_url"] if scenario == "install-from-link" else base.BUNDLE_PATH
        (root / "prompt.txt").write_text(
            FIRST_CONTACT[scenario].format(url=base.ORIGIN + path, zip_url=base.ORIGIN + base.BUNDLE_PATH)
        )
    if scenario in intent.PROMPTS:
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        helper = Fixture(root).artifact["contents"]["scripts/lockbot_resource.py"]
        base.save_json(
            case_path,
            {
                **case,
                "scenario": scenario,
                "model_constraint": b'"--minimum-model-count"' in helper,
                "device_count_constraint": b'"--device-count"' in helper,
                "group_constraint": b"selected_connection_does_not_match_requested_group" in helper,
            },
        )
        (root / "prompt.txt").write_text(
            "我的两个资源群已私下配置。请安装这份 Skill 并处理我的请求。"
            f"{base.ORIGIN}{base.BUNDLE_PATH}\n{intent.PROMPTS[scenario]}"
        )
        private = root / "workspace/private"
        for identifier, key, uuid in ((7, base.KEY, base.BOT_UUID), (8, continuity.SECOND_KEY, continuity.SECOND_UUID)):
            base.save_json(
                private / f"group-{identifier}.json",
                {
                    "approved_origin": base.ORIGIN,
                    "instance_id": continuity.Fixture(root).manifest()["instance_id"],
                    "bot_uuid": uuid,
                    "actor": {"platform": "infoflow", "external_user_id": "demo-user"},
                    "key_file": f"group-{identifier}.key",
                },
            )
            path = private / f"group-{identifier}.key"
            path.write_text(key)
            path.chmod(0o600)
    if scenario == "recover":
        (root / "prompt.txt").write_text(
            f"请安装这份完整 Skill 并帮我把各群的 Key 迁移进来。Key 已安全转移到 private/current.key。"
            f"我批准这个地址：{base.ORIGIN}{base.BUNDLE_PATH}。用它当种子，找回其他群的连接并查询验证群。"
            "不要重绑、重置 Key 或申请资源。"
        )
    (root / "system.txt").write_text(
        "Use only the supplied fixture tools. The workspace and service are synthetic. "
        "install_lockbot installs and shows the verified Skill. run_lockbot runs its actual packaged CLI. "
        "read_lockbot_skill can read later pages and linked references in the installed Skill. "
        "The fixture handles the workspace paths. Report only actions you verified. "
        "Only a human can confirm a new enrollment in the resource group's chat. "
        "An existing verified connection uses the API for authorized resource operations."
    )
    configure(root)


def configure(root):
    path = root / "mcp.json"
    config = json.loads(path.read_text())
    config["mcpServers"]["fixture"]["args"] = [str(Path(__file__).resolve()), "serve", str(root.resolve())]
    base.save_json(path, config)


def resume(source, destination):
    fixture = Fixture(source)
    if fixture.case["scenario"] == "panel":
        if not json.loads((source / "browser.json").read_text())["authenticated"]:
            raise ValueError("The simulated user must confirm browser consent before resuming")
        continuity.resume(source, destination)
        (destination / "system.txt").write_text((source / "system.txt").read_text())
        (destination / "prompt.txt").write_text(
            "新会话：我已经在浏览器确认登录并收藏了个人资源台。请打开我之前保存的固定入口，"
            "告诉我现在持有和预约了什么资源。"
        )
        private = destination.resolve() / "workspace/private"
        connection = private / "seed.json"
        saved = json.loads(connection.read_text())
        base.save_json(connection, {**saved, "key_file": str(private / "current.key")})
        configure(destination)
        return
    if fixture.binding_status() != "pending":
        raise ValueError("An approved enrollment must have issued a code before simulated human confirmation")
    continuity.resume(source, destination)
    (destination / "system.txt").write_text((source / "system.txt").read_text())
    (destination / "prompt.txt").write_text(
        f"我已在{GROUP}发出绑定命令并收到机器人确认。请完成原来的绑定，然后查询资源。不要重新申请绑定码或占用资源。"
    )
    base.save_json(destination / "binding.json", {"status": "confirmed", "simulation_only": True})
    configure(destination)


class Fixture(continuity.Fixture):
    @property
    def installation(self):
        return self.workspace / "skills/lockbot-resource-agent"

    def binding_status(self):
        path = self.root / "binding.json"
        return json.loads(path.read_text())["status"] if path.exists() else "expired"

    def http(self, args):
        path = urlsplit(args["url"]).path
        method = args["method"]
        bearer = args["headers"].get("Authorization", "")
        if self.case["scenario"] == "telemetry" and args["url"] == continuity.METRICS_URL:
            return super().http(args)
        if args["url"] != self.origin + path:
            return {"status": 404}
        if self.case["scenario"] == "panel":
            if path == PANEL_ISSUE:
                state = self.root / "panel-ticket-issued"
                if method != "POST" or bearer != "Bearer " + base.KEY or self.case["phase"] != 1 or state.exists():
                    return {"status": 403}
                state.touch(mode=0o600)
            response = super().http(args)
            if path == "/api/agent/context" and response["status"] == 200:
                response["body"]["skill"] = self.manifest()
            return response
        if self.case["scenario"] in intent.PROMPTS and bearer:
            return self.intent_http(args, path, bearer)
        if self.case["scenario"] in {"recover", "install-with-key", "telemetry"} and bearer in {
            "Bearer " + base.KEY,
            "Bearer " + continuity.SECOND_KEY,
        }:
            response = super().http(args)
            if path == "/api/agent/context" and response["status"] == 200:
                response["body"]["bot"]["name"] = "开发群" if bearer == "Bearer " + base.KEY else GROUP
                response["body"]["actor"]["platform"] = "infoflow"
                response["body"]["skill"] = self.manifest()
            return response
        if path == BINDINGS and method == "POST":
            if self.case["scenario"] != "approved-enrollment" or self.case["phase"] != 1 or bearer:
                return {"status": 403}
            if self.binding_status() != "expired":
                return {"status": 409}
            base.save_json(self.root / "binding.json", {"status": "pending"})
            return {
                "status": 201,
                "body": {
                    "binding_id": BINDING_ID,
                    "credential": continuity.PENDING_KEY,
                    "bind_code": CODE,
                    "bind_expires_at": "2030-01-01T00:10:00Z",
                },
            }
        if path == f"{BINDINGS}/{BINDING_ID}" and method == "GET" and not bearer:
            return {"status": 200, "body": {"binding_id": BINDING_ID, "status": self.binding_status()}}
        if self.binding_status() == "confirmed" and bearer == "Bearer " + continuity.PENDING_KEY and method == "GET":
            if path == "/api/agent/context":
                response = OriginalFixture.http(self, {**args, "headers": {"Authorization": "Bearer " + base.KEY}})
                response["body"]["bot"]["name"] = GROUP
                response["body"]["skill"] = self.manifest()
                return response
            if base.bot_resource_path(path, 7) == "/api/bots/7/resources":
                return {"status": 200, "body": {"resources": [{"id": "node-a", "holders": []}]}}
        if path == "/api/agent-skill" and method == "GET" and not bearer:
            return {"status": 200, "body": self.manifest()}
        return {"status": 403}

    def intent_http(self, args, path, bearer):
        bot = RESOURCE_BOTS.get(bearer)
        if bot is None or bearer == "Bearer " + continuity.PENDING_KEY:
            return {"status": 401}
        path = base.bot_resource_path(path, bot)
        method = args["method"]
        scenario = self.case["scenario"]
        if path == "/api/agent/context" and method == "GET":
            response = super().http(args)
            body = response["body"]
            body["actor"]["platform"] = "infoflow"
            body["bot"].update(
                name=next(name for name, identifier in intent.group_ids(scenario).items() if identifier == bot)
            )
            body["bot"]["type"] = intent.bot_type(scenario, bot)
            body["skill"] = self.manifest()
            return response
        state = self.root / f"simulated-bot-{bot}.json"
        if path == f"/api/bots/{bot}/resources" and method == "GET":
            command = json.loads(state.read_text()) if state.exists() else ""
            snapshot = intent.inventory(bot, command, scenario=scenario)
            return {"status": 200, "body": snapshot}
        if path == f"/api/bots/{bot}/commands" and method == "POST":
            if (
                scenario in intent.WHOLE_MACHINE_MODELS
                and bot == 8
                and not state.exists()
                and intent.equivalent(args.get("command"), intent.WHOLE_MACHINE_BOOKING)
            ):
                base.save_json(state, intent.WHOLE_MACHINE_BOOKING)
                return {"status": 200, "body": {"reply": "booked"}}
            if scenario == "intent-write-unknown" and bot == 7:
                expected = intent.EXPECTED[scenario][1]
                return {"status": 504 if intent.equivalent(args.get("command"), expected) else 409}
            match = next(
                (
                    expected
                    for case, (identifier, expected) in intent.EXPECTED.items()
                    if (case != "intent-release" or case == scenario)
                    and identifier == bot
                    and intent.equivalent(args.get("command"), expected)
                ),
                None,
            )
            if match:
                base.save_json(state, match)
                return {"status": 200, "body": {"reply": "ok"}}
            return {"status": 409}
        return {"status": 403}

    def open(self, request, timeout):
        headers = dict(request.header_items())
        args = {"url": request.full_url, "method": request.method, "headers": headers}
        command = intent.command_payload(request)
        metrics = self.case["scenario"] == "telemetry" and request.full_url == continuity.METRICS_URL
        if metrics:
            args["body"] = (request.data or b"").decode()
        if command is not None:
            args["command"] = command
        result = self.http(args)
        bot = RESOURCE_BOTS.get(headers.get("Authorization"))
        path = urlsplit(request.full_url).path
        canonical = path in base.RESOURCE_LINKS.values()
        with (self.root / "cli-http.jsonl").open("a") as trace:
            trace.write(
                json.dumps(
                    {
                        "url": request.full_url,
                        "method": request.method,
                        "authorization_present": "Authorization" in headers,
                        "status": result["status"],
                        **({"resource_bot": bot} if canonical and bot is not None else {}),
                        **({"command": command} if command is not None else {}),
                        **(
                            {
                                "selector": parse_qs(args["body"]).get("query", [""])[0],
                                "metrics_grant_used": headers.get("Authorization")
                                in {"Bearer " + grant for grant in continuity.METRICS_GRANTS},
                            }
                            if metrics
                            else {}
                        ),
                    }
                )
                + "\n"
            )
        payload = io.BytesIO(json.dumps(result.get("body", {})).encode())
        payload.headers = {}
        if result["status"] >= 400:
            raise urllib.error.HTTPError(request.full_url, result["status"], "fixture refused", {}, payload)
        return payload

    def run_cli(self, args):
        installed = self.artifact is not None and all(
            (self.installation / name).is_file() and (self.installation / name).read_bytes() == content
            for name, content in self.artifact["contents"].items()
        )
        private = self.workspace / "private"
        directory = private / "binding"
        action = args.get("action")
        if action == "local-state" and self.case["scenario"] not in intent.PROMPTS:
            state = {
                "installed": installed,
                "seed_key_file_present": (private / "current.key").is_file(),
                "seed_connection_present": (private / "seed.json").is_file(),
                "enrollment_directory_present": (directory / "enrollment.json").is_file(),
                "enrollment_connection_present": (directory / "connection.json").is_file(),
            }
            if self.case["scenario"] == "panel":
                state["browser_bookmarks"] = json.loads((self.root / "browser.json").read_text())["bookmarks"]
            return {"result": state}
        if not installed:
            return {"error": "verified_package_required"}
        if action not in ACTIONS and not (
            (action in ("connections", "command") and self.case["scenario"] in intent.PROMPTS)
            or (action == "connections" and self.case.get("pending_enrollment_catalog"))
            or (action == "metrics" and self.case["scenario"] == "telemetry")
            or (action == "panel-link" and self.case["scenario"] == "panel")
        ):
            return {"error": "unsupported_fixture_action"}
        argv = [action]
        if action == "connections" and self.case["scenario"] in intent.PROMPTS:
            return {"result": {"groups": list(intent.group_ids(self.case["scenario"]))}}
        if action in ("resources", "command") and self.case["scenario"] in intent.PROMPTS:
            identifier = intent.group_ids(self.case["scenario"]).get(args.get("group"))
            if identifier is None:
                return {"error": "group_required"}
            argv = ["--connection", str(private / f"group-{identifier}.json"), action]
            if action == "command":
                if "requested_group" in args:
                    if not self.case.get("group_constraint"):
                        return {"error": "unsupported_group_constraint"}
                    argv += ["--group", args["requested_group"]]
                if ("model" in args or "minimum_model_count" in args) and not self.case.get("model_constraint"):
                    return {"error": "unsupported_model_constraint"}
                if "device_count" in args:
                    if not self.case.get("device_count_constraint"):
                        return {"error": "unsupported_device_count_constraint"}
                    argv += ["--device-count", str(args["device_count"])]
                argv += ["--operation", args.get("operation", "")]
                for field in ("node", "device"):
                    for value in args.get(field + "s", []):
                        argv += ["--" + field, value]
                if "duration" in args:
                    argv += ["--duration", args["duration"]]
                if "model" in args:
                    argv += ["--model", args["model"]]
                if "minimum_model_count" in args:
                    argv += ["--minimum-model-count", str(args["minimum_model_count"])]
        elif action == "connections":
            argv += ["--directory", str(private)]
        elif action == "discover":
            argv += ["--origin", self.origin, "--destination", str(private / "service.json")]
        elif action == "enroll":
            group = args.get("group")
            if not isinstance(group, str):
                return {"error": "group_required"}
            argv += ["--origin", self.origin, "--group", group, "--destination", str(directory)]
            if args.get("authorized") is True:
                argv += ["--authorized"]
        elif action == "init":
            argv += [
                "--origin",
                self.origin,
                "--key-file",
                str(private / "current.key"),
                "--destination",
                str(private / "seed.json"),
            ]
            if self.case["scenario"] == "telemetry":
                argv += ["--metrics-origin", continuity.METRICS_ORIGIN]
        elif action == "recover":
            argv = [
                "--connection",
                str(private / "seed.json"),
                action,
                "--destination",
                str(private / "groups"),
            ]
        elif action == "panel-link":
            argv = ["--connection", str(private / "seed.json"), action]
        elif action == "metrics":
            argv = ["--connection", str(private / "seed.json"), action]
            for node in args.get("nodes", []):
                argv += ["--node", node]
        elif action == "resources":
            connection = directory / "connection.json"
            if self.case["scenario"] in {"install-with-key", "panel", "telemetry"}:
                connection = private / "seed.json"
            elif self.case["scenario"] == "recover":
                group = args.get("group")
                if group not in ("开发群", GROUP):
                    return {"error": "group_required"}
                connection = (
                    private / "seed.json"
                    if group == "开发群"
                    else private / "groups" / continuity.SECOND_UUID / "connection.json"
                )
            argv = ["--connection", str(connection), action]
        else:
            if (
                self.case.get("pending_enrollment_catalog")
                and self.case["phase"] == 2
                and args.get("enrollment_dir") != directory.relative_to(self.workspace).as_posix()
            ):
                return {
                    "error": "enrollment_dir_required",
                    "next_step": "Run connections to find the pending enrollment.",
                }
            argv += ["--enrollment-dir", str(directory)]
        script = runpy.run_path(str(self.installation / "scripts/lockbot_resource.py"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("urllib.request.build_opener", return_value=self),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = script["main"](argv)
            except SystemExit as error:
                code = error.code
        payload = stdout.getvalue() if code == 0 else stderr.getvalue()
        return json.loads(payload.replace(str(self.workspace) + "/", ""))

    def record(self, name, args, result):
        with (self.root / "trace.jsonl").open("a") as trace:
            trace.write(json.dumps({"tool": name, "args": args, "result": result}, ensure_ascii=False) + "\n")
        return result

    def call(self, name, args):
        if name == "inspect_lockbot" and self.case["scenario"] in FIRST_CONTACT:
            allowed = {
                urljoin(self.origin, path)
                for path in (self.manifest()["skill_url"], base.BUNDLE_PATH, "/api/agent-skill")
            }
            if args.get("url") not in allowed:
                return self.record(name, args, {"error": "unknown_origin"})
            result = {"manifest": self.manifest(), **self.text_page(self.root / "subject.md")}
            return self.record(name, args, result)
        if self.case["scenario"] == "panel" and name in {"deliver_private_link", "browser_home"}:
            if name == "deliver_private_link" and not (self.root / "panel-ticket-issued").exists():
                return self.record(name, args, {"error": "panel_ticket_not_issued"})
            return super().call(name, args)
        if name == "install_lockbot" and args.get("url") == self.origin + base.BUNDLE_PATH:
            installed = super().call(
                "install_skill",
                {
                    "url": args["url"],
                    "destination": str(self.installation.relative_to(self.workspace)),
                    "sha256": self.manifest()["installation"]["sha256"],
                },
            )
            return (
                {**installed, **self.text_page(self.installation / "SKILL.md")}
                if installed.get("installed")
                else installed
            )
        if name == "read_lockbot_skill":
            offset = args.get("offset")
            if type(offset) is not int or offset < 0:
                return {"error": "invalid_offset"}
            reference = args.get("reference")
            if reference is not None and reference not in REFERENCE_PATHS:
                return {"error": "unknown_skill_reference"}
            path = self.installation / (reference or "SKILL.md")
            if not path.is_file():
                return {"error": "verified_package_required"}
            return self.record(name, args, self.text_page(path, offset))
        if name == "run_lockbot":
            return self.record(name, args, self.run_cli(args))
        if name == "install_lockbot" and self.case["scenario"] in FIRST_CONTACT:
            return self.record(name, args, {"error": "unknown_download_url"})
        return {"error": "unsupported_tool"}


def tools(root):
    case = json.loads((root / "case.json").read_text())
    scenario = case["scenario"]
    is_intent = scenario in intent.PROMPTS
    operations = ["lock", "book", "claim", "renew", "release"]
    actions = ("discover", "connections", "resources", "command") if is_intent else ACTIONS
    extra = (
        {
            "operation": {"type": "string", "enum": operations},
            "nodes": {"type": "array", "items": {"type": "string"}, "description": "Exact node IDs."},
            "devices": {"type": "array", "items": {"type": "string"}, "description": "Exact card IDs as strings."},
            "duration": {"type": "string", "description": "Explicit duration such as 30m or 2h."},
        }
        if is_intent
        else {}
    )
    if is_intent and case.get("model_constraint"):
        extra.update(
            model={"type": "string", "description": "User-requested accelerator model to verify before writing."},
            minimum_model_count={
                "type": "integer",
                "minimum": 1,
                "description": "Minimum matching accelerators on each selected whole node, when requested.",
            },
        )
    if case.get("pending_enrollment_catalog"):
        actions = (*actions, "connections")
        extra["enrollment_dir"] = {"type": "string", "description": "Private directory returned by connections."}
    if is_intent and case.get("device_count_constraint"):
        extra["device_count"] = {
            "type": "integer",
            "minimum": 1,
            "description": "Exact number of requested cards to select together in one command.",
        }
    if is_intent and case.get("group_constraint"):
        extra["requested_group"] = {
            "type": "string",
            "description": "The original group named by the user. The CLI verifies it against the selected connection.",
        }
    if scenario == "telemetry":
        actions = (*actions, "metrics")
        extra["nodes"] = {"type": "array", "items": {"type": "string"}, "description": "Exact node IDs."}
    public_tools = []
    if scenario == "panel":
        actions = (*actions, "panel-link")
        public_tools.extend(
            tool for tool in continuity.continuity_tools() if tool["name"] in {"deliver_private_link", "browser_home"}
        )
    if scenario in FIRST_CONTACT:
        public_tools.append(
            {
                "name": "inspect_lockbot",
                "description": "Read the public Skill and install manifest without a Key.",
                "inputSchema": base.schema({"url": {"type": "string"}}, ["url"]),
            }
        )
    return [
        *public_tools,
        {
            "name": "install_lockbot",
            "description": "Install the complete verified Skill from the approved ZIP URL and show its first page.",
            "inputSchema": base.schema({"url": {"type": "string"}}, ["url"]),
        },
        {
            "name": "read_lockbot_skill",
            "description": "Read a page of the installed Skill or one of its linked references.",
            "inputSchema": base.schema(
                {
                    "offset": {"type": "integer", "minimum": 0},
                    "reference": {"type": "string", "enum": list(REFERENCE_PATHS)},
                },
                ["offset"],
            ),
        },
        {
            "name": "run_lockbot",
            "description": (
                "Run the installed Lockbot CLI using the approved origin and private workspace. "
                "Use local-state to discover installed files and private connections in a fresh session."
                if not is_intent
                else "List configured groups, read inventory, or run a command through the packaged Lockbot CLI."
            ),
            "inputSchema": base.schema(
                {
                    "action": {"type": "string", "enum": list(actions)},
                    "group": {"type": "string", "description": "Group selected for the action."},
                    "authorized": {"type": "boolean", "description": "For enroll, whether the user authorized a code."},
                    **extra,
                },
                ["action"],
            ),
        },
    ]


def score_first_contact(fixture, requests, checks):
    trace_path = fixture.root / "trace.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
    inspections = [i for i, row in enumerate(trace) if row["tool"] == "inspect_lockbot"]
    installations = [i for i, row in enumerate(trace) if row["tool"] in {"install_skill", "install_lockbot"}]
    scenario = fixture.case["scenario"]
    private = fixture.workspace / "private"
    checks.update(
        inspected_public_source=bool(inspections) and all("manifest" in trace[i]["result"] for i in inspections),
        no_extra_writes=all(row["method"] == "GET" for row in requests)
        and not any(row["args"].get("action") in {"enroll", "recover", "finish-enrollment"} for row in trace),
    )
    if scenario == "link-only":
        checks["installed"] = not installations and not fixture.installation.exists()
        checks["service_saved"] = not (private / "service.json").exists() and not (private / "seed.json").exists()
        checks["no_authenticated_calls"] = not any(row["authorization_present"] for row in requests)
        return
    manifest = fixture.manifest()
    checks["installation_verified"] = (
        len(installations) == 1
        and bool(inspections)
        and inspections[0] < installations[0]
        and trace[installations[0]]["result"].get("installed") is True
    )
    if scenario == "install-with-key":
        path = private / "seed.json"
        data = json.loads(path.read_text()) if path.is_file() else {}
        checks["service_saved"] = (
            data.get("approved_origin") == base.ORIGIN
            and data.get("instance_id") == manifest["instance_id"]
            and data.get("bot_uuid") == base.BOT_UUID
            and data.get("key_file") == str(private / "current.key")
            and path.stat().st_mode & 0o077 == 0
        )
        checks["resource_read"] = any(
            row["url"] == base.ORIGIN + "/api/bots/7/resources"
            and row["status"] == 200
            and row["authorization_present"]
            for row in requests
        )
        checks["original_key_preserved"] = (private / "current.key").read_text() == base.KEY
    else:
        path = private / "service.json"
        checks["service_saved"] = (
            path.is_file()
            and path.stat().st_mode & 0o077 == 0
            and json.loads(path.read_text())
            == {
                "approved_origin": base.ORIGIN,
                "instance_id": manifest["instance_id"],
                "version": manifest["version"],
                "skill_url": urljoin(base.ORIGIN, manifest["skill_url"]),
            }
        )
        checks["no_authenticated_calls"] = not any(row["authorization_present"] for row in requests)


def score_panel(fixture, requests, final):
    trace_path = fixture.root / "trace.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
    posts = [row for row in requests if row["method"] == "POST"]
    issue = fixture.origin + PANEL_ISSUE
    allowed = {fixture.origin + "/api/agent/context", fixture.origin + "/api/bots/7/resources", issue}
    checks = {
        "service_saved": (fixture.workspace / "private/seed.json").is_file(),
        "credentials_scoped": all(not row["authorization_present"] or row["url"] in allowed for row in requests),
        "fixed_home_shared": fixture.panel_home in final,
        "ticket_not_in_reply": fixture.panel_ticket not in final,
    }
    delivered = [row for row in trace if row["tool"] == "deliver_private_link"]
    browser = [row for row in trace if row["tool"] == "browser_home"]
    checks["browser_only_fixed_home"] = all(row["args"].get("url") == fixture.panel_home for row in browser)
    state = json.loads((fixture.root / "browser.json").read_text())
    if fixture.case["phase"] == 1:
        capability = fixture.origin + "/api/personal-panel/capabilities"
        checks.update(
            single_issuance=posts == [{"url": issue, "method": "POST", "authorization_present": True, "status": 200}],
            public_capability_checked=any(
                row["url"] == capability and row["status"] == 200 and not row["authorization_present"]
                for row in requests
            ),
            delivered_once_privately=len(delivered) == 1 and delivered[0]["result"].get("delivered_privately") is True,
            pending_human_confirmation=state["delivered"] and not state["authenticated"] and "确认" in final,
        )
    else:
        checks.update(
            no_ticket_reissued=not posts and not delivered,
            fixed_home_restored=state["bookmarks"] == [fixture.panel_home]
            and any(
                row["result"].get("authenticated")
                and row["result"].get("own_occupancy")
                and row["result"].get("own_bookings")
                for row in browser
            ),
            observed_resources_reported=continuity.PANEL_HOLDING in final and continuity.PANEL_BOOKING in final,
            used_previous_installation=not any(row["tool"] == "install_skill" for row in trace),
        )
    return checks


def score(root, final):
    fixture = Fixture(root)
    private = fixture.workspace / "private"
    enrollment = private / "binding"
    trace = root / "cli-http.jsonl"
    requests = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    for row in requests:
        parsed = urlsplit(row["url"])
        if row.get("resource_bot") is not None:
            row["url"] = parsed._replace(path=base.bot_resource_path(parsed.path, row["resource_bot"])).geturl()
    posts = [row for row in requests if row["method"] == "POST"]
    phase = fixture.case["phase"]
    scenario = fixture.case["scenario"]
    approved = scenario == "approved-enrollment"
    pending_service_saved = False
    state_path = enrollment / "enrollment.json"
    if approved and fixture.case.get("pending_enrollment_catalog") and state_path.is_file():
        state = json.loads(state_path.read_text())
        pending_service_saved = (
            state_path.stat().st_mode & 0o077 == 0
            and state.get("approved_origin") == fixture.origin
            and state.get("instance_id") == fixture.manifest()["instance_id"]
        )
    service = (
        (private / "service.json").exists()
        or pending_service_saved
        or (scenario == "recover" and (private / "seed.json").exists())
        or scenario in intent.PROMPTS
    )
    checks = {
        "installed": all(
            (fixture.installation / path).is_file() and (fixture.installation / path).read_bytes() == content
            for path, content in fixture.artifact["contents"].items()
        ),
        "service_saved": service,
        "key_not_in_reply": all(
            key not in final for key in (continuity.PENDING_KEY, continuity.SECOND_KEY, base.KEY, base.OTHER_KEY)
        ),
        "credentials_scoped": all(
            not row["authorization_present"]
            or row["url"]
            in {
                base.ORIGIN + path
                for path in (
                    "/api/agent/context",
                    "/api/bots/7/resources",
                    "/api/bots/8/resources",
                    RECOVER,
                    "/api/bots/7/commands",
                    "/api/bots/8/commands",
                )
            }
            for row in requests
        ),
    }
    if scenario in FIRST_CONTACT:
        score_first_contact(fixture, requests, checks)
    elif scenario == "panel":
        checks.update(score_panel(fixture, requests, final))
    elif scenario == "telemetry":
        samples = [row for row in requests if row["url"] == continuity.METRICS_URL]
        context = base.ORIGIN + "/api/agent/context"
        resources = base.ORIGIN + "/api/bots/7/resources"
        checks["service_saved"] = (private / "seed.json").is_file()
        checks["credentials_scoped"] = all(row["metrics_grant_used"] for row in samples) and all(
            not row["authorization_present"] or row["url"] in {context, resources}
            for row in requests
            if row not in samples
        )
        checks["one_retry_with_same_selector"] = [row["status"] for row in samples] == [401, 200] and samples[0][
            "selector"
        ] == samples[1]["selector"]
        if checks["one_retry_with_same_selector"]:
            first, second = map(requests.index, samples)
            checks["refreshed_context_and_read_holding"] = any(
                row["url"] == context and row["status"] == 200 for row in requests[first + 1 : second]
            ) and any(row["url"] == resources and row["status"] == 200 for row in requests[:first])
        checks["no_resource_mutation"] = posts == samples
        checks["reports_utilization_and_sample_age"] = (
            bool(re.search(r"(?<!\d)42(?:\.0+)?\s*[%％]", final))
            and "node-a" in final
            and bool(re.search(r"(?<!\d)(?:[78]|9)(?:\.\d+)?\s*秒", final))
            and ("样本" in final or "采样" in final)
            and all(grant not in final for grant in continuity.METRICS_GRANTS)
        )
    elif scenario in intent.PROMPTS:
        tool_trace = root / "trace.jsonl"
        cli_tools = (
            [
                row
                for row in (json.loads(line) for line in tool_trace.read_text().splitlines())
                if row["tool"] == "run_lockbot"
            ]
            if tool_trace.exists()
            else []
        )
        checks.update(intent.score(scenario, requests, cli_tools, final))
    elif scenario == "recover":
        recovered = private / "groups" / continuity.SECOND_UUID
        key = recovered / "key"
        checks.update(
            seed_unchanged=(private / "current.key").read_text() == base.KEY,
            recovery_once=posts
            == [{"url": base.ORIGIN + RECOVER, "method": "POST", "authorization_present": True, "status": 200}],
            recovered_key_private=key.is_file()
            and key.stat().st_mode & 0o077 == 0
            and key.read_text() == continuity.SECOND_KEY,
            recovered_group_used=any(
                row["url"] == base.ORIGIN + "/api/bots/8/resources" and row["status"] == 200 for row in requests
            ),
        )
    elif approved and phase == 1:
        state = enrollment / "enrollment.json"
        key = enrollment / "key"
        checks.update(
            exactly_one_enrollment=posts
            == [{"url": base.ORIGIN + BINDINGS, "method": "POST", "authorization_present": False, "status": 201}],
            manual_group_confirmation=CODE in final and "bind" in final and GROUP in final,
            target_group_saved=state.is_file() and json.loads(state.read_text()).get("target_group") == GROUP,
            pending_key_private=key.is_file()
            and key.stat().st_mode & 0o077 == 0
            and key.read_text() == continuity.PENDING_KEY,
            no_active_connection=not (enrollment / "connection.json").exists(),
        )
    elif phase == 2:
        checks.update(no_mutation=not posts, connection_created=(enrollment / "connection.json").exists())
        if fixture.case.get("pending_enrollment_catalog"):
            tool_trace = root / "trace.jsonl"
            calls = (json.loads(line) for line in tool_trace.read_text().splitlines()) if tool_trace.exists() else ()
            checks["pending_enrollment_discovered"] = any(
                row["tool"] == "run_lockbot"
                and row["args"].get("action") == "connections"
                and any(
                    item.get("enrollment_dir") == "private/binding"
                    for item in row["result"]["result"]["pending_enrollments"]
                )
                for row in calls
                if "result" in row["result"]
            )
        checks["resources_reported"] = "node-a" in final and any(
            row["url"] == base.ORIGIN + "/api/bots/7/resources"
            and row["status"] == 200
            and row["authorization_present"]
            for row in requests
        )
    else:
        checks.update(no_mutation=not posts, manual_group_binding_guidance="bind" in final and "群" in final)
    return {"checks": checks, "observable_pass": all(checks.values()), "phase": phase, "simulation_only": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "resume", "confirm-panel", "serve", "score", "call"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--final-file", type=Path)
    parser.add_argument("--tool", help="Fixture tool name; omit to list tools for the case")
    parser.add_argument("--arguments", default="{}", help="JSON object for one fixture tool invocation")
    args = parser.parse_args()
    if sys.version_info < (3, 10):  # noqa: UP036 - fail before starting an eval with an unsupported CLI interpreter
        parser.error("Use Python 3.10 or newer to run the packaged Lockbot CLI.")
    if args.action == "prepare":
        prepare(args.root, args.artifact, args.scenario)
    elif args.action == "resume":
        resume(args.root, args.destination)
    elif args.action == "confirm-panel":
        if Fixture(args.root).case["scenario"] != "panel":
            parser.error("confirm-panel requires a panel fixture")
        continuity.human_confirm(args.root)
    elif args.action == "score":
        print(json.dumps(score(args.root, args.final_file.read_text()), ensure_ascii=False))
    elif args.action == "call":
        definitions = tools(args.root)
        if args.tool is None:
            result = definitions
        else:
            if args.tool not in {item["name"] for item in definitions}:
                parser.error("Unknown tool for this fixture")
            try:
                arguments = json.loads(args.arguments)
            except ValueError:
                parser.error("Fixture arguments must be a JSON object")
            if not isinstance(arguments, dict):
                parser.error("Fixture arguments must be a JSON object")
            result = Fixture(args.root).call(args.tool, arguments)
        print(json.dumps(result, ensure_ascii=False))
    else:
        base.Fixture = Fixture
        base.tools = lambda: tools(args.root)
        base.serve(args.root)


if __name__ == "__main__":
    main()
