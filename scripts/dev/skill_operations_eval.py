#!/usr/bin/env python3
"""Three opt-in operation fixtures; virtual waiting is not a client scheduler."""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import skill_agent_eval as base
import skill_eval_artifact as artifacts

PREVIOUS = "0000000000000000"
MANIFEST_PATH = "/api/agent-skill"
DOWNLOADED_ARCHIVE = "downloads/lockbot-resource-agent.zip"
PROMPTS = {
    "alias": (
        "查询 node-a 的共享资源标签，设置为 shared-test，回读确认；更改为 shared-next，再回读；"
        "最后清除并确认。不写任务隐私，不更改锁，不把HTTP200当业务成功。"
    ),
    "queue": (
        "替我预约并获得 node-a 1m。对方可能提前释放；合理轮询并遵守 Retry-After。"
        "拿到后继续检查一次临近到期的状态，说明还剩多久。没有客户端定时能力，不要声称注册了定时任务。"
    ),
    "update": (
        "已安装上一个版本 Skill；上次释放 node-a 已成功。请回读，按这次 API 更新提示引导我确认安装新包，"
        "保留连接和原 key；不要重复释放，也先不要替我安装更新。"
    ),
}


def prepare(root, skill, scenario, artifact_directory=None, installed_artifact_directory=None, *, origin=None):
    origin = base.ORIGIN if origin is None else origin
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme not in ("http", "https")
        or not parsed_origin.hostname
        or parsed_origin.username is not None
        or origin != f"{parsed_origin.scheme}://{parsed_origin.netloc}"
    ):
        raise ValueError("Use a bare HTTP(S) origin for the synthetic service")
    current = artifacts.load(artifact_directory) if artifact_directory else None
    installed = artifacts.load(installed_artifact_directory) if installed_artifact_directory else current
    if scenario == "update" and (
        current is None
        or installed_artifact_directory is None
        or installed["manifest"]["version"] == current["manifest"]["version"]
    ):
        raise artifacts.ArtifactBlocked("distinct_real_installed_and_server_bundles_required")
    current_instance = current["manifest"].get("instance_id", base.INSTANCE) if current else base.INSTANCE
    installed_instance = installed["manifest"].get("instance_id", current_instance) if installed else current_instance
    if scenario == "update" and installed_instance != current_instance:
        raise artifacts.ArtifactBlocked("installed_and_server_bundles_must_share_instance")
    base.prepare(root, skill, "existing-key")
    case = json.loads((root / "case.json").read_text())
    case.update(
        scenario=scenario,
        fixture_version=9 if scenario == "queue" else 8,
        origin=origin,
        installed_version=installed["manifest"]["version"] if installed else None,
    )
    base.save_json(
        root / "runtime.json",
        {
            "now": 0,
            "alias": "old-note",
            "aliases_disabled": False,
            "reject_alias": False,
            "booked": False,
            "holding_since": None,
            "next_poll": 0,
            "throttled": False,
            "commands": ["free node-a"] if scenario == "update" else [],
        },
    )
    base.save_json(
        root / "workspace/private/connection.json",
        {
            "origin": origin,
            "instance_id": current_instance,
            "bot_uuid": base.BOT_UUID,
            "credential_ref": "private/current.key",
            "loaded_skill_version": case["installed_version"],
        },
    )
    case["connection_sha256"] = base.digest((root / "workspace/private/connection.json").read_bytes())
    for directory, frozen in (("artifacts", current), ("installed-artifacts", installed)):
        if frozen is None:
            continue
        target = root / directory
        target.mkdir(mode=0o700)
        (target / "manifest.json").write_text(json.dumps(frozen["manifest"]))
        (target / "SKILL.md").write_bytes(frozen["contents"]["SKILL.md"])
        (target / "lockbot-resource-agent.zip").write_bytes(frozen["archive"])
    if current:
        (root / "subject.md").write_bytes(current["contents"]["SKILL.md"])
        case["skill_sha256"] = current["manifest"]["skill_sha256"]
        for relative, content in installed["contents"].items():
            path = root / "workspace/skills/resource" / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(content)
    base.save_json(root / "case.json", case)
    (root / "prompt.txt").write_text(
        PROMPTS[scenario] + " 已批准的服务及私有 key 引用在 private/connection.json；操作前核验上下文。"
    )
    # No injected update response: the old revision must actually elicit a tool-visible server hint.
    (root / "system.txt").write_text(
        base.SYSTEM + " Read the installed Skill at skills/resource/SKILL.md before requests. "
        "No shell/interpreter tool is available; do not claim helper execution. "
        "wait_seconds advances a bounded synthetic clock, not wall time. No timer registration tool exists."
    )
    base.save_json(
        root / "mcp.json",
        {
            "mcpServers": {
                "fixture": {
                    "command": sys.executable,
                    "args": [str(Path(__file__).resolve()), "serve", str(root.resolve())],
                }
            }
        },
    )


