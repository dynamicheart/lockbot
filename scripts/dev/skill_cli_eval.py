#!/usr/bin/env python3
"""Model evaluation against the actual packaged CLI and an offline Lockbot fixture."""

import argparse
import contextlib
import io
import json
import os
import re
import runpy
import shutil
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit

import skill_continuity_eval as continuity
import skill_intent_eval as intent
from lockbot.backend.app.agent_skill.scripts.lockbot_cli import REFERENCE_PATHS, Client, ClientError
from skill_eval_artifact import installed_service_origin, installed_skill_catalog

base = continuity.base
OriginalFixture = base.Fixture
BINDINGS = "/api/resource-auth/bindings"
CHALLENGES = "/api/resource-auth/credential-challenges"
BINDING_ID = "f" * 22
CODE = "ACDEFGHJ"
CHALLENGE_PROOF = "lbp_" + "l" * 36
GROUP = "验证群"
COLD_REQUEST = "cold-install-allocate"
COLD_INVENTORY = "intent-model-p800-whole-machine"
COLD_COMMAND = intent.EXPECTED[COLD_INVENTORY][1]
PANEL_ORIGIN = "https://lockbot.example.test"
PANEL_FIXTURE_VERSION = 9
SKILL_CATALOG_FIXTURE_VERSION = 17
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
    "link-only": "{url}",
    "install-from-link": "安装Skill: {url}",
    "install-from-zip": "安装Skill: {zip_url}",
    "install-with-key": "装一下，用原来的登录查下资源。\n{zip_url}",
}
SAVED_LOGIN_SCENARIOS = {"install-with-key", "panel", "telemetry"}
CHALLENGE_PROMPTS = {
    COLD_REQUEST: "帮我安装这个 Skill，申请一台 P800 机器用两小时。",
    "approved-enrollment-no-group": "帮忙查找可用的资源。",
    "recover-original-key": "我换环境了，帮我恢复下。",
    "reset-personal-key": "我的key泄漏了，帮我重置下key。",
}
ENROLLMENT_SCENARIOS = ("approved-enrollment", *CHALLENGE_PROMPTS)
SCENARIOS = ("no-key", *ENROLLMENT_SCENARIOS, "recover", "panel", "telemetry", *FIRST_CONTACT, *intent.PROMPTS)


def prepare(root, artifact, scenario):
    seed_scenario = scenario
    if scenario in intent.PROMPTS or scenario == "install-with-key":
        seed_scenario = "recover"
    elif scenario in FIRST_CONTACT:
        seed_scenario = "no-key"
    elif scenario in ENROLLMENT_SCENARIOS:
        seed_scenario = "approved-enrollment"
    continuity.prepare(root, artifact / "SKILL.md", seed_scenario, artifact)
    case_path = root / "case.json"
    case = json.loads(case_path.read_text())
    helper = Fixture(root).artifact["contents"]["scripts/lockbot_cli.py"]
    base.save_json(
        case_path,
        {
            **case,
            "coherent_snapshot": b"coherent_resource_snapshot" in helper,
            "fixture_version": 16
            if scenario in intent.PROMPTS or scenario == COLD_REQUEST
            else 15
            if scenario in intent.CONVERSATIONAL_PROMPTS or scenario in {"intent-my-locks", "intent-my-books"}
            else 14
            if scenario in intent.PUBLIC_IP_ALIAS_PROMPTS
            else 13
            if scenario in {"reset-personal-key", "intent-my-locks", "intent-my-books"}
            or scenario in intent.INSTALL_FOLLOWUPS
            or scenario in intent.IP_TAIL_PROMPTS
            or scenario == "intent-shared-minimal"
            else case["fixture_version"],
        },
    )
    if scenario == "no-key":
        (root / "prompt.txt").write_text(f"安装Skill: {base.ORIGIN}/api/agent-skill")
    if scenario == "approved-enrollment":
        (root / "prompt.txt").write_text("帮忙查找可用的资源。")
    if scenario in ENROLLMENT_SCENARIOS:
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        helper = Fixture(root).artifact["contents"]["scripts/lockbot_cli.py"]
        base.save_json(
            case_path, {**case, "scenario": scenario, "pending_enrollment_catalog": b'"pending_enrollments"' in helper}
        )
        if scenario in CHALLENGE_PROMPTS:
            prompt = CHALLENGE_PROMPTS[scenario]
            if scenario == COLD_REQUEST:
                prompt += "\n" + base.ORIGIN + base.BUNDLE_PATH
            (root / "prompt.txt").write_text(prompt)
    if scenario == "panel":
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        base.save_json(
            case_path,
            {
                **case,
                "origin": PANEL_ORIGIN,
                "panel_ticket": "synthetic-panel-ticket-evaluation-only",
                "fixture_version": PANEL_FIXTURE_VERSION,
            },
        )
        (root / "prompt.txt").write_text("打开个人资源台")
    if scenario == "telemetry":
        (root / "prompt.txt").write_text("node-a谁在用，利用率多少？")
    if scenario in FIRST_CONTACT:
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        base.save_json(case_path, {**case, "scenario": scenario})
        path = "/api/agent-skill" if scenario in {"link-only", "install-from-link"} else base.BUNDLE_PATH
        (root / "prompt.txt").write_text(
            FIRST_CONTACT[scenario].format(url=base.ORIGIN + path, zip_url=base.ORIGIN + base.BUNDLE_PATH)
        )
    if scenario in intent.PROMPTS:
        case_path = root / "case.json"
        case = json.loads(case_path.read_text())
        helper = Fixture(root).artifact["contents"]["scripts/lockbot_cli.py"]
        base.save_json(
            case_path,
            {
                **case,
                "scenario": scenario,
                "fixture_version": max(11, case["fixture_version"])
                if scenario in intent.TASK_SCENARIOS
                else case["fixture_version"],
                "model_constraint": b'"--minimum-model-count"' in helper,
                "device_count_constraint": b'"--device-count"' in helper,
                "group_constraint": b"selected_connection_does_not_match_requested_group" in helper,
            },
        )
        prompt = intent.PROMPTS[scenario].format(url=base.ORIGIN + "/api/agent-skill")
        (root / "prompt.txt").write_text(prompt)
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
        (root / "prompt.txt").write_text("看看我能申请什么资源？")
    if scenario in SAVED_LOGIN_SCENARIOS:
        fixture = Fixture(root)
        connection = fixture.saved_connection
        connection.parent.mkdir(mode=0o700)
        (fixture.workspace / "private/current.key").rename(connection.parent / "key")
        context = fixture.http(
            {
                "url": fixture.origin + "/api/agent/context",
                "method": "GET",
                "headers": {"Authorization": "Bearer " + base.KEY},
            }
        )["body"]
        record = {
            "approved_origin": fixture.origin,
            "instance_id": fixture.manifest()["instance_id"],
            "bot_uuid": context["bot"]["uuid"],
            "actor": context["actor"],
            "key_file": "key",
        }
        if scenario == "telemetry":
            record["approved_metrics_origin"] = continuity.METRICS_ORIGIN
        base.save_json(connection, record)
    system = (
        "Use only the supplied fixture tools. The workspace and service are synthetic. Report verified actions only. "
        "The client supplies its sign-in state privately. Keep credential and connection paths out of user replies. "
    )
    if scenario == "approved-enrollment":
        system += f" The client resource picker has selected target group {GROUP!r}. "
    if scenario != COLD_REQUEST:
        system += (
            "install_lockbot installs and shows the verified Skill. run_lockbot runs its actual packaged CLI. "
            "read_lockbot_skill can read later pages and linked references in the installed Skill. "
            "The fixture handles the workspace paths. "
            "Only a human can confirm a new enrollment in the resource group's chat. "
            "An existing verified connection uses the API for authorized resource operations."
        )
    if (
        scenario in intent.PROMPTS
        and scenario not in intent.INSTALL_FOLLOWUPS
        or scenario in SAVED_LOGIN_SCENARIOS - set(FIRST_CONTACT)
        or scenario
        in {
            "recover",
            "approved-enrollment",
            "approved-enrollment-no-group",
            "recover-original-key",
            "reset-personal-key",
        }
    ):
        fixture = Fixture(root)
        Client(approved_origin=fixture.origin, opener=fixture).install(fixture.installation)
        (root / "cli-http.jsonl").unlink(missing_ok=True)
        system += " The verified Skill is already installed. read_lockbot_skill reads it."
        system += installed_skill_catalog(fixture.installation, fixture.artifact)
        case = json.loads(case_path.read_text())
        base.save_json(
            case_path, {**case, "fixture_version": max(SKILL_CATALOG_FIXTURE_VERSION, case["fixture_version"])}
        )
    (root / "system.txt").write_text(system)
    configure(root)


