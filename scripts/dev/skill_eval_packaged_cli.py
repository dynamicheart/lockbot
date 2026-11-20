"""Constrained offline host for a verified package; no model-supplied code or shell."""

import contextlib
import io
import json
import os
import runpy
import urllib.error
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from urllib.parse import urlsplit

USER_ROOT = PurePosixPath("/synthetic-user")
SKILL_ROOT = USER_ROOT / "skills"
LOCKBOT_ROOT = USER_ROOT / "lockbot"
PATH_OPTIONS = {"--connection", "--destination", "--key-file", "--directory", "--enrollment-dir", "--metadata-file"}
OPTIONS = {
    "discover": {"--origin", "--destination"},
    "init": {"--origin", "--key-file", "--destination", "--scope", "--agent-label"},
    "recover": {"--destination", "--scope"},
    "connections": {"--directory"},
    "context": set(),
    "resources": {"--mine", "--exact-lease-times"},
    "resources-all": {"--directory", "--mine", "--exact-lease-times"},
}
SWITCHES = {"--mine", "--exact-lease-times", "--help"}
RESOURCE_OPTIONS = {
    "list": {"--directory", "--node", "--model", "--search", "--limit", "--cursor", "--json"},
    "view": {"--json"},
    "devices": {"--model", "--limit", "--cursor", "--json"},
}
RESOURCE_SWITCHES = {"--mine", "--with-metrics", "--exact-lease-times", "--help"}
PROTOCOL_SWITCHES = SWITCHES | {
    "--authorized",
    "--whole-node",
    "--same-devices-on-all-nodes",
    "--full",
    "--with-metrics",
    "--clear",
}


def enabled(case):
    return case.get("fixture_family") == "onboarding" and case.get("fixture_version") in {7, 8}


def public_routing(fixture, args, known_paths):
    """New prepared-case policy; archived cases keep their original HTTP behavior."""
    if fixture.case.get("http_policy_version", 1) < 2:
        return None
    url = urlsplit(args["url"])
    if f"{url.scheme}://{url.netloc}" != fixture.origin or url.query or url.fragment:
        return {"status": 404, "error": "No such fixture route"}
    if url.path == "/":
        if args.get("method", "GET").upper() not in {"GET", "HEAD"}:
            return {"status": 405, "error": "Method not allowed"}
        return {
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "body": "<!doctype html><html><head><title>LockBot</title></head><body>LockBot</body></html>",
        }
    if url.path not in known_paths:
        return {"status": 404, "error": "No such fixture route"}
    return None