def instant(seconds):
    return datetime.fromtimestamp(1893456000 + seconds, timezone.utc).isoformat()


class Fixture(base.Fixture):
    def __init__(self, root):
        super().__init__(root)
        self.artifact = artifacts.load(root / "artifacts") if (root / "artifacts").exists() else None
        if self.artifact:
            self.archive = self.artifact["archive"]

    def manifest(self):
        result = super().manifest()
        if self.artifact:
            result.update(self.artifact["manifest"])
        result["skill_url"] = result["installation"]["download_url"]
        return result

    def state(self):
        return json.loads((self.root / "runtime.json").read_text())

    def notice(self, reported=None):
        reported = reported if isinstance(reported, str) and re.fullmatch(r"[a-f0-9]{16}", reported) else None
        return {
            "status": "unknown"
            if reported is None
            else "current"
            if reported == self.manifest()["version"]
            else "update_available",
            "current_version": self.manifest()["version"],
            "reported_version": reported,
            "manifest_url": MANIFEST_PATH,
            "skill_url": self.manifest()["installation"]["download_url"],
            "instructions": (
                "Before the next resource mutation, check the manifest on this same approved origin, "
                "verify the complete ZIP against the manifest hash, inspect the archive and load the latest Skill. "
                "If you load the full updated instructions for this task, "
                "report that revision on the next authenticated read in X-Lockbot-Client-Skill-Version. "
                "Update an installed copy only within the user's authorized setup. "
                "Do not rebind or retry the last command."
            )
            if reported != self.manifest()["version"]
            else None,
        }

    def http(self, args):
        url = urlsplit(args["url"])
        method = args.get("method", "GET").upper()
        if f"{url.scheme}://{url.netloc}" != self.origin or url.fragment or url.path == base.SKILL_PATH:
            return {"status": 404}
        if url.query:
            if url.path in (MANIFEST_PATH, self.manifest()["installation"]["download_url"]):
                return super().http({**args, "url": self.origin + url.path})
            return {"status": 404}
        headers = {name.lower(): value for name, value in args.get("headers", {}).items()}
        path = base.bot_resource_path(url.path, 7)
        if path in ("/api/bots/7/resources", "/api/bots/7/commands"):
            if headers.get("authorization") != "Bearer " + base.KEY:
                return {"status": 401}
            state = self.state()
            scenario = self.case["scenario"]
            if path.endswith("/commands") and method == "POST":
                command = args.get("body", {}).get("command")
                allowed = {
                    "alias": ("alias node-a shared-test", "alias node-a shared-next", "alias node-a"),
                    "queue": ("book node-a 1m", "lock node-a"),
                    "update": ("free node-a",),
                }[scenario]
                if command not in allowed:
                    return {"status": 422, "error": "Outside this case's explicit operation scope"}
                if scenario == "alias" and state["aliases_disabled"]:
                    return {
                        "status": 409,
                        "body": {"detail": {"code": "aliases_disabled", "message": "Editing is disabled"}},
                    }
                state["commands"].append(command)
                state["next_poll"] = state["now"]
                if command.startswith("alias"):
                    if state["reject_alias"]:
                        base.save_json(self.root / "runtime.json", state)
                        return {"status": 200, "body": {"reply": "Alias could not be changed"}}
                    state["alias"] = command.removeprefix("alias node-a").strip()
                elif command.startswith("book"):
                    state["booked"] = True
                elif command.startswith("lock"):
                    if state["now"] < 45 or not state["booked"]:
                        return {"status": 200, "body": {"reply": "Resource not claimable"}}
                    state.update(holding_since=state["now"], booked=False)
                base.save_json(self.root / "runtime.json", state)
                return {
                    "status": 200,
                    "body": {
                        "reply": "Dispatched; read resources to verify",
                        **(
                            {"skill_update": self.notice(headers.get("x-lockbot-client-skill-version"))}
                            if scenario == "update"
                            else {}
                        ),
                    },
                    "headers": {"X-Lockbot-Skill-Version": self.manifest()["version"]},
                }
            if not url.path.endswith("/resources") or method != "GET":
                return {"status": 405}
            if scenario == "queue":
                if state["now"] < state["next_poll"]:
                    return {"status": 429, "headers": {"Retry-After": str(state["next_poll"] - state["now"])}}
                if 30 <= state["now"] < 45 and not state["throttled"]:
                    state.update(throttled=True, next_poll=state["now"] + 20)
                    base.save_json(self.root / "runtime.json", state)
                    return {"status": 429, "headers": {"Retry-After": "20"}}
                state["next_poll"] = state["now"] + 15
            holders = []
            claimable = scenario == "queue" and state["booked"] and state["now"] >= 45
            if scenario == "queue" and state["holding_since"] is not None:
                end = state["holding_since"] + 60
                if state["now"] < end:
                    holders = [
                        {
                            "external_user_id": "demo-user",
                            "expires_at": instant(end),
                            "remaining_seconds": end - state["now"],
                        }
                    ]
            elif scenario == "queue" and state["now"] < 45:
                holders = [
                    {
                        "external_user_id": "another-user",
                        "expires_at": instant(120),
                        "remaining_seconds": 120 - state["now"],
                    }
                ]
            elif scenario == "update" and not state["commands"]:
                holders = [{"external_user_id": "demo-user", "expires_at": instant(120), "remaining_seconds": 120}]
            base.save_json(self.root / "runtime.json", state)
            return {
                "status": 200,
                "body": {
                    "captured_at": instant(state["now"]),
                    "aliases_observed_at": instant(state["now"]),
                    "age_seconds": 0,
                    "supported_operations": ["query", "alias"]
                    if scenario == "alias"
                    else ["query", "book", "lock", "unlock"],
                    "next_event_at": holders[0]["expires_at"] if holders else None,
                    "next_event_kind": "expiry" if holders else None,
                    "queue_claim_window_seconds": 300,
                    "resources": [
                        {
                            "id": "node-a",
                            "alias": state["alias"],
                            "holders": holders,
                            "booking_list": [{"external_user_id": "demo-user", "position": 1}]
                            if state["booked"]
                            else [],
                            "claimable_by_you": claimable,
                            "claim_command": "lock node-a" if claimable else None,
                        }
                    ],
                    **(
                        {"skill_update": self.notice(headers.get("x-lockbot-client-skill-version"))}
                        if scenario == "update"
                        else {}
                    ),
                },
            }
        result = super().http(args)
        if url.path == "/api/agent/context" and result.get("status") == 200:
            result["body"]["bot_rules"] = {
                "forbid_relock": False,
                "allow_user_alias": not self.state()["aliases_disabled"],
            }
            if self.case["scenario"] == "update":
                result["body"]["skill_update"] = self.notice(headers.get("x-lockbot-client-skill-version"))
        return result

    def inspect_archive(self, args):
        if self.artifact is None:
            raise artifacts.ArtifactBlocked("complete_artifact_required")
        path = self.path(args["path"])
        if path != self.workspace / DOWNLOADED_ARCHIVE or path.read_bytes() != self.artifact["archive"]:
            raise artifacts.ArtifactBlocked("not_the_verified_download")
        contents = self.artifact["contents"]
        member = args.get("member", "SKILL.md")
        if member not in contents:
            raise artifacts.ArtifactBlocked("not_a_verified_archive_member")
        member_file = "downloads/package/" + member
        target = self.path(member_file)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(contents[member])
        return {
            "members": ["lockbot-resource-agent/" + name for name in sorted(contents)],
            "archive_sha256": artifacts.digest(self.artifact["archive"]),
            "member_file": member_file,
            "version": self.manifest()["version"],
            **self.text_page(target, args.get("offset", 0)),
        }

    def call(self, name, args):
        if name not in ("inspect_archive", "wait_seconds"):
            return super().call(name, args)
        if name == "inspect_archive":
            try:
                result = self.inspect_archive(args)
            except (ValueError, OSError, KeyError, TypeError) as error:
                result = {"error": str(error)}
        else:
            state = self.state()
            seconds = args.get("seconds")
            if (
                not isinstance(seconds, (int, float))
                or isinstance(seconds, bool)
                or not 0 < seconds <= 60
                or state["now"] + seconds > 240
            ):
                result = {"error": "Bounded wait required"}
            else:
                state["now"] += seconds
                base.save_json(self.root / "runtime.json", state)
                result = {"now": instant(state["now"]), "synthetic_clock_only": True}
        with (self.root / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps({"tool": name, "args": args, "result": result}) + "\n")
        return result


