"""Versioned real-argv transport for the legacy structured CLI fixture.

Only argument parsing is shared with onboarding/continuity. Recorded model args
and returned CLI output remain intact; parsed_cli is explicit derived evidence.
"""

import hashlib
import json

from skill_eval_packaged_cli import OPTIONS, PackagedCliHost

# Keep generic CLI fixture v33 on its archived descriptor. Release v6 opts into
# protocol 3 explicitly rather than silently changing other prepared suites.
PROTOCOL = 2
EXTRA_OPTIONS = {
    "discover": {"--origin", "--destination"},
    "enroll": {"--origin", "--destination", "--group", "--scope", "--agent-label"},
    "key-challenge": {"--origin", "--destination", "--purpose", "--scope"},
    "enrollment-status": {"--enrollment-dir"},
    "finish-enrollment": {"--enrollment-dir", "--confirm-bot-name"},
    "resources": {"--mine", "--exact-lease-times", "--node", "--model"},
    "resources-all": {"--directory", "--mine", "--exact-lease-times", "--node", "--model"},
    "command": {
        "--operation",
        "--node",
        "--device",
        "--duration",
        "--model",
        "--device-count",
        "--minimum-model-count",
        "--group",
        "--ref",
        "--metadata-file",
    },
    "alias": {"--node", "--set"},
    "metrics": {"--node"},
    "panel-link": set(),
}


def enabled(case):
    return case.get("cli_argv_protocol") in {1, 2, 3}


def add_argv_schema(definitions, case):
    if not enabled(case):
        return definitions
    for tool in definitions:
        if tool["name"] != "run_lockbot":
            continue
        schema = tool["inputSchema"]
        schema["properties"]["argv"] = {"type": "array", "items": {"type": "string"}, "maxItems": 48}
        schema["required"] = []
        if case.get("cli_argv_protocol") == 1:
            schema["oneOf"] = [{"required": ["action"]}, {"required": ["argv"]}]
        # Native clients can discard a tool with root-level schema unions.
        # The actual dispatcher still enforces argv/action exclusivity.
        if case.get("cli_argv_protocol") == 3:
            tool["description"] = (
                "Run the verified installed LockBot CLI with an argv array, as a terminal command without a shell. "
                "When needed, argv=['--help'] or argv=['COMMAND','--help'] discovers "
                "the installed commands and options. "
                "Inspection, installation and resource changes are separate commands; "
                "choose the scope the user requested. "
                "Legacy action fields are a compatibility alternative. Pass argv OR the legacy action fields."
            )
        else:
            tool["description"] += " Prefer argv for the real CLI syntax; pass argv OR the legacy action fields."
    return definitions


def parsed_arguments(argv):
    """Describe only tokens actually supplied; never fill omitted constraints."""
    parsed, index = {}, 0
    repeat = {"node": "nodes", "device": "devices"}
    switches = {
        "mine",
        "authorized",
        "whole-node",
        "same-devices-on-all-nodes",
        "full",
        "with-metrics",
        "exact-lease-times",
        "clear",
        "help",
    }
    while index < len(argv):
        item = argv[index]
        if not item.startswith("-"):
            if "action" not in parsed:
                parsed["action"] = item
            elif parsed["action"] == "resource" and "resource_action" not in parsed:
                parsed["resource_action"] = item
            elif parsed.get("resource_action") in {"view", "devices"} and "ref" not in parsed:
                parsed["ref"] = item
            index += 1
            continue
        option, equals, value = item[2:].partition("=")
        if option in switches and not equals:
            parsed[option.replace("-", "_")] = True
        elif option == "json" and not equals and (index + 1 == len(argv) or argv[index + 1].startswith("-")):
            parsed["json"] = ""
        else:
            if not equals:
                index += 1
                value = argv[index]
            name = option.replace("-", "_")
            if option in repeat:
                parsed.setdefault(repeat[option], []).append(value)
            else:
                parsed[name] = value
        index += 1
    return parsed


def for_grading(rows, case):
    """Use derived argv tokens without altering recorded arguments or tool output."""
    if not enabled(case):
        return rows
    result = []
    for row in rows:
        if "argv" in row.get("args", {}) and isinstance(row.get("parsed_cli"), dict):
            parsed = dict(row["parsed_cli"])
            for field in ("connection", "enrollment_dir", "directory", "destination", "key_file"):
                if isinstance(parsed.get(field), str):
                    parsed[field] = parsed[field].removeprefix("/synthetic-user/")
            for field in ("device_count", "minimum_model_count"):
                if isinstance(parsed.get(field), str) and parsed[field].isdigit():
                    parsed[field] = int(parsed[field])
            result.append({**row, "args": parsed, "recorded_model_args": row["args"]})
        else:
            parsed = legacy_arguments(row.get("args", {}))
            result.append(
                {**row, "args": parsed, "recorded_model_args": row["args"]} if parsed != row.get("args", {}) else row
            )
    return result