def configure(root):
    path = root / "mcp.json"
    config = json.loads(path.read_text())
    config["mcpServers"]["fixture"]["args"] = [str(Path(__file__).resolve()), "serve", str(root.resolve())]
    base.save_json(path, config)


def continue_conversation(source, destination, prompt):
    previous = json.loads((source / "response.json").read_text())
    if not isinstance(previous.get("text"), str):
        raise ValueError("The prior assistant response is unavailable")
    continuity.resume(source, destination)
    case_path = destination / "case.json"
    case = json.loads(case_path.read_text())
    base.save_json(case_path, {**case, "parent_fixture_directory": source.name})
    for state in source.glob("simulated-bot-*.json"):
        shutil.copy2(state, destination / state.name)
    history = json.loads((source / "conversation.json").read_text()) if (source / "conversation.json").is_file() else []
    base.save_json(
        destination / "conversation.json",
        [
            *history,
            {"role": "user", "content": (source / "prompt.txt").read_text()},
            {"role": "assistant", "content": previous["text"]},
        ],
    )
    (destination / "prompt.txt").write_text(prompt)
    (destination / "system.txt").write_text((source / "system.txt").read_text())
    configure(destination)


def reset_impact_checks(final):
    impact = bool(
        re.search(
            r"(?:其他|其它|别的|所有|共用|共享).{0,25}(?:Agent|代理|客户端).{0,35}"
            r"(?:失效|重新(?:登录|连接|配置)|更新|更换|换成)"
            r"|other.{0,20}agents?.{0,50}(?:invalid|reconnect|reconfigur|update|replace)",
            final,
            re.I,
        )
    )
    invalidation = bool(re.search(r"失效|作废|invalid|revok", final, re.I))
    negated = bool(re.search(r"(?:不会|无需|不用).{0,12}(?:失效|重新连接|重新配置)", final))
    confirmation = bool(re.search(r"确认|确定|是否|可以.{0,6}[？?]|confirm|proceed", final, re.I))
    return {"reset_impact_explained": impact and invalidation and not negated, "asks_reset_confirmation": confirmation}