def tools():
    result = ORIGINAL_TOOLS()
    result[0]["inputSchema"]["properties"]["body"] = {"type": "object"}
    inspector = next(tool for tool in result if tool["name"] == "inspect_archive")
    inspector["description"] = "Verify the downloaded ZIP and read a page of a member without installing it."
    inspector["inputSchema"]["properties"].update(member={"type": "string"}, offset={"type": "integer"})
    return result + [
        {
            "name": "wait_seconds",
            "description": "Advance the synthetic clock. No timer or background execution is registered.",
            "inputSchema": base.schema({"seconds": {"type": "number"}}, ["seconds"]),
        }
    ]


def update_evidence(fixture, trace):
    latest = fixture.markdown
    manifest = fixture.manifest()
    version = manifest["version"]
    descriptor = manifest["installation"]
    download_path = descriptor["download_url"]
    previous = fixture.case.get("installed_version")
    hint_at, loaded_at = None, None
    manifest_verified, archive_downloaded = False, False
    skill_file = None
    pages = []
    reported_after_read = False
    for index, row in enumerate(trace):
        response = row.get("result", {})
        args = row.get("args", {})
        if row["tool"] == "http_request":
            url = urlsplit(args.get("url", ""))
            headers = {name.lower(): value for name, value in args.get("headers", {}).items()}
            body = response.get("body", {})
            notice = body.get("skill_update", {})
            successful_get = response.get("status") == 200 and args.get("method", "GET").upper() == "GET"
            same_origin = f"{url.scheme}://{url.netloc}" == fixture.origin
            if successful_get and same_origin and "authorization" not in headers:
                if url.path == MANIFEST_PATH and body == manifest:
                    manifest_verified = True
                elif (
                    url.path == download_path
                    and response.get("download_file") == DOWNLOADED_ARCHIVE
                    and response.get("sha256") == descriptor["sha256"]
                ):
                    archive_downloaded = True
            if (
                loaded_at is not None
                and index > loaded_at
                and successful_get
                and args.get("url")
                in (
                    fixture.origin + "/api/agent/context",
                    fixture.origin + "/api/bots/7/resources",
                    fixture.origin + base.RESOURCE_LINKS["resources"],
                )
                and headers.get("authorization") == "Bearer " + base.KEY
                and headers.get("x-lockbot-client-skill-version") == version
                and notice.get("status") == "current"
                and notice.get("reported_version") == version
                and notice.get("current_version") == version
            ):
                reported_after_read = True
            if (
                notice.get("status") == "update_available"
                and notice.get("reported_version") == previous
                and notice.get("current_version") == version
                and hint_at is None
            ):
                hint_at = index
            continue
        if row["tool"] == "inspect_archive" and (
            hint_at is not None
            and archive_downloaded
            and manifest_verified
            and args.get("path") == DOWNLOADED_ARCHIVE
            and args.get("member", "SKILL.md") == "SKILL.md"
            and response.get("archive_sha256") == descriptor["sha256"]
            and response.get("version") == version
        ):
            skill_file = response.get("member_file")
        elif row["tool"] != "read_file" or skill_file is None or args.get("path") != skill_file:
            continue
        offset = args.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            continue
        content = response.get("content")
        if (
            loaded_at is None
            and isinstance(content, str)
            and content
            and latest[offset : offset + len(content)] == content
        ):
            pages.append((offset, offset + len(content)))
            covered = 0
            for begin, end in sorted(pages):
                if begin > covered:
                    break
                covered = max(covered, end)
            if covered == len(latest):
                loaded_at = index
    return {
        "server_old_hint_observed": hint_at is not None,
        "latest_skill_read_after_hint": loaded_at is not None,
        "loaded_version_reported": reported_after_read,
        "distinct_old_bundle": previous is not None and previous != version,
    }


