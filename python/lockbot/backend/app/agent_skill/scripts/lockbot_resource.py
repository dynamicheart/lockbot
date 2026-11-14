#!/usr/bin/env python3
"""Standard-library, scoped Lockbot client. No resource key belongs in argv or output."""

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SERVICE = "lockbot.resource-api"
API_VERSION = "1"
SCRIPT_PATH = "scripts/lockbot_resource.py"
MAX_BYTES = 1024 * 1024
MAX_CALLS = 12
METRICS = (
    "lockbot_accelerator_utilization_percent",
    "lockbot_accelerator_memory_used_bytes",
    "lockbot_accelerator_memory_total_bytes",
    "lockbot_host_cpu_utilization_percent",
    "lockbot_host_memory_used_bytes",
    "lockbot_host_memory_total_bytes",
    "lockbot_host_memory_available_bytes",
    "lockbot_host_memory_cache_bytes",
    "lockbot_host_filesystem_used_bytes",
    "lockbot_host_filesystem_total_bytes",
    "lockbot_sample_timestamp_seconds",
    "lockbot_collector_success",
)


class ClientError(Exception):
    def __init__(self, code, status=None, allowed_categories=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.allowed_categories = allowed_categories


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ClientError("redirect_refused")


def origin(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or "\\" in value
            or any(ord(char) < 33 for char in value)
            or parsed.port == 0
        ):
            raise ValueError
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        port = parsed.port
        suffix = f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, TypeError, AttributeError) as exc:
        raise ClientError("invalid_approved_origin") from exc


def same_origin_url(base, link):
    if not isinstance(link, str) or "\\" in link or any(ord(char) < 33 for char in link):
        raise ClientError("invalid_link")
    target = urllib.parse.urljoin(base + "/", link)
    parsed = urllib.parse.urlsplit(target)
    if origin(f"{parsed.scheme}://{parsed.netloc}") != base or parsed.query or parsed.fragment:
        raise ClientError("cross_origin_link_refused")
    return target


def private_bytes(path):
    path = Path(path)
    try:
        # Do not follow a swapped/symlinked credential file.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
                raise ClientError("private_file_permissions_or_size")
            return stream.read(16385)
    except OSError as exc:
        raise ClientError("private_file_unavailable") from exc


def private_json(path):
    try:
        value = json.loads(private_bytes(path))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ClientError("invalid_private_json") from exc