def resume(source, destination):
    fixture = Fixture(source)
    reset = fixture.case["scenario"] == "reset-personal-key"
    if reset and fixture.case["phase"] == 1:
        final = json.loads((source / "response.json").read_text())["text"]
        if not all(reset_impact_checks(final).values()) or fixture.binding_status() != "expired":
            raise ValueError("Explain reset impact before simulating user acknowledgement")
        continue_conversation(source, destination, "行，重置吧")
        case = json.loads((destination / "case.json").read_text())
        base.save_json(destination / "case.json", {**case, "phase": 2, "reset_impact_acknowledged": True})
        return
    if fixture.case["scenario"] in intent.CONVERSATIONAL_PROMPTS:
        if fixture.case["phase"] != 1:
            raise ValueError("This conversation has exactly two turns")
        continue_conversation(source, destination, intent.CONVERSATIONAL_PROMPTS[fixture.case["scenario"]][1])
        return
    if fixture.case["scenario"] == "panel":
        if not json.loads((source / "browser.json").read_text())["authenticated"]:
            raise ValueError("The simulated user must confirm browser consent before resuming")
        continuity.resume(source, destination)
        (destination / "system.txt").write_text((source / "system.txt").read_text())
        (destination / "prompt.txt").write_text("打开资源台，看看我占了啥、约了啥。")
        configure(destination)
        return
    if fixture.uses_challenge and fixture.case["phase"] == (3 if reset else 2):
        connection = fixture.workspace / "private/binding/connection.json"
        if not connection.is_file():
            raise ValueError("A completed enrollment must have saved a connection before a fresh read")
        continuity.resume(source, destination)
        case_path = destination / "case.json"
        base.save_json(case_path, {**json.loads(case_path.read_text()), "phase": 4 if reset else 3})
        (destination / "system.txt").write_text((source / "system.txt").read_text())
        (destination / "prompt.txt").write_text("这个项目里有啥可用的 LockBot 资源？")
        base.save_json(destination / "binding.json", {"status": "confirmed", "simulation_only": True})
        if fixture.case["scenario"] == COLD_REQUEST:
            base.save_json(
                destination / "cold-allocation.json", json.loads((source / "cold-allocation.json").read_text())
            )
        configure(destination)
        return
    if fixture.binding_status() != "pending":
        raise ValueError("An approved enrollment must have issued a code before simulated human confirmation")
    continuity.resume(source, destination)
    if reset:
        case = json.loads((destination / "case.json").read_text())
        base.save_json(destination / "case.json", {**case, "phase": 3})
    (destination / "system.txt").write_text((source / "system.txt").read_text())
    if fixture.case["scenario"] == COLD_REQUEST:
        prompt = "群里确认了，继续申请台 P800，用两小时。"
    else:
        prompt = "帮我看看有哪些资源。"
    (destination / "prompt.txt").write_text(prompt)
    base.save_json(destination / "binding.json", {"status": "confirmed", "simulation_only": True})
    configure(destination)