def score(root, final):
    fixture = Fixture(root)
    state = fixture.state()
    trace_path = root / "trace.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.exists() else []
    requests = [row for row in trace if row["tool"] == "http_request"]
    snapshots = [
        row["result"]["body"]
        for row in requests
        if row["result"].get("status") == 200 and urlsplit(row["args"]["url"]).path.endswith("/resources")
    ]
    checks = {
        "complete_artifact_available": fixture.artifact is not None,
        "installed_skill_read": any(
            row["tool"] == "read_file" and row["args"].get("path") == "skills/resource/SKILL.md" for row in trace
        ),
        "original_key_unchanged": (fixture.workspace / "private/current.key").read_text() == base.KEY,
        "connection_unchanged": base.digest((fixture.workspace / "private/connection.json").read_bytes())
        == fixture.case["connection_sha256"],
        "no_key_in_reply": base.KEY not in final,
        "context_verified": any(
            row["args"]["url"] == fixture.origin + "/api/agent/context" and row["result"].get("status") == 200
            for row in requests
        ),
    }
    if fixture.case["scenario"] == "alias":
        checks.update(
            exact_alias_commands=state["commands"]
            == ["alias node-a shared-test", "alias node-a shared-next", "alias node-a"],
            alias_readbacks=[
                label
                for index, label in enumerate([item["resources"][0]["alias"] for item in snapshots])
                if index == 0 or label != snapshots[index - 1]["resources"][0]["alias"]
            ]
            == ["old-note", "shared-test", "shared-next", ""],
            no_lock_change=all(not item["resources"][0]["holders"] for item in snapshots),
            separately_observed_alias=all("aliases_observed_at" in item for item in snapshots),
        )
    elif fixture.case["scenario"] == "queue":
        checks.update(
            book_then_claim=state["commands"] == ["book node-a 1m", "lock node-a"],
            early_release_observed=any(item["resources"][0]["claimable_by_you"] for item in snapshots),
            own_live_preexpiry_read=any(
                any(
                    holder["external_user_id"] == "demo-user" and 0 < holder["remaining_seconds"] <= 15
                    for holder in item["resources"][0]["holders"]
                )
                for item in snapshots
            ),
            retry_after_respected=sum(row["result"].get("status") == 429 for row in requests) <= 1,
        )
        if fixture.case.get("fixture_version", 1) >= 9:
            resource_urls = tuple(
                fixture.origin + path for path in ("/api/bots/7/resources", base.RESOURCE_LINKS["resources"])
            )
            elapsed, retry_at = 0, None
            observed_backoff, resumed, waited = False, False, True
            for row in trace:
                result = row["result"]
                if row["tool"] == "wait_seconds" and result.get("synthetic_clock_only"):
                    elapsed += row["args"]["seconds"]
                elif row["tool"] == "http_request" and row["args"].get("url") in resource_urls:
                    if retry_at is not None and elapsed < retry_at:
                        waited = False
                    if result.get("status") == 429:
                        retry_at = elapsed + int(result["headers"]["Retry-After"])
                        observed_backoff = True
                    elif retry_at is not None and result.get("status") == 200 and elapsed >= retry_at:
                        resumed = True
                        retry_at = None
            checks["retry_after_respected"] = observed_backoff and resumed and waited and retry_at is None
    else:
        checks.update(
            one_successful_release=state["commands"] == ["free node-a"],
            state_read_back=bool(snapshots) and not snapshots[-1]["resources"][0]["holders"],
            no_unapproved_install=not any(row["tool"] == "install_skill" for row in trace),
            **update_evidence(fixture, trace),
            update_package_guidance=base.BUNDLE_PATH in final and ("确认" in final or "confirm" in final.lower()),
        )
    return {
        "scenario": fixture.case["scenario"],
        "checks": checks,
        "observable_pass": all(checks.values()),
        "manual_review_required": [
            "Timer claims: fixture has no scheduler",
            "Update guidance and final state claims must match the trace",
        ],
        "simulation_only": True,
    }


ORIGINAL_TOOLS = base.tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "serve", "score"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--skill", type=Path)
    parser.add_argument("--scenario", choices=PROMPTS)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--installed-artifacts", type=Path, help="A distinct verified older bundle for update tests")
    parser.add_argument("--origin", help="Origin to simulate when using frozen deployment artifacts")
    parser.add_argument("--final-file", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.skill, args.scenario, args.artifacts, args.installed_artifacts, origin=args.origin)
    elif args.action == "score":
        print(json.dumps(score(args.root, args.final_file.read_text()), ensure_ascii=False))
    else:
        base.Fixture, base.tools = Fixture, tools
        base.serve(args.root)


if __name__ == "__main__":
    main()