def installed_version(root):
    try:
        stamp = json.loads((root / "installation.json").read_bytes())
        if stamp["service_id"] != SERVICE or stamp["api_version"] != API_VERSION:
            raise ValueError
        for relative in ("SKILL.md", SCRIPT_PATH):
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != stamp["files"][relative]:
                raise ValueError
        if not re.fullmatch(r"[a-f0-9]{16}", stamp["version"]):
            raise ValueError
        content_version = hashlib.sha256(
            (root / "SKILL.md").read_bytes() + b"\0" + (root / SCRIPT_PATH).read_bytes()
        ).hexdigest()[:16]
        if content_version != stamp["version"]:
            raise ValueError
        return stamp["version"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ClientError("installed_bundle_incomplete_or_modified") from exc


class Client:
    def __init__(
        self, connection=None, *, approved_origin=None, root=None, opener=None, clock=time.monotonic, sleep=time.sleep
    ):
        self.config = private_json(connection) if connection else {}
        self.base = origin(approved_origin or self.config.get("approved_origin"))
        if connection and approved_origin and origin(self.config.get("approved_origin")) != self.base:
            raise ClientError("connection_origin_mismatch")
        self.connection_dir = Path(connection).resolve().parent if connection else None
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + 60
        self.calls = 0
        self.secrets = []
        self.key = None
        self.version = None
        self.needs_update = False
        self.manifest = None

    def _file(self, name):
        value = self.config.get(name)
        if not isinstance(value, str) or not value:
            raise ClientError("missing_" + name)
        path = Path(value)
        return path if path.is_absolute() else self.connection_dir / path

    def _request(self, url, *, token=None, method="GET", body=None, readonly=True, form=False, binary=False):
        for attempt in range(2 if readonly else 1):
            if self.calls >= MAX_CALLS or self.clock() >= self.deadline:
                raise ClientError("request_budget_exhausted")
            self.calls += 1
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            if self.version:
                headers["X-Lockbot-Client-Skill-Version"] = self.version
                headers["X-Lockbot-Client-API-Version"] = API_VERSION
            data = None
            if body is not None:
                data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
                if len(data) > 16384:
                    raise ClientError("request_too_large")
                headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=min(10, self.deadline - self.clock())) as response:
                    api = response.headers.get("X-Lockbot-API-Version")
                    if (api and api != API_VERSION) or response.headers.get(
                        "X-Lockbot-API-Compatibility"
                    ) == "incompatible":
                        raise ClientError("api_version_changed")
                    revision = response.headers.get("X-Lockbot-Skill-Version")
                    if revision and self.version and revision != self.version:
                        self.needs_update = True
                    chunks, size = [], 0
                    reader = getattr(response, "read1", response.read)
                    while True:
                        if self.clock() >= self.deadline:
                            raise ClientError("request_budget_exhausted")
                        chunk = reader(min(65536, MAX_BYTES + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ClientError("response_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    if binary:
                        return raw
                    try:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError
                        current = (value.get("skill_update") or {}).get("current_version")
                        if self.version and current and current != self.version:
                            self.needs_update = True
                        return value
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise ClientError("invalid_json_response") from exc
            except urllib.error.HTTPError as exc:
                retry = exc.headers.get("Retry-After", "")
                status = exc.code
                exc.close()
                if status == 429 and readonly and attempt == 0:
                    delay = self._retry_after(retry)
                    if delay <= 30 and self.clock() + delay < self.deadline:
                        self.sleep(delay)
                        continue
                raise ClientError(
                    "mutation_outcome_unknown" if not readonly and status >= 500 else f"http_{status}", status
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ClientError("request_failed" if readonly else "mutation_outcome_unknown") from exc
        raise ClientError("request_budget_exhausted")

    @staticmethod
    def _retry_after(value):
        try:
            if value.isdecimal():
                return int(value)
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return max(0, instant.timestamp() - time.time())
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 31  # No guessed retry interval or retry storm.

    def discover(self):
        result = self._request(self.base + "/api/agent-skill")
        if (
            result.get("service_id") != SERVICE
            or result.get("api_version") != API_VERSION
            or result.get("skill_id") != "lockbot-resource-agent"
            or not isinstance(result.get("instance_id"), str)
            or not result["instance_id"]
            or not re.fullmatch(r"[a-f0-9]{16}", str(result.get("version", "")))
        ):
            raise ClientError("unsupported_service")
        pinned = self.config.get("instance_id")
        if pinned is not None and pinned != result["instance_id"]:
            raise ClientError("instance_mismatch")
        self.manifest = result
        return result

    def _read_context(self, *, initializing=False):
        if self.manifest is None:
            self.discover()  # no credentials have been read or sent yet
        if not self.config.get("instance_id") or (not initializing and not self.config.get("bot_uuid")):
            raise ClientError("connection_identity_not_pinned")
        if self.version is None:
            self.version = installed_version(self.root)
        self.needs_update |= self.version != self.manifest.get("version")
        if self.key is None:
            try:
                self.key = private_bytes(self._file("key_file")).decode().strip()
            except UnicodeError as exc:
                raise ClientError("invalid_resource_key") from exc
            if not re.fullmatch(r"lbr_[A-Za-z0-9_-]{20,200}", self.key):
                raise ClientError("invalid_resource_key")
            self.secrets.append(self.key)
        result = self._request(same_origin_url(self.base, self.manifest["context_url"]), token=self.key)
        server = result.get("server", {})
        uuid = result.get("bot", {}).get("uuid")
        if (
            server.get("service_id") != SERVICE
            or server.get("api_version") != API_VERSION
            or server.get("instance_id") != self.config["instance_id"]
            or not isinstance(uuid, str)
            or not uuid
            or (not initializing and uuid != self.config["bot_uuid"])
        ):
            raise ClientError("context_identity_mismatch")
        self.needs_update |= result.get("skill", {}).get("version") != self.version
        return result

    def context(self):
        return self._read_context()

    def initialize(self, key_file, destination, metrics_origin=None):
        if self.config or self.connection_dir is not None:
            raise ClientError("init_requires_new_connection")
        manifest = self.discover()
        self.config = {
            "approved_origin": self.base,
            "instance_id": manifest["instance_id"],
            "key_file": str(Path(key_file).absolute()),
        }
        context = self._read_context(initializing=True)
        self.config["bot_uuid"] = context["bot"]["uuid"]
        if metrics_origin is not None:
            self.config["approved_metrics_origin"] = origin(metrics_origin)
        path = Path(destination)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.config, stream)
        return {
            "connection_created": True,
            "bot": context["bot"],
            "actor": context["actor"],
            "key_unchanged": True,
            "update_required": self.needs_update,
        }

    def resources(self, context):
        return self._request(same_origin_url(self.base, context["links"]["resources"]), token=self.key)

    def command(self, operation, nodes, devices, duration, metadata=None):
        context = self.context()
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        if not context.get("agent_access_enabled"):
            raise ClientError("agent_access_disabled")
        if operation not in {"lock", "slock", "book", "renew", "release"}:
            raise ClientError("unsupported_operation")
        if not nodes or len(nodes) > 64 or len(set(nodes)) != len(nodes):
            raise ClientError("explicit_unique_nodes_required")
        if any(not re.fullmatch(r"[^\s,\x00-\x1f\x7f]+", node) for node in nodes):
            raise ClientError("invalid_node")
        card_bot = context["bot"]["type"] in {"DEVICE", "QUEUE_DEVICE"}
        if card_bot != bool(devices) or len(set(devices)) != len(devices):
            raise ClientError("explicit_device_selection_required")
        if any(not re.fullmatch(r"0|[1-9][0-9]{0,4}", device) for device in devices):
            raise ClientError("invalid_device")
        if operation != "release" and (
            not duration or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?)[smhd]", duration) or float(duration[:-1]) <= 0
        ):
            raise ClientError("explicit_positive_duration_required")
        if operation == "release" and duration is not None:
            raise ClientError("release_has_no_duration")
        metadata = metadata or {}
        if set(metadata) - {"task_category", "purpose", "agent_label"}:
            raise ClientError("unsupported_metadata")
        categories = context.get("usage_privacy", {}).get("public_task_categories", {})
        category = metadata.get("task_category")
        if category is not None and (not isinstance(category, str) or category not in categories):
            allowed = {
                key: label
                for key, label in categories.items()
                if isinstance(key, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                and isinstance(label, str)
                and len(label) <= 128
            }
            raise ClientError("invalid_task_category_select_from_context", allowed_categories=allowed)
        for name, limit in (("purpose", 512), ("agent_label", 64)):
            value = metadata.get(name)
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= limit or re.search(r"[\x00-\x1f\x7f]", value)
            ):
                raise ClientError("invalid_" + name)
        if any(isinstance(value, str) and self.key in value for value in metadata.values()):
            raise ClientError("secret_in_metadata_refused")
        before = self.resources(context)
        selected = self._selected(before, nodes, devices)
        actor = context["actor"]["external_user_id"]
        held = [self._own(item.get("holders", []), actor) for item in selected]
        if operation == "renew" and (not all(held) or context.get("bot_rules", {}).get("forbid_relock")):
            raise ClientError("renewal_not_allowed_or_not_owned")
        if operation in {"lock", "slock"} and any(held):
            raise ClientError("already_owned_use_explicit_renew")
        if operation == "book" and (
            any(held) or any(self._own(item.get("booking_list", []), actor) for item in selected)
        ):
            raise ClientError("already_owned_or_booked_read_state_do_not_rebook")
        wire = {"renew": "lock", "release": "unlock"}.get(operation, operation)
        if operation == "renew":
            modes = {item.get("lock_status") for item in selected}
            if modes == {"shared"}:
                wire = "slock"
            elif modes != {"exclusive"}:
                raise ClientError("mixed_or_unknown_renewal_modes")
        if wire not in before.get("supported_operations", []):
            raise ClientError("operation_not_supported_by_bot")
        command = wire + " " + ",".join(nodes)
        if devices:
            command += " dev" + ",".join(devices)
        if duration:
            command += " " + duration
        if len(command) > 512:
            raise ClientError("command_too_large")
        # Header updates learned during the pre-read must block the subsequent write.
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        try:
            result = self._request(
                same_origin_url(self.base, context["links"]["commands"]),
                token=self.key,
                method="POST",
                body={"command": command, **metadata},
                readonly=False,
            )
        except ClientError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                raise
            raise ClientError("mutation_outcome_unknown_read_resources_do_not_replay") from exc
        try:
            after = self.resources(context)
            targets = self._selected(after, nodes, devices)
            if operation == "release":
                verified = all(
                    not self._own(item.get("holders", []) + item.get("booking_list", []), actor) for item in targets
                )
            elif operation == "book":
                verified = all(self._own(item.get("booking_list", []), actor) for item in targets)
            else:
                fresh = [self._own(item.get("holders", []), actor) for item in targets]
                sampled_at = self._expiry({"expires_at": after["captured_at"]})
                age = float(after.get("age_seconds", 0))
                verified = (
                    all(fresh)
                    and age >= 0
                    and all(
                        float(item.get("remaining_seconds", 0)) > age and self._expiry(item) > sampled_at + age
                        for item in fresh
                    )
                )
                if operation == "renew":
                    verified = verified and all(
                        self._expiry(new) > self._expiry(old) for old, new in zip(held, fresh, strict=True)
                    )
            if not verified:
                raise ClientError("mutation_not_confirmed_read_resources_do_not_replay")
        except (ClientError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ClientError("mutation_not_confirmed_read_resources_do_not_replay") from exc
        return {
            "command_result": result,
            "verified_snapshot": after,
            "verified": True,
            "verification": "observed_state_only",
            "command_outcome": "not_structured_by_server",
            "update_required": self.needs_update,
            "job_runner": False,
        }

    @staticmethod
    def _own(rows, actor):
        return next((row for row in rows if row.get("external_user_id") == actor), None)

    @staticmethod
    def _expiry(row):
        value = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ClientError("expiry_offset_required")
        return value.timestamp()

    @staticmethod
    def _selected(snapshot, nodes, devices):
        indexed = {str(row["id"]): row for row in snapshot.get("resources", [])}
        result = []
        for node in nodes:
            if node not in indexed:
                raise ClientError("unknown_resource")
            item = indexed[node]
            if devices:
                cards = {str(card["id"]): card for card in item.get("devices", [])}
                if set(devices) - set(cards):
                    raise ClientError("unknown_device")
                result.extend(cards[device] for device in devices)
            else:
                result.append(item)
        return result

    def metrics(self, nodes):
        context = self.context()
        for attempt in range(2):
            source = (context.get("utilization") or {}).get("configuration") or {}
            if source.get("type") != "prometheus" or source.get("profile") != "lockbot-v1":
                raise ClientError("unsupported_metrics_profile")
            endpoint = source.get("endpoint", "").rstrip("/")
            parsed = urllib.parse.urlsplit(endpoint)
            target_origin = origin(f"{parsed.scheme}://{parsed.netloc}")
            approved = self.config.get("approved_metrics_origin")
            if not approved or origin(approved) != target_origin:
                raise ClientError("metrics_origin_requires_approval")
            url = same_origin_url(target_origin, endpoint + "/api/v1/query")
            mapping = source.get("nodeMapping", {})
            if not nodes or len(nodes) > 64 or set(nodes) - set(mapping):
                raise ClientError("explicit_mapped_metrics_nodes_required")
            targets = sorted(set(mapping[node] for node in nodes))
            if any(not isinstance(node, str) or not node or len(node) > 256 for node in targets):
                raise ClientError("invalid_metrics_mapping")
            if source.get("accessMode") == "short_lived":
                access = (context.get("utilization") or {}).get("access") or {}
                token = access.get("token")
                if access.get("scope") != "metrics:read" or not isinstance(token, str) or not token:
                    raise ClientError("metrics_grant_unavailable")
            elif source.get("accessMode") == "independent":
                token = private_bytes(self._file("metrics_key_file")).decode().strip()
            else:
                raise ClientError("unsupported_metrics_authentication")
            if token.startswith(("lbr_", "lbk_")) or token == self.key:
                raise ClientError("resource_key_for_metrics_refused")
            self.secrets.append(token)
            selector = "{__name__=~" + json.dumps("^(?:" + "|".join(METRICS) + ")$")
            selector += (
                ",node=~"
                + json.dumps(
                    "^(?:" + "|".join(re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", node) for node in targets) + ")$"
                )
                + "}"
            )
            try:
                data = self._request(
                    url,
                    token=token,
                    method="POST",
                    form=True,
                    body={"query": selector, "time": str(time.time()), "timeout": "4s"},
                )
                vector = data.get("data", {}).get("result")
                if (
                    data.get("status") != "success"
                    or data.get("data", {}).get("resultType") != "vector"
                    or not isinstance(vector, list)
                    or len(vector) > 2048
                ):
                    raise ClientError("invalid_metrics_response")
                for row in vector:
                    labels = row.get("metric", {})
                    if labels.get("node") not in targets or labels.get("__name__") not in METRICS:
                        raise ClientError("metrics_scope_mismatch")
                return {
                    "best_effort": True,
                    "verification": "query_response_only",
                    "data": data,
                    "does_not_authorize_resources": True,
                }
            except ClientError as exc:
                if exc.status not in (401, 403) or attempt or source.get("accessMode") != "short_lived":
                    raise
                context = self.context()  # refresh a short grant once; never send the resource key to the source
        raise ClientError("metrics_authentication_failed")

    def redacted(self, value):
        if isinstance(value, dict):
            return {
                self.redacted(key): (
                    "[redacted]"
                    if key.lower() in {"credential", "token", "secret", "api_key", "authorization"}
                    else self.redacted(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redacted(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"\b(?:lbr_|lbk_)[A-Za-z0-9_-]{20,200}", "[redacted]", value)
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
        return value

    def install(self, destination):
        meta = self.discover()
        archive = self._request(same_origin_url(self.base, meta["installation"]["download_url"]), binary=True)
        if hashlib.sha256(archive).hexdigest() != meta["installation"]["sha256"]:
            raise ClientError("bundle_digest_mismatch")
        target = Path(destination)
        if target.exists():
            raise ClientError("install_destination_must_be_new")
        expected = {"SKILL.md", SCRIPT_PATH, "installation.json"}
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                root = meta["installation"]["name"] + "/"
                if set(zipped.namelist()) != {root + item for item in expected}:
                    raise ClientError("unsafe_or_incomplete_bundle")
                entries = zipped.infolist()
                if len(entries) != len(expected) or sum(entry.file_size for entry in entries) > MAX_BYTES:
                    raise ClientError("bundle_too_large_or_duplicate")
                if any(stat.S_ISLNK(entry.external_attr >> 16) for entry in entries):
                    raise ClientError("bundle_symlink_refused")
                contents = {name: zipped.read(root + name) for name in expected}
                if sum(map(len, contents.values())) > MAX_BYTES:
                    raise ClientError("bundle_too_large")
                stamp = json.loads(contents["installation.json"])
                if (
                    stamp["version"] != meta["version"]
                    or hashlib.sha256(contents["SKILL.md"]).hexdigest() != meta["skill_sha256"]
                ):
                    raise ClientError("bundle_metadata_mismatch")
                for name in ("SKILL.md", SCRIPT_PATH):
                    if hashlib.sha256(contents[name]).hexdigest() != stamp["files"][name]:
                        raise ClientError("bundle_file_digest_mismatch")
        except (ValueError, OSError, KeyError, zipfile.BadZipFile, RecursionError) as exc:
            raise ClientError("invalid_installation_bundle") from exc
        target.mkdir(parents=True, mode=0o700)
        for name, content in contents.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return {
            "installed_to": str(target),
            "version": meta["version"],
            "client_reload_required": True,
            "connection_and_enrollment_created": False,
        }


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid argv; a mistakenly pasted key must not leak.
        self.exit(2, '{"error":"invalid_cli_arguments","retry_mutation":false}\n')


def runtime_compatible():
    return sys.version_info >= (3, 10)


def main(argv=None):
    if not runtime_compatible():
        print(json.dumps({"error": "helper_requires_python_3_10_or_newer", "retry_mutation": False}), file=sys.stderr)
        return 2
    parser = SafeParser(description=__doc__)
    parser.add_argument("--connection", help="Private mode0600 scoped connection JSON; never a raw key")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("discover", "install", "init"):
        command = sub.add_parser(action)
        command.add_argument("--origin", required=True, help="Explicitly user-approved service origin")
        if action in {"install", "init"}:
            command.add_argument("--destination", required=True)
        if action == "init":
            command.add_argument("--key-file", required=True, help="Existing private personal-key file, not a raw key")
            command.add_argument("--metrics-origin", help="Separately approved metrics origin, optional")
    sub.add_parser("context")
    sub.add_parser("resources")
    command = sub.add_parser("command")
    command.add_argument("--operation", required=True, choices=["lock", "slock", "book", "renew", "release"])
    command.add_argument("--node", action="append", required=True)
    command.add_argument("--device", action="append", default=[])
    command.add_argument("--duration")
    command.add_argument("--metadata-file", help="Private JSON: task_category, purpose, agent_label only")
    command = sub.add_parser("metrics")
    command.add_argument("--node", action="append", required=True)
    args = parser.parse_args(argv)
    client = None
    try:
        if args.action not in {"discover", "install", "init"} and not args.connection:
            raise ClientError("private_connection_required")
        client = Client(args.connection, approved_origin=getattr(args, "origin", None))
        if args.action == "discover":
            result = client.discover()
        elif args.action == "install":
            result = client.install(args.destination)
        elif args.action == "init":
            result = client.initialize(args.key_file, args.destination, args.metrics_origin)
        elif args.action == "context":
            result = client.context()
        elif args.action == "resources":
            result = client.resources(client.context())
        elif args.action == "command":
            metadata = private_json(args.metadata_file) if args.metadata_file else {}
            result = client.command(args.operation, args.node, args.device, args.duration, metadata)
        else:
            result = client.metrics(args.node)
        print(
            json.dumps(client.redacted({"result": result, "update_required": client.needs_update}), ensure_ascii=False)
        )
        return 0
    except ClientError as exc:
        output = {"error": exc.code, "retry_mutation": False}
        if exc.allowed_categories is not None:
            output["allowed_categories"] = exc.allowed_categories
        print(json.dumps(client.redacted(output) if client else output), file=sys.stderr)
        return 2
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OSError):
        print(json.dumps({"error": "invalid_server_or_local_data", "retry_mutation": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