class PackagedCliHost:
    def __init__(self, fixture, *, artifact=None, options=None):
        self.artifact = artifact if artifact is not None else fixture.frozen
        self.options = OPTIONS if options is None else options
        self.argv_protocol = fixture.case.get("cli_argv_protocol") in {1, 2, 3}
        if self.argv_protocol:
            self.options = {**self.options, "resource": set()}
        self.fixture = fixture
        self.workspace = fixture.workspace
        self.user = self.workspace / "client-user"
        self.skills = self.user / "skills"
        self.private = self.user / "lockbot"

    def tool_descriptors(self):
        return [
            {
                "name": "discover_local_state",
                "description": (
                    "Inspect the synthetic user Skill catalog and private file names; never reads native host state."
                ),
                "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
            {
                "name": "run_lockbot",
                "description": "Run only the verified installed packaged CLI with its script path and argv array. "
                "Supports help and these offline fixture actions: " + ", ".join(self.options) + ". "
                "All HTTP and file paths stay inside the synthetic fixture; no shell or arbitrary Python.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "script": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}, "maxItems": 48},
                    },
                    "required": ["script", "argv"],
                    "additionalProperties": False,
                },
            },
        ]

    def path(self, name):
        if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
            raise ValueError("Invalid workspace path")
        value = PurePosixPath(name)
        if ".." in value.parts or str(value) == ".":
            raise ValueError("Path escapes workspace")
        if value.is_absolute():
            if not value.is_relative_to(USER_ROOT):
                raise ValueError("Only the synthetic user namespace is supported")
            value = value.relative_to(USER_ROOT)
            path = self.user.joinpath(*value.parts)
        else:
            path = self.workspace.joinpath(*value.parts)
        # Reject symlinks even when their current target remains inside the workspace.
        if not path.absolute().is_relative_to(self.workspace) or path.resolve() != path.absolute():
            raise ValueError("Path escapes workspace or contains a symlink")
        return path

    def virtual(self, value):
        if isinstance(value, dict):
            return {key: self.virtual(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.virtual(item) for item in value]
        if isinstance(value, str):
            for physical, logical in ((self.user, USER_ROOT), (self.workspace, PurePosixPath("."))):
                if value == str(physical) or value.startswith(str(physical) + "/"):
                    return str(logical / Path(value).relative_to(physical))
            for physical, logical in (
                (self.user, USER_ROOT),
                (self.workspace, "/synthetic-workspace"),
                (self.fixture.root, "/synthetic-fixture"),
            ):
                value = value.replace(str(physical), str(logical))
        return value

    def verified(self, destination):
        if self.artifact is None or destination.parent != self.skills:
            return False
        try:
            return all(
                self.path(str(SKILL_ROOT / destination.name / name)).read_bytes() == content
                for name, content in self.artifact["contents"].items()
            )
        except (OSError, ValueError):
            return False

    def inventory(self):
        self.path(str(SKILL_ROOT))
        self.path("private")
        self.path(str(LOCKBOT_ROOT))
        installed = []
        for directory in sorted(self.skills.iterdir()) if self.skills.is_dir() else []:
            if self.verified(directory):
                installed.append(
                    {
                        "path": str(SKILL_ROOT / directory.name),
                        "skill_file": str(SKILL_ROOT / directory.name / "SKILL.md"),
                        "helper_path": str(SKILL_ROOT / directory.name / "scripts/lockbot_cli.py"),
                    }
                )
        return {
            "supported_skill_catalogs": [{"path": str(SKILL_ROOT), "scope": "user", "synthetic": True}],
            "installed_skills": installed,
            "private_files": sorted(
                self.virtual(str(path))
                for root in (self.workspace / "private", self.private)
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
            "native_global_discovery_verified": False,
        }

    def arguments(self, supplied, execution):
        if not isinstance(supplied, list) or not supplied or len(supplied) > 48:
            raise ValueError("Use a nonempty bounded CLI argv array")
        if not all(isinstance(value, str) and len(value) <= 4096 and "\0" not in value for value in supplied):
            raise ValueError("Invalid CLI argument")
        result, action, resource_action, reference = [], None, None, None
        index = 0
        while index < len(supplied):
            item = supplied[index]
            option, equals, inline = item.partition("=")
            if not item.startswith("-"):
                if action == "resource" and resource_action is None:
                    if item not in RESOURCE_OPTIONS:
                        raise ValueError("Unsupported fixture resource action")
                    resource_action = item
                    execution["cli_resource_action"] = item
                    result.append(item)
                    index += 1
                    continue
                if action == "resource" and resource_action in {"view", "devices"} and reference is None:
                    reference = item
                    execution["cli_resource_ref"] = item
                    result.append(item)
                    index += 1
                    continue
                if action is not None:
                    raise ValueError("Unexpected positional CLI argument")
                action = item
                execution["cli_action"] = action
                if action not in self.options:
                    raise ValueError("Unsupported fixture CLI action")
                result.append(action)
                index += 1
                continue
            allowed = {"--connection"} if action is None else self.options[action]
            if action == "resource":
                if resource_action is None:
                    raise ValueError("Resource subcommand required")
                allowed = RESOURCE_OPTIONS[resource_action]
            switches = (
                RESOURCE_SWITCHES if action == "resource" else PROTOCOL_SWITCHES if self.argv_protocol else SWITCHES
            )
            if option in switches and (action is not None or option == "--help") and not equals:
                result.append(option)
                index += 1
                continue
            if option not in allowed:
                raise ValueError("Unsupported fixture CLI option")
            if (
                action == "resource"
                and option == "--json"
                and not equals
                and (index + 1 == len(supplied) or supplied[index + 1].startswith("-"))
            ):
                result.append(option)
                index += 1
                continue
            if equals:
                value = inline
            else:
                index += 1
                if index >= len(supplied):
                    raise ValueError("CLI option requires a value")
                value = supplied[index]
            if option in PATH_OPTIONS:
                value = str(self.path(value))
            if option == "--scope" and value not in {"user", "project"}:
                raise ValueError("Invalid connection scope")
            result.extend((option, value))
            index += 1
        if action is None and supplied != ["--help"]:
            raise ValueError("CLI action required")
        if action == "resource" and (
            resource_action is None or resource_action in {"view", "devices"} and reference is None
        ):
            raise ValueError("Resource subcommand/reference required")
        return result

    def validate_connection_files(self, argv, action):
        selected = None
        directory = self.private / "connections"
        for option, value in zip(argv, argv[1:], strict=False):
            if option == "--connection":
                selected = Path(value)
            elif option == "--directory":
                directory = Path(value)
        paths = [selected] if selected is not None else []
        if action in {"connections", "resources-all", "resource"} and directory.is_dir():
            # Catalog traversal in the real CLI is bounded and does not follow directory symlinks.
            if directory.resolve() != directory:
                raise ValueError("Connection catalog contains a symlink")
            paths.extend(directory.rglob("connection.json"))
        if len(paths) > 256:
            raise ValueError("Fixture connection inspection limit exceeded")
        for path in paths:
            if not path.is_relative_to(self.workspace) or path.resolve() != path:
                raise ValueError("Connection path escapes workspace or contains a symlink")
            if not path.exists():
                continue  # Let the actual CLI report its normal missing-file error.
            if path.stat().st_size > 16384:
                raise ValueError("Connection file exceeds the fixture limit")
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError("Invalid connection JSON")
            for field in ("key_file", "metrics_key_file"):
                if field not in value:
                    continue
                if not isinstance(value[field], str):
                    raise ValueError("Invalid private-file reference")
                referenced = Path(value[field])
                referenced = referenced if referenced.is_absolute() else path.parent / referenced
                referenced = Path(os.path.abspath(referenced))
                if not referenced.is_relative_to(self.workspace) or referenced.resolve() != referenced:
                    raise ValueError("Connection private-file reference escapes workspace or contains a symlink")

    def open(self, request, timeout):
        # The only opener given to the packaged CLI is the synthetic fixture.
        args = {"url": request.full_url, "method": request.method, "headers": dict(request.header_items())}
        if request.data is not None:
            args["body"] = request.data.decode("utf-8")
        result = self.fixture.http(args)
        self.fixture.record("http_request", args, result, source="packaged_cli")
        if "download_file" in result:
            content = self.path(result["download_file"]).read_bytes()
        else:
            content = json.dumps(result.get("body", {})).encode()
        response = io.BytesIO(content)
        response.headers = {}
        if result.get("status", 500) >= 400:
            raise urllib.error.HTTPError(request.full_url, result["status"], "fixture refused", {}, response)
        return response

    def invoke(self, args, execution):
        script = self.path(args["script"])
        installation = script.parent.parent
        if script.name != "lockbot_cli.py" or script.parent.name != "scripts" or not self.verified(installation):
            raise ValueError("Verified installed packaged CLI required")
        argv = self.arguments(args["argv"], execution)
        self.path(str(LOCKBOT_ROOT)).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.validate_connection_files(argv, execution.get("cli_action"))
        execution["argv"] = [str(script), *argv]
        stdout, stderr = io.StringIO(), io.StringIO()
        # Only the documented LockBot storage override changes. Never change HOME/CODEX_HOME.
        previous = Path.cwd()
        with (
            patch("urllib.request.build_opener", return_value=self),
            patch("sys.argv", [str(script), *argv]),
            patch.dict(os.environ, {"LOCKBOT_HOME": str(self.private)}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                os.chdir(self.workspace)
                namespace = runpy.run_path(str(script))
                try:
                    code = namespace["main"](argv)
                except SystemExit as error:
                    code = error.code
            finally:
                os.chdir(previous)
        execution["stdout"], execution["stderr"] = stdout.getvalue(), stderr.getvalue()
        content = stdout.getvalue() if code == 0 else stderr.getvalue()
        try:
            value = json.loads(content)
        except ValueError:
            return {
                "exit_code": code,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                **({"error": "packaged_cli_nonzero_exit"} if code else {}),
            }
        return {"exit_code": code, **value}