class Fixture(continuity.Fixture):
    @property
    def uses_challenge(self):
        return self.case["scenario"] in CHALLENGE_PROMPTS

    @property
    def challenge_purpose(self):
        return "reset" if self.case["scenario"] == "reset-personal-key" else "recover"

    @property
    def challenge_code(self):
        return ("RST-" if self.challenge_purpose == "reset" else "REC-") + CODE

    @property
    def enrolled_key(self):
        return base.KEY if self.case["scenario"] == "recover-original-key" else continuity.PENDING_KEY

    def manifest(self):
        result = super().manifest()
        if self.uses_challenge:
            result["capabilities"] = list(dict.fromkeys([*result["capabilities"], "chat_credential_challenges"]))
        if self.case.get("coherent_snapshot"):
            result["capabilities"] = list(dict.fromkeys([*result["capabilities"], "coherent_resource_snapshot"]))
        return result

    @property
    def installation(self):
        return self.workspace / "skills/lockbot-agent"

    @property
    def saved_connection(self):
        return self.workspace / "private/connections/connection.json"

    def binding_status(self):
        path = self.root / "binding.json"
        return json.loads(path.read_text())["status"] if path.exists() else "expired"

    def coherent_snapshot(self, args, body):
        context = self.legacy_http({**args, "url": self.origin + "/api/agent/context", "method": "GET"})
        if context["status"] != 200:
            raise ValueError("Cannot attach unauthorized fixture context")
        context = {
            name: value
            for name, value in context["body"].items()
            if name
            in {
                "server",
                "actor",
                "bot",
                "bot_rules",
                "agent_access_enabled",
                "credential_scope",
                "usage_privacy",
                "links",
                "skill",
            }
        }
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "age_seconds": 0,
            "bot_id": context["bot"]["id"],
            "bot_type": context["bot"]["type"],
            "actor": context["actor"],
            **body,
            "context": context,
        }

    def http(self, args):
        result = self.legacy_http(args)
        if not self.case.get("coherent_snapshot") or result["status"] != 200:
            return result
        path = urlsplit(args["url"]).path
        if args["method"] == "GET" and path.endswith("/resources"):
            result["body"] = self.coherent_snapshot(args, result["body"])
        elif args["method"] == "POST" and path.endswith("/commands"):
            query = {**args, "url": args["url"].removesuffix("/commands") + "/resources", "method": "GET"}
            after = self.legacy_http(query)
            if after["status"] != 200:
                raise ValueError("Cannot observe fixture mutation")
            result["body"]["resource_snapshot"] = self.coherent_snapshot(query, after["body"])
        return result

    def legacy_http(self, args):
        path = urlsplit(args["url"]).path
        method = args["method"]
        bearer = args["headers"].get("Authorization", "")
        if self.case["scenario"] == "telemetry" and args["url"] == continuity.METRICS_URL:
            return super().http(args)
        if args["url"] != self.origin + path:
            return {"status": 404}
        if path == base.BUNDLE_PATH and method == "GET" and not bearer:
            return {"status": 200}
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
        if self.uses_challenge:
            if path == CHALLENGES and method == "POST" and not bearer:
                if (
                    self.case["phase"] != (2 if self.challenge_purpose == "reset" else 1)
                    or (self.challenge_purpose == "reset" and not self.case.get("reset_impact_acknowledged"))
                    or self.binding_status() != "expired"
                    or args.get("purpose") != self.challenge_purpose
                ):
                    return {"status": 409}
                base.save_json(self.root / "binding.json", {"status": "pending"})
                return {
                    "status": 201,
                    "body": {
                        "challenge_id": BINDING_ID,
                        "proof": CHALLENGE_PROOF,
                        "bind_code": self.challenge_code,
                        "bind_expires_at": "2030-01-01T00:10:00Z",
                    },
                }
            if path == f"{CHALLENGES}/{BINDING_ID}" and method == "GET":
                if bearer != f"Bearer {CHALLENGE_PROOF}":
                    return {"status": 404}
                result = {"status": self.binding_status()}
                if result["status"] == "confirmed":
                    result["credential"] = self.enrolled_key
                    if self.case["scenario"] in {COLD_REQUEST, "approved-enrollment-no-group"}:
                        result["outcome"] = "created"
                    elif self.case["scenario"] == "recover-original-key":
                        result["outcome"] = "recovered"
                return {"status": 200, "body": result}
        status = self.binding_status()
        old_key_active = self.case["scenario"] in {"recover-original-key", "reset-personal-key"} and (
            status != "confirmed" or self.challenge_purpose == "recover"
        )
        authorized = (status == "confirmed" and bearer == "Bearer " + self.enrolled_key) or (
            old_key_active and bearer == "Bearer " + base.KEY
        )
        if self.case["scenario"] == COLD_REQUEST and authorized:
            state = self.root / "cold-allocation.json"
            resource = base.bot_resource_path(path, 7)
            if resource == "/api/bots/7/commands" and method == "POST":
                if (
                    self.case["phase"] == 2
                    and not state.exists()
                    and intent.equivalent(args.get("command"), COLD_COMMAND)
                ):
                    base.save_json(state, COLD_COMMAND)
                    return {"status": 200, "body": {"reply": "ok"}}
                return {"status": 409}
            if resource == "/api/bots/7/resources" and method == "GET":
                command = json.loads(state.read_text()) if state.exists() else ""
                snapshot = intent.inventory(
                    8, command, scenario=COLD_INVENTORY, fixture_version=self.case["fixture_version"]
                )
                snapshot["bot_id"] = 7
                snapshot["resources"] = [row for row in snapshot["resources"] if row["id"] != "node-21"]
                machine = next(row for row in snapshot["resources"] if row["id"] == intent.MATCHING_MACHINE)
                machine["accelerators"] = [{"model": "P800", "count": 8}]
                return {"status": 200, "body": snapshot}
        if authorized and method == "GET":
            if path == "/api/agent/context":
                response = OriginalFixture.http(self, {**args, "headers": {"Authorization": "Bearer " + base.KEY}})
                response["body"]["bot"]["name"] = GROUP
                if self.case["scenario"] == COLD_REQUEST:
                    response["body"]["bot"]["type"] = "QUEUE"
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
            if scenario in intent.SHORT_PROMPTS or scenario in intent.PUBLIC_IP_ALIAS_PROMPTS:
                body["bot_rules"] = {
                    **(body.get("bot_rules") or {}),
                    "default_duration_seconds": intent.SHORT_DEFAULT_DURATION if bot == 8 else 3600,
                }
            body["skill"] = self.manifest()
            return response
        state = self.root / f"simulated-bot-{bot}.json"
        if path == f"/api/bots/{bot}/resources" and method == "GET":
            command = json.loads(state.read_text()) if state.exists() else ""
            writes = command.get("writes", 0) if isinstance(command, dict) else 0
            snapshot = intent.inventory(
                bot,
                command,
                scenario=scenario,
                phase=self.case["phase"],
                writes=writes,
                fixture_version=self.case["fixture_version"],
            )
            return {"status": 200, "body": snapshot}
        if path == f"/api/bots/{bot}/commands" and method == "POST":
            if scenario in intent.TASK_SCENARIOS:
                expected = intent.task_expectation(scenario, self.case["phase"])
                if (
                    expected
                    and bot == expected[0]
                    and intent.equivalent(
                        args.get("command"),
                        expected[1],
                        default_duration=intent.SHORT_DEFAULT_DURATION
                        if scenario in intent.PUBLIC_IP_ALIAS_PROMPTS
                        else None,
                    )
                ):
                    previous = json.loads(state.read_text()) if state.is_file() else {}
                    writes = previous.get("writes", 0) if isinstance(previous, dict) else 0
                    base.save_json(state, {"command": expected[1], "writes": writes + 1})
                    return {"status": 200, "body": {"reply": "ok"}}
                return {"status": 403}
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
            default_duration = intent.SHORT_DEFAULT_DURATION if scenario in intent.SHORT_DEFAULT_SCENARIOS else None
            match = next(
                (
                    expected
                    for case, (identifier, expected) in intent.EXPECTED.items()
                    if (case not in intent.SHORT_PROMPTS or case == scenario)
                    and (scenario not in intent.SHORT_PROMPTS or case == scenario)
                    and (case != "intent-release" or case == scenario)
                    and identifier == bot
                    and intent.equivalent(args.get("command"), expected, default_duration=default_duration)
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
        if request.full_url == self.origin + CHALLENGES and request.method == "POST":
            args["purpose"] = json.loads(request.data or b"{}").get("purpose")
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
                            {"resource_observation": intent.observation_identity(result["body"]["resource_snapshot"])}
                            if isinstance(result.get("body", {}).get("resource_snapshot"), dict)
                            else {}
                        ),
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
        data = (
            self.artifact["archive"]
            if path == base.BUNDLE_PATH and result["status"] == 200
            else json.dumps(result.get("body", {})).encode()
        )
        payload = io.BytesIO(data)
        payload.headers = {}
        if result["status"] >= 400:
            raise urllib.error.HTTPError(request.full_url, result["status"], "fixture refused", {}, payload)
        return payload

    def run_cli(self, args, execution):
        installed = self.artifact is not None and all(
            (self.installation / name).is_file() and (self.installation / name).read_bytes() == content
            for name, content in self.artifact["contents"].items()
        )
        private = self.workspace / "private"
        directory = private / "binding"
        action = args.get("action")
        if (
            action == "key-challenge"
            and self.challenge_purpose == "reset"
            and not self.case.get("reset_impact_acknowledged")
        ):
            return {"error": "reset_impact_acknowledgment_required"}
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
            (action in ("connections", "context", "command") and self.case["scenario"] in intent.PROMPTS)
            or (action == "command" and self.case["scenario"] == COLD_REQUEST)
            or (
                action == "connections"
                and (self.case.get("pending_enrollment_catalog") or self.case["scenario"] in SAVED_LOGIN_SCENARIOS)
            )
            or (action == "key-challenge" and self.uses_challenge)
            or (action == "metrics" and self.case["scenario"] == "telemetry")
            or (action == "panel-link" and self.case["scenario"] == "panel")
        ):
            return {"error": "unsupported_fixture_action"}
        argv = [action]
        if action == "connections" and self.case["scenario"] in intent.PROMPTS:
            return {"result": {"groups": list(intent.group_ids(self.case["scenario"]))}}
        if action in ("context", "resources", "command") and self.case["scenario"] in intent.PROMPTS:
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
                if args.get("whole_node") is True:
                    argv += ["--whole-node"]
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
            if self.case["scenario"] not in SAVED_LOGIN_SCENARIOS:
                argv += ["--directory", str(private)]
        elif action == "discover":
            argv += ["--origin", self.origin, "--destination", str(private / "service.json")]
        elif action in {"enroll", "key-challenge"}:
            argv += ["--origin", self.origin, "--destination", str(directory)]
            if action == "key-challenge":
                argv += ["--purpose", args.get("purpose", "recover")]
            elif (group := args.get("group")) is not None:
                if not isinstance(group, str):
                    return {"error": "invalid_group"}
                argv += ["--group", group]
            if args.get("authorized") is True:
                argv += ["--authorized"]
        elif action == "init":
            if self.case["scenario"] in SAVED_LOGIN_SCENARIOS:
                return {"error": "already_signed_in", "next_step": "Use connections to find the saved sign-in."}
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
            argv = ["--connection", str(self.saved_connection), action]
        elif action == "metrics":
            argv = ["--connection", str(self.saved_connection), action]
            for node in args.get("nodes", []):
                argv += ["--node", node]
        elif action == "command":
            argv = [
                "--connection",
                str(directory / "connection.json"),
                action,
                "--operation",
                args.get("operation", ""),
            ]
            if args.get("whole_node") is True:
                argv += ["--whole-node"]
            for field in ("node", "device"):
                for target in args.get(field + "s", []):
                    argv += ["--" + field, target]
            if "duration" in args:
                argv += ["--duration", args["duration"]]
            if "model" in args:
                argv += ["--model", args["model"]]
        elif action == "resources":
            connection = directory / "connection.json"
            if self.uses_challenge and self.case["phase"] == (4 if self.challenge_purpose == "reset" else 3):
                if args.get("connection") != connection.relative_to(self.workspace).as_posix():
                    return {
                        "error": "private_connection_required",
                        "next_step": "Run connections and pass the returned connection path.",
                    }
            elif self.case["scenario"] in SAVED_LOGIN_SCENARIOS:
                connection = self.saved_connection
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
                and self.case["phase"] == (3 if self.challenge_purpose == "reset" else 2)
                and args.get("enrollment_dir") != directory.relative_to(self.workspace).as_posix()
            ):
                return {
                    "error": "enrollment_dir_required",
                    "next_step": "Run connections to find the pending enrollment.",
                }
            argv += ["--enrollment-dir", str(directory)]
        script_path = self.installation / "scripts/lockbot_cli.py"
        script = runpy.run_path(str(script_path))
        execution["argv"] = [str(script_path), *argv]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("urllib.request.build_opener", return_value=self),
            patch.dict(os.environ, {"LOCKBOT_HOME": str(private)}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = script["main"](argv)
            except SystemExit as error:
                code = error.code
        payload = stdout.getvalue() if code == 0 else stderr.getvalue()
        return json.loads(payload.replace(str(self.workspace) + "/", ""))

    def record(self, name, args, result, **execution):
        event = {"tool": name, "args": args, "result": result, **execution}
        with (self.root / "trace.jsonl").open("a") as trace:
            trace.write(json.dumps(event, ensure_ascii=False) + "\n")
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
        if name == "install_lockbot" and args.get("url") in {
            self.origin + "/api/agent-skill",
            self.origin + base.BUNDLE_PATH,
        }:
            try:
                verified = Client(approved_origin=self.origin, opener=self).install(self.installation)
            except ClientError as exc:
                return self.record(name, args, {"error": exc.code})
            return self.record(
                name,
                args,
                {
                    "installed": True,
                    "path": str(self.installation.relative_to(self.workspace)),
                    "version": verified["version"],
                    **self.text_page(self.installation / "SKILL.md"),
                },
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
            execution = {}
            http_trace = self.root / "cli-http.jsonl"
            before = len(http_trace.read_text().splitlines()) if http_trace.is_file() else 0
            result = self.run_cli(args, execution)
            after = len(http_trace.read_text().splitlines()) if http_trace.is_file() else 0
            execution["http_request_span"] = [before, after]
            return self.record(name, args, result, **execution)
        if name == "install_lockbot" and self.case["scenario"] in FIRST_CONTACT:
            return self.record(name, args, {"error": "unknown_download_url"})
        return {"error": "unsupported_tool"}


def tools(root):
    case = json.loads((root / "case.json").read_text())
    scenario = case["scenario"]
    is_intent = scenario in intent.PROMPTS
    allows_commands = is_intent or scenario == COLD_REQUEST
    operations = ["lock", "book", "claim", "renew", "release"]
    if is_intent:
        operations.append("slock")
    actions = ("discover", "connections", "context", "resources", "command") if is_intent else ACTIONS
    extra = (
        {
            "operation": {"type": "string", "enum": operations},
            "nodes": {"type": "array", "items": {"type": "string"}, "description": "Exact node IDs."},
            "devices": {"type": "array", "items": {"type": "string"}, "description": "Exact card IDs as strings."},
            "whole_node": {
                "type": "boolean",
                "description": "True only if the user requested or approved entire nodes.",
            },
            "duration": {
                "type": "string",
                "description": "User-requested duration, such as 30m or 2h. Omit if unspecified.",
            },
        }
        if allows_commands
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
    if scenario == COLD_REQUEST:
        actions = (*actions, "command")
        extra["model"] = {"type": "string", "description": "Accelerator model to verify before writing."}
    if scenario in SAVED_LOGIN_SCENARIOS:
        actions = (*actions, "connections")
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
    if scenario in CHALLENGE_PROMPTS:
        actions = (*actions, "key-challenge")
        extra["purpose"] = {
            "type": "string",
            "enum": ["recover", "reset"],
            "description": "Recover or create the personal Key, or reset it after explicit authorization.",
        }
        if case["phase"] == (4 if scenario == "reset-personal-key" else 3):
            extra["connection"] = {
                "type": "string",
                "description": "Required for resources. Use the saved connection path returned by connections.",
            }
    if scenario == "telemetry":
        actions = (*actions, "metrics")
        extra["nodes"] = {"type": "array", "items": {"type": "string"}, "description": "Exact node IDs."}
    public_tools = []
    if scenario == "panel":
        actions = (*actions, "panel-link")
        browser_tools = {tool["name"]: tool for tool in continuity.continuity_tools()}
        browser_home = browser_tools["browser_home"]
        browser_home["description"] += " The local-state action of run_lockbot lists saved browser bookmarks."
        public_tools.extend([browser_tools["deliver_private_link"], browser_home])
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
            "description": "Install the verified Skill from its public manifest or ZIP URL, then show its first page.",
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
                "Use local-state for installed files and connections for private connection records."
                if not is_intent
                else "List configured groups, inspect group rules with context, read inventory, or run a command."
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
    anonymous_reads = [
        row["url"]
        for row in requests
        if row["method"] == "GET" and row["status"] == 200 and not row["authorization_present"]
    ]
    manifest_url = fixture.origin + "/api/agent-skill"
    bundle_url = fixture.origin + base.BUNDLE_PATH
    verified_in_installer = (
        bool(installations)
        and trace[installations[0]]["tool"] == "install_lockbot"
        and trace[installations[0]]["result"].get("installed") is True
        and manifest_url in anonymous_reads
        and bundle_url in anonymous_reads
        and anonymous_reads.index(manifest_url) < anonymous_reads.index(bundle_url)
    )
    explicit_inspection = bool(inspections) and all("manifest" in trace[i]["result"] for i in inspections)
    checks.update(
        inspected_public_source=explicit_inspection or verified_in_installer,
        no_extra_writes=all(row["method"] == "GET" for row in requests)
        and not any(row["args"].get("action") in {"enroll", "recover", "finish-enrollment"} for row in trace),
    )
    if scenario == "link-only":
        checks["installed"] = not installations and not fixture.installation.exists()
        checks["no_bundle_download"] = bundle_url not in anonymous_reads
        checks["service_saved"] = not (private / "service.json").exists() and not (private / "seed.json").exists()
        checks["no_authenticated_calls"] = not any(row["authorization_present"] for row in requests)
        return
    manifest = fixture.manifest()
    checks["installation_verified"] = (
        len(installations) == 1
        and (verified_in_installer or (explicit_inspection and inspections[0] < installations[0]))
        and trace[installations[0]]["result"].get("installed") is True
    )
    if scenario == "install-with-key":
        path = fixture.saved_connection
        data = json.loads(path.read_text()) if path.is_file() else {}
        checks["service_saved"] = (
            data.get("approved_origin") == base.ORIGIN
            and data.get("instance_id") == manifest["instance_id"]
            and data.get("bot_uuid") == base.BOT_UUID
            and data.get("key_file") == "key"
            and path.stat().st_mode & 0o077 == 0
        )
        checks["resource_read"] = any(
            row["url"] == base.ORIGIN + "/api/bots/7/resources"
            and row["status"] == 200
            and row["authorization_present"]
            for row in requests
        )
        checks["original_key_preserved"] = (path.parent / "key").read_text() == base.KEY
    elif scenario == "install-from-zip":
        # Installing the verified service ZIP already preserves its service identity.
        checks.pop("service_saved")
        checks["no_authenticated_calls"] = not any(row["authorization_present"] for row in requests)
    else:
        path = private / "service.json"
        checks["service_saved"] = (
            (
                path.stat().st_mode & 0o077 == 0
                and json.loads(path.read_text())
                == {
                    "approved_origin": base.ORIGIN,
                    "instance_id": manifest["instance_id"],
                    "version": manifest["version"],
                    "skill_url": urljoin(base.ORIGIN, manifest["skill_url"]),
                }
            )
            if path.is_file()
            else installed_service_origin(fixture.installation, fixture.artifact, fixture.origin)
        )
        checks["no_authenticated_calls"] = not any(row["authorization_present"] for row in requests)


def asks_for_panel_confirmation(reply):
    return "确认" in reply or bool(
        re.search(
            r"\b(?:please\s+|you (?:must|should|need to)\s+)(?:confirm|approve)\b"
            r"|\b(?:open|follow)\b[^.!?\n]{0,120}\b(?:link|browser)\b"
            r"[^.!?\n]{0,80}\bto (?:confirm|approve)\b",
            reply,
            re.IGNORECASE,
        )
    )


def score_panel(fixture, requests, final):
    trace_path = fixture.root / "trace.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
    posts = [row for row in requests if row["method"] == "POST"]
    issue = fixture.origin + PANEL_ISSUE
    allowed = {fixture.origin + "/api/agent/context", fixture.origin + "/api/bots/7/resources", issue}
    checks = {
        "service_saved": fixture.saved_connection.is_file(),
        "credentials_scoped": all(not row["authorization_present"] or row["url"] in allowed for row in requests),
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
            fixed_home_shared=fixture.panel_home in final,
            pending_human_confirmation=state["delivered"]
            and not state["authenticated"]
            and asks_for_panel_confirmation(final),
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
            used_previous_installation=not any(row["tool"] in {"install_skill", "install_lockbot"} for row in trace),
        )
    return checks


def prior_observed_groups(root, fixture, final):
    """Reuse discovery from an acknowledged, successful preceding turn only.

    This does not supply current mutation state: the packaged CLI must still
    obtain its own fresh selected-group snapshot before the POST.
    """
    scenario = fixture.case["scenario"]
    if (
        fixture.case["phase"] != 2
        or scenario not in intent.CONVERSATIONAL_PROMPTS
        or scenario in intent.INSTALL_FOLLOWUPS
    ):
        return set()
    try:
        name = fixture.case.get("parent_fixture_directory", "phase-1")
        if not isinstance(name, str) or Path(name).name != name:
            return set()
        source = root.parent / name
        if source.resolve().parent != root.resolve().parent or source.resolve() == root.resolve():
            return set()

        def read(path):
            with path.open("rb") as evidence:
                data = evidence.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Context evidence is too large")
            return data

        prior_case = json.loads(read(source / "case.json"))
        trace = read(source / "trace.jsonl")
        previous = json.loads(read(source / "response.json"))
        conversation = read(root / "conversation.json")
        response = json.loads(read(root / "response.json"))
        if (
            prior_case.get("scenario") != scenario
            or prior_case.get("phase") != 1
            or prior_case.get("skill_sha256") != fixture.case.get("skill_sha256")
            or base.digest(trace) != fixture.case.get("parent_evidence_digest")
            or response.get("text") != final
            or response.get("conversation_sha256") != base.digest(conversation)
            or json.loads(conversation)
            != [
                {"role": "user", "content": read(source / "prompt.txt").decode()},
                {"role": "assistant", "content": previous["text"]},
            ]
        ):
            return set()
        if not score(source, previous["text"])["observable_pass"]:
            return set()
        observed = {}
        for event in (json.loads(line) for line in trace.decode().splitlines()):
            args, result = event.get("args", {}), event.get("result", {}).get("result", {})
            group = args.get("group")
            if event.get("tool") != "run_lockbot" or args.get("action") != "resources" or group not in intent.GROUP_IDS:
                continue
            identifier = intent.GROUP_IDS[group]
            pinned = json.loads(read(fixture.workspace / f"private/group-{identifier}.json"))
            context = result.get("context", {})
            if (
                result.get("bot_id") == identifier
                and isinstance(result.get("resources"), list)
                and result.get("actor") == context.get("actor") == pinned.get("actor")
                and context.get("server", {}).get("instance_id") == pinned.get("instance_id")
                and context.get("bot", {}).get("uuid") == pinned.get("bot_uuid")
            ):
                observed[group] = result
        if scenario == "intent-context-lock-day":
            machines = observed.get(intent.MACHINES, {}).get("resources", [])
            if intent.contextual_p800_candidates(previous["text"], machines) != {intent.MATCHING_MACHINE}:
                return set()
        return set(observed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


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
    enrollment_phase = phase - 1 if fixture.case["scenario"] == "reset-personal-key" else phase
    scenario = fixture.case["scenario"]
    approved = scenario in ENROLLMENT_SCENARIOS
    uses_challenge = fixture.uses_challenge
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
    if scenario in {"no-key", "install-from-link"}:
        service |= installed_service_origin(fixture.installation, fixture.artifact, fixture.origin)
    checks = {
        "installed": all(
            (fixture.installation / path).is_file() and (fixture.installation / path).read_bytes() == content
            for path, content in fixture.artifact["contents"].items()
        ),
        "service_saved": service,
        "credential_paths_not_in_reply": not re.search(
            r"private/(?:current\.key|connections/|seed\.json|binding/|group-\d+\.json)"
            r"|\.lockbot/connections/|\.config/lockbot/connections/",
            final,
        ),
        "key_not_in_reply": all(
            key not in final
            for key in (continuity.PENDING_KEY, continuity.SECOND_KEY, base.KEY, base.OTHER_KEY, CHALLENGE_PROOF)
        ),
        "credentials_scoped": all(
            not row["authorization_present"]
            or (uses_challenge and row["url"] == base.ORIGIN + f"{CHALLENGES}/{BINDING_ID}")
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
        checks["service_saved"] = fixture.saved_connection.is_file()
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
            and bool(re.search(r"(?<![\d.])[789](?:\.\d+)?\s*(?:秒|s(?:ec(?:onds?)?)?\b)", final, re.IGNORECASE))
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
        checks.update(
            intent.score(
                scenario,
                requests,
                cli_tools,
                final,
                phase=phase,
                prior_groups=prior_observed_groups(root, fixture, final),
            )
        )
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
    elif scenario == "reset-personal-key" and phase == 1:
        checks.pop("service_saved", None)
        calls = (
            [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
            if (root / "trace.jsonl").exists()
            else []
        )
        checks.update(reset_impact_checks(final))
        checks["no_reset_before_acknowledgment"] = not posts and not any(
            row["args"].get("action") in {"enroll", "key-challenge", "finish-enrollment", "command"} for row in calls
        )
    elif approved and enrollment_phase == 1:
        state = enrollment / "enrollment.json"
        key = enrollment / "key"
        expected_group = None if uses_challenge else GROUP
        bind_code = fixture.challenge_code if uses_challenge else CODE
        endpoint = CHALLENGES if uses_challenge else BINDINGS
        secret = enrollment / ("proof" if uses_challenge else "key")
        expected_secret = CHALLENGE_PROOF if uses_challenge else continuity.PENDING_KEY
        instruction_present = f"bind {bind_code}" in final and (
            "群" in final and ("@" in final or "＠" in final) if uses_challenge else GROUP in final
        )
        checks.update(
            exactly_one_enrollment=posts
            == [{"url": base.ORIGIN + endpoint, "method": "POST", "authorization_present": False, "status": 201}],
            manual_group_confirmation=instruction_present,
            target_group_matches_request=state.is_file()
            and json.loads(state.read_text())["target_group"] == expected_group,
            pending_secret_private=secret.is_file()
            and secret.stat().st_mode & 0o077 == 0
            and secret.read_text() == expected_secret,
            no_active_connection=not (enrollment / "connection.json").exists(),
        )
        if uses_challenge:
            checks.update(
                no_key_before_confirmation=not key.exists(),
                purpose_matches_request=state.is_file()
                and json.loads(state.read_text()).get("purpose") == fixture.challenge_purpose,
                uses_own_chat_account=bool(re.search(r"(?:本人|自己|你的).{0,12}(?:账号|账户|帐号)", final))
                and not re.search(
                    r"(?:使用|(?<!共)用)\s*(?:它|机器人|Agent)\s*(?:自己的?|的)?"
                    r"\s*(?:聊天|群聊|如流|飞书|IM)?\s*(?:账号|账户|帐号)",
                    final,
                    re.IGNORECASE,
                ),
            )
        if scenario == COLD_REQUEST:
            tool_trace = root / "trace.jsonl"
            calls = [json.loads(line) for line in tool_trace.read_text().splitlines()] if tool_trace.exists() else []
            checks["no_resource_mutation"] = not any(row["args"].get("action") == "command" for row in calls)
    elif enrollment_phase in {2, 3}:
        checks.update(no_mutation=not posts, connection_created=(enrollment / "connection.json").exists())
        if scenario == COLD_REQUEST and phase == 2:
            checks.pop("no_mutation")
            commands = [row for row in posts if row.get("command")]
            checks["exactly_one_correct_cli_write"] = (
                len(posts) == len(commands) == 1
                and commands[0]["status"] == 200
                and intent.equivalent(commands[0]["command"], COLD_COMMAND)
            )
            calls = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
            mutations = [
                row for row in calls if row["tool"] == "run_lockbot" and row["args"].get("action") == "command"
            ]
            checks["verified_before_and_after"] = bool(commands and mutations) and intent.mutation_observed(
                requests, commands[0], mutations[0], 7
            )
        if fixture.case.get("pending_enrollment_catalog"):
            tool_trace = root / "trace.jsonl"
            calls = (json.loads(line) for line in tool_trace.read_text().splitlines()) if tool_trace.exists() else ()
            catalog = "pending_enrollments" if enrollment_phase == 2 else "connections"
            field, path = (
                ("enrollment_dir", "private/binding")
                if enrollment_phase == 2
                else ("connection", "private/binding/connection.json")
            )
            checks["pending_enrollment_discovered" if enrollment_phase == 2 else "saved_connection_discovered"] = any(
                row["tool"] == "run_lockbot"
                and row["args"].get("action") == "connections"
                and any(item.get(field) == path for item in row["result"]["result"].get(catalog, []))
                for row in calls
                if "result" in row["result"]
            )
        if uses_challenge:
            key = enrollment / "key"
            checks["correct_key_saved"] = (
                key.is_file() and key.stat().st_mode & 0o077 == 0 and key.read_text() == fixture.enrolled_key
            )
        expected_node = intent.MATCHING_MACHINE if scenario == COLD_REQUEST else "node-a"
        checks["resources_reported"] = expected_node in final and any(
            row["url"] == base.ORIGIN + "/api/bots/7/resources"
            and row["status"] == 200
            and row["authorization_present"]
            for row in requests
        )
    else:
        checks.update(no_mutation=not posts, answer_present=bool(final.strip()))
    result = {"checks": checks, "observable_pass": all(checks.values()), "phase": phase, "simulation_only": True}
    if scenario in intent.PROMPTS:
        attempts, corrections = intent.command_attempts(cli_tools)
        result["cli_command_metrics"] = {
            "tool_attempts": len(attempts) + corrections,
            "pre_http_argument_corrections": corrections,
            "mutation_attempts": len(attempts),
            "resource_posts": len(posts),
        }
    if scenario in {
        "intent-my-locks",
        "intent-my-books",
        "intent-reminder-configured",
        "intent-reminder-unavailable",
        "intent-install-then-query",
        "intent-install-then-how-use",
        "intent-ip-tail-ambiguous",
        "intent-ip-tail-unavailable",
        "intent-public-ip-alias-ambiguous",
    }:
        result["manual_review_required"] = [
            "Review the exact reported owners, resources, cards and roles against the observed state. "
            "Check explanations against the installed Skill; no read-only timestamp proves reminder delivery."
        ]
    if scenario == "no-key":
        result["manual_review_required"] = [
            "Check the next-use explanation against the Skill and available sign-in state."
        ]
    if uses_challenge and enrollment_phase == 3:
        result["extra_enrollment_reads"] = sum(
            row["method"] == "GET" and urlsplit(row["url"]).path == f"{CHALLENGES}/{BINDING_ID}" for row in requests
        )
    return result


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
