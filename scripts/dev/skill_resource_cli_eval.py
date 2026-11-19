#!/usr/bin/env python3
"""Read-only short-prompt evaluations of the actual bounded resource CLI.

Fixtures and model outcomes remain separate: this module runs the frozen bundle,
records its HTTP calls, and grades observable coverage, not prose correctness.
"""

import argparse
import contextlib
import copy
import io
import json
import os
import runpy
from pathlib import Path
from unittest.mock import patch

import skill_cli_eval as cli

VERSION = 2
PROMPTS = {
    "find-p800": "帮我找台P800的机器",
    "list-resources": "看看我能申请什么资源？",
    "all-resources": "资源都列一下",
    "node-detail": "node-10什么情况",
    "device-detail": "node-10有哪些卡？",
    "metrics": "P800的利用率多少？",
}
VALUE_OPTIONS = {"--node", "--model", "--search", "--limit", "--cursor", "--json"}
SWITCHES = {"--mine", "--with-metrics", "--exact-lease-times", "--help"}


def prepare(root, artifact, scenario):
    if scenario not in PROMPTS:
        raise ValueError("Unknown resource CLI scenario")
    cli.prepare(root, artifact, "intent-find-p800", fixture_version=30)
    case = json.loads((root / "case.json").read_text())
    cli.base.save_json(
        root / "case.json",
        {
            **case,
            "fixture_family": "resource-cli",
            "resource_cli_version": VERSION,
            "resource_cli_scenario": scenario,
        },
    )
    (root / "prompt.txt").write_text(PROMPTS[scenario])
    config = json.loads((root / "mcp.json").read_text())
    config["mcpServers"]["fixture"]["args"] = [str(Path(__file__).resolve()), "serve", str(root.resolve())]
    cli.base.save_json(root / "mcp.json", config)
    # Preserve the ordinary catalog, not a task-specific suggested solution.
    context = (
        "Use only the supplied isolated fixture tools. The Skill is already installed; "
        "read_lockbot_skill reads its instructions and references. run_lockbot runs its "
        "actual verified packaged CLI with an argv array. Paths and HTTP stay inside the "
        "synthetic user's approved workspace. Keep private connection paths and credentials "
        "out of replies. No prior conversation is supplied."
    )
    context += cli.installed_skill_catalog(Fixture(root).installation, Fixture(root).artifact)
    (root / "system.txt").write_text(context)


class Fixture(cli.Fixture):
    def http(self, args):
        response = super().http(args)
        if (
            self.case["resource_cli_scenario"] == "all-resources"
            and args["method"] == "GET"
            and args["url"].endswith("/resources")
            and response.get("status") == 200
            and args.get("headers", {}).get("Authorization") == "Bearer " + cli.base.KEY
        ):
            response = copy.deepcopy(response)
            first = response["body"]["resources"][0]
            response["body"]["resources"] = [
                {**copy.deepcopy(first), "id": f"node-many-{index:02d}"} for index in range(27)
            ]
        return response

    def run_cli(self, args, execution):
        supplied = args.get("argv")
        if (
            not isinstance(supplied, list)
            or not 2 <= len(supplied) <= 48
            or any(not isinstance(value, str) or len(value) > 16384 or "\0" in value for value in supplied)
            or supplied[0] != "resource"
            or supplied[1] not in {"list", "view", "devices"}
        ):
            return {"error": "unsupported_fixture_argv"}
        position = 2
        if supplied[1] in {"view", "devices"}:
            if position >= len(supplied) or supplied[position].startswith("-"):
                return {"error": "resource_reference_required"}
            position += 1
        while position < len(supplied):
            option, equals, value = supplied[position].partition("=")
            if option in SWITCHES and not equals:
                position += 1
                continue
            if option not in VALUE_OPTIONS:
                return {"error": "unsupported_fixture_option"}
            if (
                option == "--json"
                and not equals
                and (position + 1 == len(supplied) or supplied[position + 1].startswith("-"))
            ):
                position += 1
                continue
            if not equals:
                position += 1
                if position >= len(supplied):
                    return {"error": "fixture_option_value_required"}
            position += 1
        if not all(
            (self.installation / name).is_file() and (self.installation / name).read_bytes() == content
            for name, content in self.artifact["contents"].items()
        ):
            return {"error": "verified_package_required"}
        script_path = self.installation / "scripts/lockbot_cli.py"
        script = runpy.run_path(str(script_path))
        execution["argv"] = [str(script_path), *supplied]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("urllib.request.build_opener", return_value=self),
            patch.dict(os.environ, {"LOCKBOT_HOME": str(self.workspace / "private")}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = script["main"](supplied)
            except SystemExit as error:
                code = error.code
        execution["exit_code"] = code
        output = stdout.getvalue() if code == 0 else stderr.getvalue()
        try:
            return json.loads(output.replace(str(self.workspace) + "/", ""))
        except ValueError:
            return {"exit_code": code, "text": output.replace(str(self.workspace) + "/", "")}


def tools(_root=None):
    return [
        {
            "name": "read_lockbot_skill",
            "description": "Read installed Skill instructions or a linked reference.",
            "inputSchema": cli.base.schema(
                {
                    "offset": {"type": "integer", "minimum": 0},
                    "reference": {"type": "string", "enum": list(cli.REFERENCE_PATHS)},
                },
                ["offset"],
            ),
        },
        {
            "name": "run_lockbot",
            "description": "Run the actual installed CLI with an argv array; no shell.",
            "inputSchema": cli.base.schema(
                {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 48,
                    }
                },
                ["argv"],
            ),
        },
    ]