def private_locator(value):
    return value.removeprefix("/synthetic-user/") if isinstance(value, str) else value


def legacy_arguments(args):
    """Map the documented user namespace for mixed calls; keep recorded args intact."""
    paths = {"connection", "directory", "enrollment_dir", "destination", "key_file"}
    return {name: private_locator(value) if name in paths else value for name, value in args.items()}


def enrollment_directory(fixture):
    private = fixture.workspace / "private"
    fallback = private / "binding"
    if not enabled(fixture.case):
        return fallback
    matches = []
    for index, path in enumerate(private.rglob("enrollment.json")):
        if index >= 256:
            return fallback
        if path.is_symlink() or path.resolve() != path.absolute() or path.stat().st_size > 16384:
            continue
        try:
            state = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if (
            state.get("approved_origin") == fixture.origin
            and state.get("instance_id") == fixture.manifest()["instance_id"]
            and isinstance(state.get("binding_id"), str)
        ):
            matches.append(path.parent)
    return matches[0] if len(matches) == 1 else fallback


def bounded_evidence(fixture, event, requests, groups):
    """Join only delivered bounded rows to their actual verified native wire scope."""
    from lockbot.backend.app.agent_skill.scripts.lockbot_cli import resource_reference

    result = event.get("result", {})
    parsed = event.get("args", {})
    span = event.get("http_request_span")
    if (
        parsed.get("action") != "resource"
        or result.get("exit_code") != 0
        or not isinstance(span, list)
        or len(span) != 2
        or not all(type(value) is int for value in span)
        or not 0 <= span[0] <= span[1] <= len(requests)
        or not event.get("argv", [""])[0].endswith("/scripts/lockbot_cli.py")
    ):
        return event
    page = result.get("result", {})
    verified_rows, observed_groups = [], []
    wire = requests[span[0] : span[1]]
    for identifier, name in groups.items():
        path = fixture.workspace / f"private/group-{identifier}.json"
        if not path.is_file():
            continue
        pinned = json.loads(path.read_text())
        matches = [
            row
            for row in wire
            if row.get("method") == "GET"
            and row.get("status") == 200
            and row.get("authorization_present")
            and row.get("resource_bot") == identifier
            and row.get("resource_observation", {}).get("instance_id") == pinned["instance_id"]
            and row.get("resource_observation", {}).get("bot_uuid") == pinned["bot_uuid"]
            and row.get("resource_observation", {}).get("actor") == pinned["actor"]
        ]
        if not matches:
            continue
        observed_groups.append(name)
        for row in page.get("rows", []):
            node = row.get("node")
            if not isinstance(node, str) or row.get("ref") != resource_reference(pinned, node, width=64):
                continue
            age = matches[-1].get("resource_read_state", {}).get("age_seconds")
            verified_rows.append({"group": name, "row": row, "age_seconds": age})
    return {**event, "verified_bounded_rows": verified_rows, "verified_read_groups": observed_groups}


class CliArgvHost(PackagedCliHost):
    def __init__(self, fixture, actions):
        options = {name: set(EXTRA_OPTIONS.get(name, OPTIONS.get(name, set()))) for name in actions}
        super().__init__(fixture, artifact=fixture.artifact, options=options)
        self.user, self.skills, self.private = self.workspace, self.workspace / "skills", self.workspace / "private"

    def verified(self, destination):
        expected = self.fixture.case.get("installed_tree_sha256")
        if self.fixture.case.get("release_scenario") != "update-inquiry" or not expected:
            return super().verified(destination)
        if destination != self.fixture.installation:
            return False
        digest = hashlib.sha256()
        for path in sorted(path for path in destination.rglob("*") if path.is_file()):
            if path.is_symlink():
                return False
            digest.update(path.relative_to(destination).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest() == expected

    def open(self, request, timeout):
        return self.fixture.open(request, timeout)


def invoke(fixture, args, execution, actions):
    if "action" in args or set(args) != {"argv"}:
        return {"error": "use_argv_or_legacy_action"}
    if fixture.case.get("release_scenario") == "update-inquiry":
        actions = {"discover", "connections", "context", "resources", "resources-all"}
    host = CliArgvHost(fixture, actions)
    try:
        argv = host.arguments(args["argv"], {})
        parsed = parsed_arguments(argv)
        if (
            fixture.case.get("scenario") == "reset-personal-key"
            and parsed.get("purpose") == "reset"
            and not fixture.case.get("reset_impact_acknowledged")
        ):
            return {"error": "reset_impact_acknowledgment_required"}
        execution["parsed_cli"] = host.virtual(parsed)
        return host.virtual(
            host.invoke(
                {"script": host.virtual(str(fixture.installation / "scripts/lockbot_cli.py")), "argv": args["argv"]},
                execution,
            )
        )
    except (ValueError, OSError, KeyError, TypeError) as error:
        return {"error": "fixture_cli_arguments_rejected", "reason": str(error)}