def score(root, final):
    fixture = Fixture(root)

    def read(name):
        path = root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    trace, http = read("trace.jsonl"), read("cli-http.jsonl")
    calls = [row for row in trace if row["tool"] == "run_lockbot"]
    successful = [row for row in calls if row.get("exit_code") == 0 and not row["result"].get("error")]
    pages = [row["result"].get("result", {}) for row in successful]
    scenario = fixture.case["resource_cli_scenario"]
    checks = {
        "answer_present": bool(final.strip()),
        "read_only_http": bool(http) and all(row["method"] == "GET" for row in http),
        "packaged_cli_succeeded": bool(successful),
        "skill_read": any(row["tool"] == "read_lockbot_skill" for row in trace),
        "no_credentials_in_reply": all(key not in final for key in (cli.base.KEY, cli.continuity.SECOND_KEY)),
    }
    if scenario == "all-resources":
        seen = {row["ref"] for page in pages for row in page.get("rows", [])}
        checks["all_fixture_rows_observed"] = len(seen) == 28
        checks["pagination_completed"] = any(
            page.get("has_more") is False and page.get("traversal_exhausted") is True for page in pages
        )
    elif scenario == "node-detail":
        if fixture.case.get("resource_cli_version", 1) < 2:
            checks["exact_detail_observed"] = any(
                call["args"]["argv"][1] == "view"
                and any(row.get("node") == "node-10" for row in call["result"].get("result", {}).get("rows", []))
                for call in successful
            )
        else:
            # A compact list can already carry the requested status and hardware.
            # Requiring another view would reward a redundant network read.
            checks["exact_detail_observed"] = any(
                row.get("node") == "node-10"
                and isinstance(row.get("ref"), str)
                and isinstance(row.get("hardware"), list)
                and isinstance(row.get("states"), dict)
                for page in pages
                for row in page.get("rows", [])
            )
    elif scenario == "device-detail":
        checks["device_page_observed"] = any(
            call["args"]["argv"][1] == "devices"
            and call["result"].get("result", {}).get("node_ref", "").endswith("/node-10")
            for call in successful
        )
    elif scenario == "metrics":
        checks["metrics_explicitly_requested"] = any("--with-metrics" in row["args"]["argv"] for row in calls)
        checks["metrics_coverage_reported"] = any("metrics_read_complete" in page for page in pages)
    return {
        "scenario": scenario,
        "checks": checks,
        "observable_pass": all(checks.values()),
        "simulation_only": True,
        "manual_review_required": [
            "Check answer correctness, coverage caveats and unknown utilization against the actual tool output.",
            "Observable checks do not establish semantic correctness or live service acceptance.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "serve", "score"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--scenario", choices=PROMPTS)
    parser.add_argument("--final-file", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.artifacts, args.scenario)
    elif args.action == "score":
        print(json.dumps(score(args.root, args.final_file.read_text()), ensure_ascii=False))
    else:
        cli.base.Fixture, cli.base.tools = Fixture, tools
        cli.base.serve(args.root)


if __name__ == "__main__":
    main()
