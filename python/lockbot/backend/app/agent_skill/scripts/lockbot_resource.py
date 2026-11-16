#!/usr/bin/env python3
"""Standard-library, scoped Lockbot client. No resource key belongs in argv or output."""

import argparse
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SERVICE = "lockbot.resource-api"
API_VERSION = "1"
SCRIPT_PATH = "scripts/lockbot_resource.py"
BUNDLE_FORMAT = "canonical-origin-v2"
LEGACY_BUNDLE_FORMAT = "canonical-origin-v1"
REFERENCE_PATHS = (
    "references/connections.md",
    "references/resource-operations.md",
    "references/workbench-and-metrics.md",
)
ENROLLMENT_PATH = "/api/resource-auth/bindings"
CREDENTIAL_CHALLENGE_PATH = "/api/resource-auth/credential-challenges"
RECOVERY_PATH = "/api/agent/bindings/recover"
MAX_BYTES = 1024 * 1024
MAX_CALLS = 12
MAX_RESOURCE_SNAPSHOT_AGE = 5.0
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


def metric_number(value):
    if type(value) not in (str, int, float):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def utilization_sample(values, success, stamps, now, stale_seconds):
    """Classify a utilization reading using its collector's own timestamp."""
    stamp = stamps[0] if len(stamps) == 1 else None
    age = max(0, now - stamp) if stamp is not None and 0 < stamp <= now + 30 else None
    value = values[0] if len(values) == 1 else None
    if success == [0]:
        status = "failed"
    elif (
        len(success) > 1
        or (success and success != [1])
        or len(stamps) > 1
        or (stamps and age is None)
        or len(values) > 1
        or (values and (value is None or not 0 <= value <= 100))
    ):
        status = "invalid"
    elif not success or age is None or not values:
        status = "missing"
    elif age > stale_seconds:
        status = "stale"
    else:
        status = "ok"
    return {"status": status, "percent": value if status == "ok" else None, "sample_age_seconds": age}


def utilization_summary(vector, mappings, now, stale_seconds):
    collectors = {}
    samples = {}
    devices = {}
    for row in vector:
        labels = row["metric"]
        name, node = labels["__name__"], labels["node"]
        pair = row.get("value")
        value = metric_number(pair[1]) if isinstance(pair, list) and len(pair) == 2 else None
        if name in ("lockbot_sample_timestamp_seconds", "lockbot_collector_success"):
            collector = labels.get("collector")
            if collector in ("accelerator", "host"):
                collectors.setdefault((node, collector, name), []).append(value)
        elif name == "lockbot_host_cpu_utilization_percent":
            samples.setdefault((node, "host", None), []).append(value)
        elif name.startswith("lockbot_accelerator_"):
            device = labels.get("device")
            if isinstance(device, str) and device:
                devices.setdefault(node, set()).add(device)
                if name == "lockbot_accelerator_utilization_percent":
                    samples.setdefault((node, "accelerator", device), []).append(value)

    def reading(node, collector, device=None):
        return utilization_sample(
            samples.get((node, collector, device), []),
            collectors.get((node, collector, "lockbot_collector_success"), []),
            collectors.get((node, collector, "lockbot_sample_timestamp_seconds"), []),
            now,
            stale_seconds,
        )

    return [
        {
            "node": node,
            "source_node": target,
            "cpu": reading(target, "host"),
            "cards": [
                {"device": device, **reading(target, "accelerator", device)}
                for device in sorted(devices.get(target, ()))
            ],
            "cards_status": "reported" if target in devices else reading(target, "accelerator")["status"],
        }
        for node, target in mappings.items()
    ]


class ClientError(Exception):
    def __init__(self, code, status=None, allowed_categories=None, *, retry_after_seconds=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.allowed_categories = allowed_categories
        self.retry_after_seconds = retry_after_seconds


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
        # Check the opened file without following symlinks or waiting on a pipe.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
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


def connection_catalog(scope):
    if scope == "project":
        return Path(".lockbot/connections")
    return Path.home() / ".config/lockbot/connections"


def new_catalog_entry(scope, name=None):
    root = connection_catalog(scope)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = root.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise ClientError("private_connection_directory_required")
    return root.resolve() / (name or secrets.token_hex(16))


def listed_connections(directory=None):
    """List connections and resumable enrollments without opening their Key files."""
    catalogs = (
        [("custom", Path(directory))]
        if directory is not None
        else [(scope, connection_catalog(scope)) for scope in ("project", "user")]
    )
    connections = []
    enrollments = []
    unavailable_count = 0
    for scope, root in catalogs:
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode) or mode & 0o077:
            raise ClientError("private_connection_directory_required")
        root = root.resolve()
        paths = catalog_records(root, "connection.json")
        completed = set()
        for path in paths:
            try:
                if path.resolve() != path:
                    raise ValueError
                record = private_json(path)
                service = origin(record["approved_origin"])
                instance, bot = record["instance_id"], record["bot_uuid"]
                if any(not isinstance(field, str) or not field for field in (instance, bot, record["key_file"])):
                    raise ValueError
                entry = {"origin": service, "instance_id": instance, "bot_uuid": bot, "connection": str(path)}
                if re.search(r"\b(?:lbr_|lbk_|lbc1\.)", " ".join(entry.values())):
                    raise ValueError
                connections.append({**entry, "scope": scope})
                completed.add(path.parent)
            except (ClientError, OSError, KeyError, RuntimeError, ValueError):
                unavailable_count += 1
        for path in catalog_records(root, "enrollment.json"):
            if path.parent in completed:
                continue
            try:
                if path.resolve() != path:
                    raise ValueError
                enrollments.append({**pending_enrollment_record(path.parent), "scope": scope})
            except (ClientError, OSError, KeyError, RuntimeError, ValueError):
                unavailable_count += 1
    return {"connections": connections, "pending_enrollments": enrollments, "unavailable_count": unavailable_count}


def catalog_records(root, filename):
    return sorted([*root.glob(filename), *root.glob(f"*/{filename}"), *root.glob(f"*/*/{filename}")])


def write_private(path, content, *, replace=False):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            if replace:
                temporary.replace(path)
            else:
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def enrollment_group(value):
    if (
        not isinstance(value, str)
        or not 0 < len(value.strip()) <= 512
        or any(unicodedata.category(char) == "Cc" for char in value)
        or re.search(r"\b(?:lbr_|lbk_)[A-Za-z0-9_-]{20,200}", value)
    ):
        raise ClientError("invalid_enrollment_group")
    return value.strip()


def enrollment_state(directory):
    path = Path(directory).absolute()
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ClientError("private_enrollment_directory_required")
    return path, private_json(path / "enrollment.json")


def enrollment_deadline(value):
    if not isinstance(value, str):
        raise ValueError
    deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if deadline.utcoffset() is None:
        raise ValueError
    return deadline


def pending_enrollment_record(directory):
    path, state = enrollment_state(directory)
    instance = state["instance_id"]
    if not isinstance(instance, str) or not instance:
        raise ValueError
    status = state["status"]
    group = state["target_group"]
    entry = {
        "origin": origin(state["approved_origin"]),
        "instance_id": instance,
        "target_group": enrollment_group(group) if group is not None else None,
        "enrollment_dir": str(path),
        "status": "request_outcome_unknown" if status == "requesting" else status,
    }
    purpose = state.get("purpose", "bind")
    if purpose not in ("bind", "recover", "reset"):
        raise ValueError
    if purpose != "bind":
        entry["purpose"] = purpose
    if status == "pending":
        binding = state["binding_id"]
        if not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", binding):
            raise ValueError
        expiry = state["bind_expires_at"]
        deadline = enrollment_deadline(expiry)
        entry["bind_expires_at"] = expiry
        if purpose != "bind" and deadline <= datetime.now(timezone.utc):
            entry["status"] = "expired"
    elif status != "requesting":
        raise ValueError
    if re.search(r"\b(?:lbr_|lbp_|lbk_|lbc1\.)", " ".join(value for value in entry.values() if value is not None)):
        raise ValueError
    if purpose == "reset" and entry["status"] == "pending":
        entry["status_source"] = "local"
        entry["next_command"] = ["enrollment-status", "--enrollment-dir", str(path)]
        entry["next_step"] = "Check whether the group already applied the reset. This command only reads its status."
    return entry


def _local_hardware_note(note):
    if not isinstance(note, dict) or set(note) != {"source", "accelerators"}:
        return None
    source, inventory = note["source"], note["accelerators"]
    if not isinstance(source, str) or not source.strip() or len(source) > 160 or not source.isprintable():
        return None
    if not isinstance(inventory, list) or not inventory or len(inventory) > 32:
        return None
    models = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) - {"model", "count"}:
            return None
        model = item.get("model")
        if not isinstance(model, str) or not model or model != model.strip() or len(model) > 80:
            return None
        if not model.isprintable() or model.casefold() in models:
            return None
        models.add(model.casefold())
        if "count" in item and (type(item["count"]) is not int or item["count"] < 1):
            return None
    return note


class NodeAccessMetadata:
    """Private sidecar with compare-and-replace for overlapping helper invocations."""

    def __init__(self, connection):
        self.path = connection.with_name(connection.name + ".node-access.json")
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self):
        import fcntl

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise OSError("Private node access lock required")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield lock

    def _replace(self, nodes, scope):
        data = json.dumps({"version": 1, "scope": scope, "nodes": nodes}).encode()
        if len(data) > MAX_BYTES:
            return
        fd, destination = tempfile.mkstemp(prefix=".lockbot-node-access-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(destination, self.path)
        finally:
            Path(destination).unlink(missing_ok=True)

    def invalidate(self):
        generation = secrets.token_hex(16).encode()
        try:
            with self._locked() as lock:
                lock.write(generation)
                lock.truncate()
                self.path.unlink(missing_ok=True)
        except OSError:
            return None  # A read-only connection directory can still run the live client.
        return generation

    def save(self, generation, nodes, scope):
        if not generation or not nodes:
            return
        try:
            with self._locked() as lock:
                if lock.read(len(generation) + 1) == generation:
                    self._replace(nodes, scope)
        except OSError:
            return


def _node_address(value):
    if not isinstance(value, dict):
        return None
    host, port = value.get("host"), value.get("port")
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9.:-]{1,253}", host):
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    address = {"host": host, "port": port}
    if "ip" in value:
        try:
            if not isinstance(value["ip"], str):
                return None
            address["ip"] = str(ipaddress.IPv4Address(value["ip"]))
        except ipaddress.AddressValueError:
            return None
    return address


def deployment_fragments(base, package_format=BUNDLE_FORMAT):
    """Exact builder-owned locator substitutions, never arbitrary Markdown stripping."""
    fragments = {
        "<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->": "\n".join(
            [
                "Deployment API addresses (use the verified context links for the bot scope):",
                f"- Manifest: `GET {base}/api/agent-skill`",
                f"- Context: `GET {base}/api/agent/context`",
                f"- Complete installation ZIP: `{base}/api/agent-skill/lockbot-resource-agent.zip`",
                "- Read resources and send commands using the same-origin links returned by context.",
                "  The personal Key identifies its bot.",
            ]
        ),
    }
    if package_format == LEGACY_BUNDLE_FORMAT:
        fragments["<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->"] = f"Deployment Skill URL: `{base}/api/agent-skill/SKILL.md`."
    return fragments


def content_version(markdown, script, references=None):
    digest = hashlib.sha256(markdown + b"\0" + script)
    for name, content in sorted((references or {}).items()):
        digest.update(b"\0" + name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()[:16]


def canonical_package_version(markdown, script, deployment_origin, references=None):
    package_format = BUNDLE_FORMAT if references else LEGACY_BUNDLE_FORMAT
    if not isinstance(deployment_origin, str):
        raise ClientError("invalid_bundle_deployment_origin")
    if deployment_origin:
        # The published Markdown preserves the configured authority spelling.
        # Validate it without normalizing the bytes used by the renderer.
        origin(deployment_origin)
        if not re.fullmatch(r"https?://[A-Za-z0-9.:[\]-]+", deployment_origin):
            raise ClientError("invalid_bundle_deployment_origin")
        for marker, rendered in deployment_fragments(deployment_origin, package_format).items():
            if markdown.count(rendered.encode()) != 1 or marker.encode() in markdown:
                raise ClientError("unknown_bundle_locator")
            markdown = markdown.replace(rendered.encode(), marker.encode(), 1)
    if any(markdown.count(marker.encode()) != 1 for marker in deployment_fragments("", package_format)):
        raise ClientError("unknown_bundle_locator")
    return content_version(markdown, script, references)


def installed_package_version(root):
    installed_version(root)  # retain full rendered file/bundle integrity checks
    try:
        stamp = json.loads((root / "installation.json").read_bytes())
        if "package_version" not in stamp:  # old complete bundles stay conservative
            return None
        if stamp.get("package_format") not in (LEGACY_BUNDLE_FORMAT, BUNDLE_FORMAT):
            raise ClientError("unsupported_bundle_package_format")
        references = (
            {name: (root / name).read_bytes() for name in REFERENCE_PATHS}
            if (stamp["package_format"] == BUNDLE_FORMAT)
            else None
        )
        package = canonical_package_version(
            (root / "SKILL.md").read_bytes(),
            (root / SCRIPT_PATH).read_bytes(),
            stamp.get("deployment_origin"),
            references,
        )
        if package != stamp["package_version"]:
            raise ClientError("installed_package_modified")
        return package
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ClientError("installed_bundle_incomplete_or_modified") from exc


def installed_version(root):
    try:
        stamp = json.loads((root / "installation.json").read_bytes())
        if stamp["service_id"] != SERVICE or stamp["api_version"] != API_VERSION:
            raise ValueError
        reference_paths = REFERENCE_PATHS if stamp.get("package_format") == BUNDLE_FORMAT else ()
        contents = {
            relative: (root / relative).read_bytes() for relative in ("SKILL.md", SCRIPT_PATH, *reference_paths)
        }
        for relative, data in contents.items():
            if hashlib.sha256(data).hexdigest() != stamp["files"][relative]:
                raise ValueError
        if not re.fullmatch(r"[a-f0-9]{16}", stamp["version"]):
            raise ValueError
        references = {name: contents[name] for name in reference_paths}
        if content_version(contents["SKILL.md"], contents[SCRIPT_PATH], references) != stamp["version"]:
            raise ValueError
        return stamp["version"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ClientError("installed_bundle_incomplete_or_modified") from exc


def normalize_agent_label(value):
    if not isinstance(value, str) or not value.isprintable():
        raise ClientError("invalid_agent_label")
    label = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(label) <= 64 or re.search(r"\b(?:lbr_|lbk_|lb[a-z][0-9]+\.)", label):
        raise ClientError("invalid_agent_label")
    return label


class Client:
    def __init__(
        self, connection=None, *, approved_origin=None, root=None, opener=None, clock=time.monotonic, sleep=time.sleep
    ):
        self.config = private_json(connection) if connection else {}
        self.base = origin(approved_origin or self.config.get("approved_origin"))
        if connection and approved_origin and origin(self.config.get("approved_origin")) != self.base:
            raise ClientError("connection_origin_mismatch")
        self.connection_dir = Path(connection).resolve().parent if connection else None
        self.hardware_notes = (
            Path(connection).with_name(Path(connection).name + ".node-hardware.json") if connection else None
        )
        self.node_access = (
            NodeAccessMetadata(Path(connection).absolute()) if connection and os.name == "posix" else None
        )
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + 60
        self.calls = 0
        self.secrets = []
        self.key = None
        self.version = None
        self.package_version = None
        self.portable_package = False
        self.needs_update = False
        self.manifest = None

    def _file(self, name):
        value = self.config.get(name)
        if not isinstance(value, str) or not value:
            raise ClientError("missing_" + name)
        path = Path(value)
        return path if path.is_absolute() else self.connection_dir / path

    def _request(
        self,
        url,
        *,
        token=None,
        method="GET",
        body=None,
        readonly=True,
        form=False,
        binary=False,
        include_started=False,
    ):
        for attempt in range(2 if readonly else 1):
            if self.calls >= MAX_CALLS or self.clock() >= self.deadline:
                raise ClientError("request_budget_exhausted")
            self.calls += 1
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
                if token == self.key and "agent_label" in self.config:
                    label = normalize_agent_label(self.config["agent_label"])
                    headers["X-Lockbot-Agent-Label"] = urllib.parse.quote(label, safe="")
            if self.version:
                headers["X-Lockbot-Client-Skill-Version"] = self.version
                headers["X-Lockbot-Client-API-Version"] = API_VERSION
                if self.package_version:
                    headers["X-Lockbot-Client-Package-Version"] = self.package_version
            data = None
            if body is not None:
                data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
                if len(data) > 16384:
                    raise ClientError("request_too_large")
                headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            started = self.clock()
            try:
                with self.opener.open(request, timeout=min(10, self.deadline - started)) as response:
                    api = response.headers.get("X-Lockbot-API-Version")
                    if (api and api != API_VERSION) or response.headers.get(
                        "X-Lockbot-API-Compatibility"
                    ) == "incompatible":
                        raise ClientError("api_version_changed")
                    revision = response.headers.get("X-Lockbot-Skill-Version")
                    if (
                        revision
                        and self.version
                        and self._version_differs(revision, response.headers.get("X-Lockbot-Package-Version"))
                    ):
                        self.needs_update = True
                    chunks, size = [], 0
                    reader = getattr(response, "read1", response.read)
                    while True:
                        if self.clock() >= self.deadline:
                            raise ClientError("request_budget_exhausted")
                        chunk = reader(min(65536, MAX_BYTES + 1 - size))
                        if self.clock() >= self.deadline:
                            raise ClientError("request_budget_exhausted")
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
                        if (
                            self.version
                            and current
                            and self._version_differs(
                                current, (value.get("skill_update") or {}).get("current_package_version")
                            )
                        ):
                            self.needs_update = True
                        return (value, started) if include_started else value
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise ClientError("invalid_json_response") from exc
            except urllib.error.HTTPError as exc:
                retry = exc.headers.get("Retry-After", "")
                status = exc.code
                exc.close()
                delay = self._retry_after(retry) if status == 429 else None
                if (
                    readonly
                    and attempt == 0
                    and delay is not None
                    and delay <= 30
                    and self.clock() + delay < self.deadline
                ):
                    self.sleep(delay)
                    continue
                raise ClientError(
                    "mutation_outcome_unknown" if not readonly and status >= 500 else f"http_{status}",
                    status,
                    retry_after_seconds=delay,
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
            return max(0, math.ceil(instant.timestamp() - time.time()))
        except (ValueError, TypeError, AttributeError, OverflowError):
            return None

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

    def _version_differs(self, rendered, package=None):
        if self.portable_package and package is not None:
            return package != self.package_version
        return rendered != self.version

    def _check_installed_version(self):
        if self.version is None:
            self.version = installed_version(self.root)
            self.package_version = installed_package_version(self.root)
        self.portable_package = bool(
            self.package_version
            and self.manifest.get("package_format") in (LEGACY_BUNDLE_FORMAT, BUNDLE_FORMAT)
            and re.fullmatch(r"[a-f0-9]{16}", str(self.manifest.get("package_version", "")))
        )
        self.needs_update |= self._version_differs(self.manifest.get("version"), self.manifest.get("package_version"))

    def _read_context(self, *, initializing=False):
        if self.manifest is None:
            self.discover()  # no credentials have been read or sent yet
        if not self.config.get("instance_id") or (not initializing and not self.config.get("bot_uuid")):
            raise ClientError("connection_identity_not_pinned")
        self._check_installed_version()
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
        actor = result.get("actor")
        if (
            not isinstance(actor, dict)
            or not all(
                isinstance(actor.get(field), str) and 0 < len(actor[field]) <= 512
                for field in ("platform", "external_user_id")
            )
            or (
                not initializing
                and (
                    not isinstance(self.config.get("actor"), dict)
                    or any(
                        actor[field] != self.config["actor"].get(field) for field in ("platform", "external_user_id")
                    )
                )
            )
        ):
            raise ClientError("context_actor_mismatch_reinitialize_connection_without_rotating_key")
        if (
            server.get("service_id") != SERVICE
            or server.get("api_version") != API_VERSION
            or server.get("instance_id") != self.config["instance_id"]
            or not isinstance(uuid, str)
            or not uuid
            or (not initializing and uuid != self.config["bot_uuid"])
        ):
            raise ClientError("context_identity_mismatch")
        self.needs_update |= self._version_differs(
            result.get("skill", {}).get("version"), result.get("skill", {}).get("package_version")
        )
        return result

    def context(self):
        if self.node_access:
            self.node_access.invalidate()
        return self._read_context()

    def initialize(self, key_file, destination, metrics_origin=None, agent_label=None, *, scope="user"):
        if self.config or self.connection_dir is not None:
            raise ClientError("init_requires_new_connection")
        manifest = self.discover()
        self.config = {
            "approved_origin": self.base,
            "instance_id": manifest["instance_id"],
            "key_file": str(Path(key_file).absolute()),
        }
        if agent_label is not None:
            self.config["agent_label"] = normalize_agent_label(agent_label)
        context = self._read_context(initializing=True)
        if metrics_origin is not None:
            self.config["approved_metrics_origin"] = origin(metrics_origin)
        if destination is not None:
            self._save_connection(context, destination)
        else:
            directory = new_catalog_entry(scope)
            directory.mkdir(mode=0o700)
            destination = directory / "connection.json"
            try:
                self._save_connection(context, destination)
            except BaseException:
                with suppress(OSError):
                    directory.rmdir()
                raise
        return {
            "connection_created": True,
            "connection": str(destination),
            "bot": context["bot"],
            "actor": context["actor"],
            "key_unchanged": True,
            "update_required": self.needs_update,
        }

    def _save_connection(self, context, destination):
        self.config["bot_uuid"] = context["bot"]["uuid"]
        self.config["actor"] = {field: context["actor"][field] for field in ("platform", "external_user_id")}
        write_private(destination, json.dumps(self.config).encode())

    def recover(self, directory, *, scope="user"):
        path = Path(directory).absolute() if directory is not None else None
        if path is not None and os.path.lexists(path):
            raise ClientError("recovery_directory_exists")
        context = self.context()
        if "own_binding_recovery" not in self.manifest.get("capabilities", ()):
            raise ClientError("binding_recovery_unavailable")
        endpoint = self.base + RECOVERY_PATH
        if (
            same_origin_url(self.base, self.manifest.get("binding_recovery_url")) != endpoint
            or same_origin_url(self.base, context.get("links", {}).get("binding_recovery")) != endpoint
        ):
            raise ClientError("unsupported_binding_recovery_endpoint")
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        if path is None:
            path = new_catalog_entry(scope)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ClientError("recovery_directory_exists") from exc
        response = self._request(endpoint, method="POST", token=self.key, readonly=False)
        items = self._recovery_items(response, context)
        actor = self.config["actor"]
        connections = []
        for bot, key, current in items:
            entry = {"bot": bot, "status": "existing" if current else "unavailable"}
            if key is not None and not current:
                target = path / bot["uuid"]
                target.mkdir(mode=0o700)
                write_private(target / "key", key.encode())
                connection = {
                    "approved_origin": self.base,
                    "instance_id": self.config["instance_id"],
                    "bot_uuid": bot["uuid"],
                    "actor": actor,
                    "key_file": "key",
                }
                if "agent_label" in self.config:
                    connection["agent_label"] = self.config["agent_label"]
                write_private(target / "connection.json", json.dumps(connection).encode())
                entry.update(status="saved", connection=str(target / "connection.json"))
            connections.append(entry)
        return {"connections": connections, "key_unchanged": True}

    def _recovery_items(self, response, context):
        server, actor, bindings = response.get("server"), response.get("actor"), response.get("bindings")
        if (
            not isinstance(server, dict)
            or server.get("service_id") != SERVICE
            or server.get("api_version") != API_VERSION
            or server.get("instance_id") != self.config["instance_id"]
            or not isinstance(actor, dict)
            or any(actor.get(field) != value for field, value in self.config["actor"].items())
            or not isinstance(bindings, list)
        ):
            raise ClientError("binding_recovery_scope_mismatch")
        records, seen_bots = [], set()
        match_current = False
        for item in bindings:
            bot, key, current = item["bot"], item["credential"], item["current"]
            uuid = bot.get("uuid")
            if (
                not isinstance(uuid, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uuid)
                or uuid in seen_bots
                or type(current) is not bool
                or current != (uuid == context["bot"]["uuid"])
                or (key is not None and not re.fullmatch(r"lbr_[A-Za-z0-9_-]{20,200}", key))
            ):
                raise ClientError("invalid_binding_recovery_result")
            if current:
                if key is None or not secrets.compare_digest(key, self.key):
                    raise ClientError("binding_recovery_current_key_mismatch")
                match_current = True
            elif key is not None and secrets.compare_digest(key, self.key):
                raise ClientError("binding_recovery_scope_mismatch")
            seen_bots.add(uuid)
            records.append(({field: bot[field] for field in ("id", "uuid", "name", "type")}, key, current))
        if not match_current:
            raise ClientError("binding_recovery_current_key_missing")
        return records

    def _enrollment_endpoint(self):
        manifest = self.discover()
        endpoint = self.base + ENROLLMENT_PATH
        if (
            same_origin_url(self.base, manifest.get("enrollment_url")) != endpoint
            or same_origin_url(self.base, manifest.get("context_url")) != self.base + "/api/agent/context"
        ):
            raise ClientError("unsupported_enrollment_endpoint")
        return endpoint

    def enroll(self, directory, group=None, agent_label=None, *, scope="user", purpose="bind"):
        if self.config:
            raise ClientError("enrollment_requires_new_connection")
        group = enrollment_group(group) if group is not None else None
        label = normalize_agent_label(agent_label) if agent_label is not None else None
        if directory is None:
            target = (group or "") if purpose == "bind" else "\0" + purpose
            name = "enrollment-" + hashlib.sha256((self.base + "\0" + target).encode()).hexdigest()[:32]
            directory = new_catalog_entry(scope, name)
            if group is None:
                sequence = 1
                while not directory.is_symlink() and directory.is_dir():
                    if not (directory / "connection.json").is_file():
                        try:
                            previous = pending_enrollment_record(directory)
                        except (ClientError, OSError, KeyError, ValueError):
                            break
                        if purpose == "bind" or previous.get("purpose") != purpose or previous["status"] != "expired":
                            break
                    sequence += 1
                    directory = directory.with_name(f"{name}-{sequence}")
        path = Path(directory).absolute()
        if os.path.lexists(path):
            raise ClientError("enrollment_directory_exists")
        endpoint = self._enrollment_endpoint() if purpose == "bind" else self._credential_challenge_endpoint()
        self._check_installed_version()
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        state = {
            "approved_origin": self.base,
            "instance_id": self.manifest["instance_id"],
            "target_group": group,
            "status": "requesting",
        }
        if purpose != "bind":
            state["purpose"] = purpose
        if label is not None:
            state["agent_label"] = label
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ClientError("enrollment_directory_exists") from exc
        write_private(path / "enrollment.json", json.dumps(state).encode())
        if purpose == "bind":
            issued = self._request(endpoint, method="POST", readonly=False)
            binding_id, secret, prefix = issued.get("binding_id"), issued.get("credential"), ""
            secret_pattern, secret_file = r"lbr_[A-Za-z0-9_-]{20,200}", "key"
        else:
            issued = self._request(endpoint, method="POST", body={"purpose": purpose}, readonly=False)
            binding_id, secret = issued.get("challenge_id"), issued.get("proof")
            prefix = {"recover": "REC-", "reset": "RST-"}[purpose]
            secret_pattern, secret_file = r"lbp_[A-Za-z0-9_-]{20,200}", "proof"
        code = issued.get("bind_code")
        expires_at = issued.get("bind_expires_at")
        try:
            enrollment_deadline(expires_at)
        except ValueError:
            raise ClientError("invalid_enrollment_response") from None
        if (
            not isinstance(binding_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{22}", binding_id)
            or not isinstance(secret, str)
            or not re.fullmatch(secret_pattern, secret)
            or not isinstance(code, str)
            or not re.fullmatch(prefix + r"[ACDEFGHJKMNPQRTUVWXY2345679]{8}", code)
        ):
            raise ClientError("invalid_enrollment_response")
        write_private(path / secret_file, secret.encode())
        state.update(binding_id=binding_id, status="pending", bind_expires_at=expires_at)
        write_private(path / "enrollment.json", json.dumps(state).encode(), replace=True)
        return {
            "status": "pending",
            "purpose": purpose,
            "target_group": group,
            "bind_command": f"bind {code}",
            "bind_expires_at": expires_at,
            "enrollment_dir": str(path),
        }

    def check_enrollment(self, path, state, *, finish=False, confirm_bot_name=None):
        if "target_group" not in state:
            raise ClientError("invalid_enrollment_state")
        group = state["target_group"]
        if group is not None:
            group = enrollment_group(group)
        instance_id = state.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ClientError("invalid_enrollment_state")
        result = {"target_group": group, "connection_created": False}
        if state.get("status") == "requesting":
            return {**result, "status": "request_outcome_unknown"}
        binding_id = state.get("binding_id")
        if (
            state.get("status") != "pending"
            or not isinstance(binding_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{22}", binding_id)
        ):
            raise ClientError("invalid_enrollment_state")
        self.config = {
            "approved_origin": self.base,
            "instance_id": instance_id,
            "key_file": "key",
        }
        self.connection_dir = path
        if "agent_label" in state:
            self.config["agent_label"] = normalize_agent_label(state["agent_label"])
        purpose = state.get("purpose", "bind")
        if purpose in ("recover", "reset"):
            try:
                deadline = enrollment_deadline(state.get("bind_expires_at"))
            except ValueError:
                raise ClientError("invalid_enrollment_state") from None
            return self._check_credential_challenge(path, binding_id, purpose, deadline, finish=finish)
        if purpose != "bind":
            raise ClientError("invalid_enrollment_state")
        endpoint = self._enrollment_endpoint()
        binding = self._request(f"{endpoint}/{binding_id}")
        status = binding.get("status")
        if binding.get("binding_id") != binding_id or status not in ("pending", "confirmed", "expired", "revoked"):
            raise ClientError("invalid_enrollment_status")
        result["status"] = status
        if status != "confirmed":
            return result
        context = self._read_context(initializing=True)
        bot, actor = context["bot"], context["actor"]
        bot_name = enrollment_group(bot.get("name"))
        result.update(bot=bot, actor=actor)
        if group is not None:
            result["matches_target_group"] = bot_name == group
        if not finish:
            return result
        expected_bot_name = confirm_bot_name or group
        if expected_bot_name is not None and bot_name != expected_bot_name:
            return {**result, "status": "bot_name_confirmation_required"}
        connection = path / "connection.json"
        try:
            self._save_connection(context, connection)
        except FileExistsError as exc:
            raise ClientError("enrollment_connection_exists") from exc
        return {**result, "connection_created": True, "connection": str(connection)}

    def _credential_challenge_endpoint(self):
        if "chat_credential_challenges" not in self.discover().get("capabilities", ()):
            raise ClientError("chat_credential_challenges_unavailable")
        return self.base + CREDENTIAL_CHALLENGE_PATH

    def _check_credential_challenge(self, path, binding_id, purpose, deadline, *, finish):
        endpoint = self._credential_challenge_endpoint()
        proof = private_bytes(path / "proof").decode()
        if not re.fullmatch(r"lbp_[A-Za-z0-9_-]{20,200}", proof):
            raise ClientError("invalid_challenge_proof")
        self.secrets.append(proof)
        try:
            response = self._request(f"{endpoint}/{binding_id}", token=proof)
        except ClientError as exc:
            if exc.status != 404:
                raise
            status = "expired" if deadline <= datetime.now(timezone.utc) else "unavailable"
            return {"purpose": purpose, "status": status, "connection_created": False}
        status = response.get("status")
        result = {"purpose": purpose, "status": status, "connection_created": False}
        if status in ("pending", "unavailable"):
            return result
        key = response.get("credential")
        if status != "confirmed" or not isinstance(key, str) or not re.fullmatch(r"lbr_[A-Za-z0-9_-]{20,200}", key):
            raise ClientError("invalid_credential_challenge_result")
        if purpose == "recover" and response.get("outcome") == "created":
            result["outcome"] = "created"
        self.secrets.append(key)
        if purpose == "reset":
            result["chat_action"] = "reset_applied"
        if not finish:
            if purpose == "reset":
                result["next_command"] = ["finish-enrollment", "--enrollment-dir", str(path)]
                result["next_step"] = "The group already applied the reset. Finish to save the result locally."
            return result
        key_path = path / "key"
        try:
            write_private(key_path, key.encode())
        except FileExistsError:
            if not secrets.compare_digest(private_bytes(key_path), key.encode()):
                raise ClientError("challenge_key_file_conflict") from None
        context = self._read_context(initializing=True)
        connection = path / "connection.json"
        if not connection.exists():
            self._save_connection(context, connection)
        else:
            if private_json(connection) != {
                **self.config,
                "bot_uuid": context["bot"]["uuid"],
                "actor": {field: context["actor"][field] for field in ("platform", "external_user_id")},
            }:
                raise ClientError("enrollment_connection_exists")
        return {**result, "connection_created": True, "connection": str(connection), "bot": context["bot"]}

    def resources(self, context):
        generation = self.node_access.invalidate() if self.node_access else None
        snapshot, started = self._request(
            same_origin_url(self.base, context["links"]["resources"]), token=self.key, include_started=True
        )
        if generation:
            nodes = self._current_node_access(snapshot, context, self.clock() - started)
            scope = {
                "approved_origin": self.base,
                "instance_id": self.config["instance_id"],
                "bot_uuid": context["bot"]["uuid"],
                "actor": {field: context["actor"][field] for field in ("platform", "external_user_id")},
            }
            self.node_access.save(generation, nodes, self.redacted(scope))
        snapshot["snapshot_fresh_for_write"] = self._snapshot_fresh_for_write(snapshot, started)
        return snapshot

    def resources_with_hardware_notes(self, context):
        snapshot = self.resources(context)
        bot = context["bot"]
        if (
            self.hardware_notes is None
            or bot.get("type") not in {"NODE", "QUEUE"}
            or snapshot.get("bot_id") != bot["id"]
            or not isinstance(snapshot.get("resources"), list)
        ):
            return snapshot
        try:
            notes = private_json(self.hardware_notes)
        except ClientError:
            return snapshot
        scope = {"approved_origin": self.base, "instance_id": self.config["instance_id"], "bot_uuid": bot["uuid"]}
        entries = notes.get("nodes")
        if type(notes.get("version")) is not int or notes["version"] != 1 or notes.get("scope") != scope:
            return snapshot
        if not isinstance(entries, dict):
            return snapshot
        for node in snapshot["resources"]:
            if not isinstance(node, dict) or "accelerators" in node or not isinstance(node.get("id"), str):
                continue
            note = _local_hardware_note(entries.get(node["id"]))
            if note is not None:
                node["local_hardware_note"] = note
        return snapshot

    def _current_node_access(self, snapshot, context, elapsed):
        if (
            context.get("agent_access_enabled") is not True
            or snapshot.get("health") != "running"
            or snapshot.get("bot_id") != context["bot"]["id"]
        ):
            return {}
        try:
            age = float(snapshot["age_seconds"])
            if not math.isfinite(age) or age < 0:
                return {}
            reference = self._expiry({"expires_at": snapshot["captured_at"]}) + age + elapsed
        except (ClientError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return {}
        now = time.time()
        actor = context["actor"]["external_user_id"]
        nodes = {}
        inventory = snapshot.get("resources")
        if not isinstance(inventory, list):
            return {}
        for node in inventory:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id or len(node_id) > 256:
                continue
            devices = node.get("devices", [node])
            if not isinstance(devices, list):
                continue
            for unit in devices:
                if not isinstance(unit, dict) or not isinstance(unit.get("holders"), list):
                    continue
                address = _node_address(unit.get("node_access"))
                holder = self._own([holder for holder in unit["holders"] if isinstance(holder, dict)], actor)
                if address is None or holder is None:
                    continue
                try:
                    remaining = self._expiry(holder) - reference
                except (ClientError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    continue
                expires = now + remaining
                if remaining > 0 and (node_id not in nodes or expires > nodes[node_id]["usable_until"]):
                    nodes[node_id] = {**address, "expires_at": holder["expires_at"], "usable_until": expires}
        return self.redacted(nodes)

    def resource_guidance(self, reference=None):
        if reference is not None and not re.fullmatch(r"[a-f0-9]{32}", reference):
            raise ClientError("invalid_resource_guidance_reference")
        context = self.context()
        bot_id = context["bot"]["id"]
        if type(bot_id) is not int or bot_id <= 0:
            raise ClientError("invalid_resource_guidance_scope")
        path = f"/api/bots/{bot_id}/resource-guidance"
        if reference is not None:
            path += "/" + reference
        result = self._request(self.base + path, token=self.key)
        if result.get("protocol") != "lockbot.resource-guidance.v1" or (
            not isinstance(result.get("items"), list) if reference is None else result.get("id") != reference
        ):
            raise ClientError("invalid_resource_guidance_response")
        return result

    def personal_report(self, extension):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", extension):
            raise ClientError("invalid_report_extension")
        context = self.context()
        directory = context.get("personal_report_extensions")
        if (
            not context.get("agent_access_enabled")
            or not isinstance(directory, dict)
            or directory.get("protocol") != "lockbot.personal-report.v1"
            or directory.get("credential_mode") != "same_origin_native"
            or not isinstance(directory.get("items"), list)
        ):
            raise ClientError("personal_reports_unavailable")
        path = "/api/personal-panel/agent/extensions/" + extension
        matches = [item for item in directory["items"] if isinstance(item, dict) and item.get("id") == extension]
        if len(matches) != 1 or matches[0].get("mode") != "read_only" or matches[0].get("read_path") != path:
            raise ClientError("report_extension_not_authorized")
        result = self._request(self.base + path, token=self.key)
        if (
            result.get("protocol") != directory["protocol"]
            or result.get("id") != extension
            or result.get("revision") != matches[0].get("revision")
            or result.get("status") not in {"ok", "unknown"}
            or not isinstance(result.get("groups"), list)
        ):
            raise ClientError("report_extension_changed_refresh_context")
        return result

    def panel_link(self):
        """Issue one private handoff without following or consuming its browser ticket."""
        url = urllib.parse.urlsplit(self.base)
        transport = "https"
        if url.scheme == "http":
            try:
                address = ipaddress.ip_address(url.hostname)
            except ValueError:
                raise ClientError("personal_panel_transport_refused") from None
            if not any(
                address in ipaddress.ip_network(network)
                for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128")
            ):
                raise ClientError("personal_panel_transport_refused")
            transport = "private-http"
        home = self.base + "/#/me"
        issue_path = "/api/personal-panel/links"
        capability = self._request(self.base + "/api/personal-panel/capabilities")
        management = capability.get("connection_management")
        if (
            capability.get("protocol") != "lockbot.personal-panel.v1"
            or capability.get("home") != home
            or capability.get("issue_path") != issue_path
            or capability.get("transport") != transport
            or not isinstance(management, dict)
            or type(management.get("version")) is not int
            or management["version"] != 1
            or management.get("list_path") != "/api/personal-panel/connections"
        ):
            raise ClientError("native_personal_panel_unavailable")
        context = self._read_context()
        if context.get("agent_access_enabled") is not True:
            raise ClientError("agent_access_disabled")
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        account_path = management.get("account_issue_path")
        if account_path == "/api/personal-panel/account-links":
            issue_path = account_path
        issued = self._request(self.base + issue_path, token=self.key, method="POST", readonly=False)
        login_url = issued.get("login_url")
        expires_in = issued.get("expires_in")
        prefix = home + "?ticket="
        if (
            issued.get("home") != home
            or type(expires_in) is not int
            or not 0 < expires_in <= 120
            or not isinstance(login_url, str)
            or not login_url.startswith(prefix)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", login_url[len(prefix) :])
            or self.redacted(login_url) != login_url
        ):
            raise ClientError("invalid_personal_panel_link")
        return {"home": home, "login_url": login_url}

    def command(
        self,
        operation,
        nodes,
        devices,
        duration,
        metadata=None,
        *,
        same_devices_on_all_nodes=False,
        device_count=None,
        model=None,
        minimum_model_count=None,
        group=None,
    ):
        if devices and len(nodes) > 1 and not same_devices_on_all_nodes:
            raise ClientError("multi_node_device_selection_requires_explicit_same_devices")
        if device_count is not None and (
            type(device_count) is not int or device_count < 1 or len(nodes) * len(devices) != device_count
        ):
            raise ClientError("selected_device_count_does_not_match_request")
        if model is not None and (not isinstance(model, str) or not model.strip() or not model.isprintable()):
            raise ClientError("invalid_accelerator_model")
        if minimum_model_count is not None and (
            model is None or devices or type(minimum_model_count) is not int or minimum_model_count < 1
        ):
            raise ClientError("minimum_model_count_requires_whole_node_and_model")
        context = self.context()
        if group is not None and context["bot"]["name"] != group:
            raise ClientError("selected_connection_does_not_match_requested_group")
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        if not context.get("agent_access_enabled"):
            raise ClientError("agent_access_disabled")
        if operation not in {"lock", "slock", "book", "claim", "renew", "release"}:
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
        if operation == "claim":
            if context["bot"]["type"] not in {"QUEUE", "QUEUE_DEVICE"} or len(nodes) != 1 or len(devices) > 1:
                raise ClientError("claim_requires_one_queued_resource")
            if duration is not None:
                raise ClientError("claim_uses_booked_duration")
        elif operation != "release" and (
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
        before = self.resources_with_hardware_notes(context) if model is not None else self.resources(context)
        self._require_fresh_snapshot(before)
        selected = self._selected(before, nodes, devices)
        if model is not None and not all(
            self._matches_model(item, model.strip().casefold(), card_bot, minimum_model_count) for item in selected
        ):
            raise ClientError("selected_resource_model_unverified_or_insufficient")
        actor = context["actor"]["external_user_id"]
        held = [self._own(item.get("holders", []), actor) for item in selected]
        if operation == "renew" and (not all(held) or context.get("bot_rules", {}).get("forbid_relock")):
            raise ClientError("renewal_not_allowed_or_not_owned")
        if operation in {"lock", "slock"}:
            if any(held):
                raise ClientError("already_owned_use_explicit_renew")
            if any(item.get("claimable_by_you") is True for item in selected):
                raise ClientError("booking_claimable_use_explicit_claim")
        if operation == "book" and (
            any(held) or any(self._own(item.get("booking_list", []), actor) for item in selected)
        ):
            raise ClientError("already_owned_or_booked_read_state_do_not_rebook")
        wire = {"claim": "lock", "renew": "lock", "release": "unlock"}.get(operation, operation)
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
        if operation == "claim":
            item = selected[0]
            booked = self._own(item.get("booking_list", []), actor)
            if item.get("claimable_by_you") is not True or held[0] or not booked:
                raise ClientError("booking_not_claimable")
            if item.get("claim_command") != command:
                raise ClientError("unexpected_claim_command")
        if duration:
            command += " " + duration
        if len(command) > 512:
            raise ClientError("command_too_large")
        result = self._post_command(context, command, metadata)
        try:
            after = self.resources(context)
            self._require_fresh_snapshot(after)
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

    def _snapshot_fresh_for_write(self, snapshot, started):
        age = snapshot.get("age_seconds")
        return (
            type(age) in (float, int)
            and 0 <= age <= MAX_RESOURCE_SNAPSHOT_AGE
            and age + (self.clock() - started) <= MAX_RESOURCE_SNAPSHOT_AGE
        )

    def _require_fresh_snapshot(self, snapshot):
        if not snapshot["snapshot_fresh_for_write"]:
            raise ClientError("resource_snapshot_stale_or_unverifiable")

    def alias(self, node, value=None):
        if not re.fullmatch(r"[\w-]+", node):
            raise ClientError("invalid_node")
        if value is not None and (
            len(value) > 15 or not (value == "" or value.isprintable() and value == value.strip())
        ):
            raise ClientError("invalid_public_alias")
        if value and re.search(r"\b(?:lbr_|lbk_|lb[a-z][0-9]+\.)", value):
            raise ClientError("secret_in_alias_refused")
        context = self.context()
        if not context.get("agent_access_enabled"):
            raise ClientError("agent_access_disabled")
        before = self.resources(context)
        observed = self._alias_observation(before, node)
        if value is None:
            return observed
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        if context.get("bot_rules", {}).get("allow_user_alias") is not True:
            raise ClientError("aliases_disabled")
        if "alias" not in before.get("supported_operations", []):
            raise ClientError("operation_not_supported_by_bot")
        if observed["alias"] == value:
            return {**observed, "command_sent": False}
        command = f"alias {node}" + (f" {value}" if value else "")
        result = self._post_command(context, command, {})
        try:
            after = self._alias_observation(self.resources(context), node)
            if after["alias"] != value:
                raise ClientError("alias_value_mismatch")
        except (ClientError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ClientError("mutation_not_confirmed_read_resources_do_not_replay") from exc
        return {**after, "command_sent": True, "command_result": result}

    def _alias_observation(self, snapshot, node):
        (parent,) = self._selected(snapshot, [node], [])
        alias = parent.get("alias")
        observed_at = snapshot.get("aliases_observed_at")
        if not isinstance(alias, str) or not isinstance(observed_at, str):
            raise ClientError("alias_readback_unavailable")
        try:
            timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ClientError("alias_readback_unavailable") from exc
        if timestamp.utcoffset() is None:
            raise ClientError("alias_readback_unavailable")
        return {
            "node": node,
            "alias": alias,
            "aliases_observed_at": observed_at,
            "verification": "observed_shared_alias_only",
        }

    def _post_command(self, context, command, metadata):
        # Header updates learned during the pre-read must block the subsequent write.
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        if self.node_access:
            self.node_access.invalidate()
        try:
            return self._request(
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
    def _matches_model(item, model, card_bot, minimum_count):
        def is_match(value):
            return isinstance(value, str) and value.strip().casefold() == model

        if card_bot:
            return is_match(item.get("dev_model"))
        note = item.get("local_hardware_note")
        fallback = note.get("accelerators") if isinstance(note, dict) else None
        inventory = item.get("accelerators", fallback)
        return isinstance(inventory, list) and any(
            isinstance(row, dict)
            and is_match(row.get("model"))
            and (minimum_count is None or (type(row.get("count")) is int and row["count"] >= minimum_count))
            for row in inventory
        )

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
            if not isinstance(mapping, dict) or not nodes or len(nodes) > 64 or set(nodes) - set(mapping):
                raise ClientError("explicit_mapped_metrics_nodes_required")
            mappings = {node: mapping[node] for node in nodes}
            if any(not isinstance(target, str) or not target or len(target) > 256 for target in mappings.values()):
                raise ClientError("invalid_metrics_mapping")
            targets = sorted(set(mappings.values()))
            stale_seconds = source.get("staleSeconds", 90)
            if type(stale_seconds) is not int or not 30 <= stale_seconds <= 3600:
                raise ClientError("invalid_metrics_freshness")
            if source.get("accessMode") == "short_lived":
                access = (context.get("utilization") or {}).get("access") or {}
                token = access.get("token")
                if access.get("scope") != "metrics:read" or not isinstance(token, str) or not token:
                    raise ClientError("metrics_grant_unavailable")
            elif source.get("accessMode") == "independent":
                token = private_bytes(self._file("metrics_key_file")).decode().strip()
            else:
                raise ClientError("unsupported_metrics_authentication")
            if token.startswith(("lbr_", "lbk_", "lbc1.")) or token == self.key:
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
                    labels = row.get("metric") if isinstance(row, dict) else None
                    if (
                        not isinstance(labels, dict)
                        or labels.get("node") not in targets
                        or labels.get("__name__") not in METRICS
                    ):
                        raise ClientError("metrics_scope_mismatch")
                return {
                    "best_effort": True,
                    "verification": "query_response_only",
                    "data": data,
                    "utilization": utilization_summary(vector, mappings, time.time(), stale_seconds),
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
                    if key.lower() in {"credential", "token", "secret", "api_key", "authorization", "proof"}
                    else self.redacted(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redacted(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"\b(?:lbr_|lbp_|lbk_)[A-Za-z0-9_-]{20,200}|lbc1\.[\w-]{22}\.[\w-]{43}", "[redacted]", value)
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
        return value

    def install(self, destination):
        output_path = Path(destination)
        target = output_path.absolute()
        if os.path.lexists(target):
            raise ClientError("install_destination_must_be_new")
        meta = self.discover()
        archive = self._request(same_origin_url(self.base, meta["installation"]["download_url"]), binary=True)
        if hashlib.sha256(archive).hexdigest() != meta["installation"]["sha256"]:
            raise ClientError("bundle_digest_mismatch")
        package_format = meta.get("package_format")
        if package_format == BUNDLE_FORMAT and "package_version" not in meta:
            raise ClientError("bundle_package_mismatch")
        reference_paths = REFERENCE_PATHS if package_format == BUNDLE_FORMAT else ()
        expected = (SCRIPT_PATH, "installation.json", "SKILL.md", *reference_paths)
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
                for name in ("SKILL.md", SCRIPT_PATH, *reference_paths):
                    if hashlib.sha256(contents[name]).hexdigest() != stamp["files"][name]:
                        raise ClientError("bundle_file_digest_mismatch")
        except (ValueError, OSError, KeyError, zipfile.BadZipFile, RecursionError) as exc:
            raise ClientError("invalid_installation_bundle") from exc
        if "package_version" in meta and (
            package_format not in (LEGACY_BUNDLE_FORMAT, BUNDLE_FORMAT)
            or stamp.get("package_format") != meta["package_format"]
            or stamp.get("package_version") != meta["package_version"]
            or canonical_package_version(
                contents["SKILL.md"],
                contents[SCRIPT_PATH],
                stamp.get("deployment_origin"),
                {name: contents[name] for name in reference_paths},
            )
            != meta["package_version"]
        ):
            raise ClientError("bundle_package_mismatch")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".lockbot-install-") as staging:
                staged = Path(staging) / "bundle"
                staged.mkdir(mode=0o700)
                for name, content in contents.items():
                    path = staged / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                if os.path.lexists(target):
                    raise ClientError("install_destination_must_be_new")
                try:
                    staged.rename(target)
                except OSError as exc:
                    if os.path.lexists(target):
                        raise ClientError("install_destination_must_be_new") from exc
                    raise
        except OSError as exc:
            raise ClientError("install_write_failed") from exc
        return {
            "installed_to": str(output_path),
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
    parser = SafeParser(
        description="Install the LockBot Skill, manage private connections, and query or allocate compute resources.",
        epilog="Run SUBCOMMAND --help for its options. Start with connections, then pass --connection FILE before "
        "context, resources, or command. Supply Key file paths only.",
    )
    parser.add_argument("--connection", help="Verified private connection JSON file (mode 0600)")
    sub = parser.add_subparsers(dest="action", required=True)
    bootstrap = {
        "discover": "Verify an approved service and optionally save its metadata",
        "install": "Verify and install the complete Skill ZIP in a new directory",
        "init": "Verify an existing personal Key file and save its scoped connection",
    }
    for action, help_text in bootstrap.items():
        command = sub.add_parser(action, help=help_text, description=help_text)
        command.add_argument("--origin", required=True, help="Explicitly user-approved service origin")
        if action == "install":
            command.add_argument("--destination", required=True, help="New Skill installation directory")
        if action == "discover":
            command.add_argument("--destination", help="New private file for approved service metadata")
        if action == "init":
            command.add_argument("--destination", help="Explicit private connection file")
            command.add_argument(
                "--scope", choices=["user", "project"], default="user", help="Connection storage scope (default: user)"
            )
            command.add_argument("--key-file", required=True, help="Existing private personal-key file, not a raw key")
            command.add_argument("--metrics-origin", help="Separately approved metrics origin, optional")
            command.add_argument("--agent-label", help="Optional stable name for this Agent installation")
    enroll = sub.add_parser("enroll", help="Request a bind code for the user to confirm in the target group")
    enroll.add_argument("--origin", required=True)
    enroll.add_argument("--group", help="Optional group explicitly named by the user")
    enroll.add_argument("--destination", help="Explicit new private enrollment directory")
    enroll.add_argument(
        "--scope", choices=["user", "project"], default="user", help="Credential storage scope (default: user)"
    )
    enroll.add_argument("--agent-label", help="Optional stable name for this Agent installation")
    enroll.add_argument("--authorized", action="store_true", required=True, help="User authorized requesting a code")
    challenge = sub.add_parser(
        "key-challenge", help="Request a chat code to recover or explicitly reset a personal Key"
    )
    challenge.add_argument("--origin", required=True)
    challenge.add_argument("--purpose", choices=["recover", "reset"], default="recover")
    challenge.add_argument("--destination", help="Explicit new private challenge directory")
    challenge.add_argument("--scope", choices=["user", "project"], default="user")
    challenge.add_argument("--authorized", action="store_true", required=True, help="User authorized requesting a code")
    for action, help_text in (
        ("enrollment-status", "Check a previously requested binding code"),
        ("finish-enrollment", "Verify chat confirmation and save the scoped connection"),
    ):
        command = sub.add_parser(action, help=help_text, description=help_text)
        command.add_argument("--enrollment-dir", required=True, help="Private directory returned by enroll")
        if action == "finish-enrollment":
            command.add_argument("--confirm-bot-name", help="Bot name explicitly confirmed by the user")
    catalog = sub.add_parser("connections", help="List project and user connections plus unfinished enrollments")
    catalog.add_argument("--directory", help="Read only this private connection catalog")
    sub.add_parser("context", help="Verify the selected connection's identity, bot and rules")
    sub.add_parser("resources", help="Read the selected bot's current resources and queue")
    recovery = sub.add_parser("recover", help="Save recoverable group credentials through a verified seed connection")
    recovery.add_argument("--destination", help="Explicit new private directory for separate group connections")
    recovery.add_argument("--scope", choices=["user", "project"], default="user")
    sub.add_parser(
        "panel-link",
        help="Issue a single-use sign-in link for an authorized private handoff",
        description="Run with --connection for an authorized private handoff. The recipient confirms in their browser.",
    )
    guidance = sub.add_parser("resource-guidance", help="Read deployment-owned references for current holdings")
    guidance.add_argument("--reference")
    report = sub.add_parser("personal-report", help="Read a granted personal report")
    report.add_argument("--extension", required=True)
    alias = sub.add_parser("alias", help="Read or edit a shared node alias")
    alias.add_argument("--node", required=True)
    edit = alias.add_mutually_exclusive_group()
    edit.add_argument("--set", dest="alias_value", metavar="LABEL")
    edit.add_argument("--clear", dest="alias_value", action="store_const", const="")
    command = sub.add_parser(
        "command",
        help="Allocate, book, claim, renew, or release exact resources",
        description="Use the selected connection to send one resource operation and read back its result.",
        epilog="Example: --connection CONNECTION command --operation lock --node node-01 --device 0 --duration 30m",
    )
    command.add_argument("--operation", required=True, choices=["lock", "slock", "book", "claim", "renew", "release"])
    command.add_argument(
        "--node", action="append", required=True, help="Exact node ID from resources. Repeat for multiple nodes."
    )
    command.add_argument(
        "--device", action="append", default=[], help="Exact device ID on the selected node. Repeat for more cards."
    )
    command.add_argument(
        "--same-devices-on-all-nodes",
        action="store_true",
        help="Apply all listed device IDs to every selected node. Use separate commands for different IDs per node.",
    )
    command.add_argument("--device-count", type=int, help="Require exactly this many selected cards in this command.")
    command.add_argument("--model", help="Check that every selected node or card has this accelerator model.")
    command.add_argument("--group", help="Require the connection to match the exact group named by the user.")
    command.add_argument(
        "--minimum-model-count", type=int, help="Require this many accelerators of --model on each selected whole node."
    )
    command.add_argument(
        "--duration", help="Lease for lock/book or time to add on renew, e.g. 30m. Omit for claim/release."
    )
    command.add_argument("--metadata-file", help="Private JSON: task_category, purpose, agent_label only")
    command = sub.add_parser("metrics", help="Read current utilization for exact nodes from an approved source")
    command.add_argument(
        "--node", action="append", required=True, help="Exact node ID from resources. Repeat as needed."
    )
    args = parser.parse_args(argv)
    client = None
    try:
        if args.action == "connections":
            if args.connection:
                raise ClientError("connection_not_used_for_listing")
            print(
                json.dumps({"result": listed_connections(args.directory), "update_required": False}, ensure_ascii=False)
            )
            return 0
        enrollment_actions = {"enroll", "key-challenge", "enrollment-status", "finish-enrollment"}
        if args.action in enrollment_actions and args.connection:
            raise ClientError("enrollment_requires_new_connection")
        if args.action in {"enrollment-status", "finish-enrollment"}:
            path, state = enrollment_state(args.enrollment_dir)
            client = Client(approved_origin=state.get("approved_origin"))
        elif args.action in {"discover", "install", "init", "enroll", "key-challenge"} or args.connection:
            client = Client(args.connection, approved_origin=getattr(args, "origin", None))
        else:
            raise ClientError("private_connection_required")
        if args.action == "discover":
            result = client.discover()
            if args.destination:
                metadata = {
                    "approved_origin": client.base,
                    "instance_id": result["instance_id"],
                    "version": result["version"],
                    "skill_url": same_origin_url(client.base, result["skill_url"]),
                }
                try:
                    write_private(args.destination, json.dumps(metadata).encode())
                except FileExistsError:
                    if private_json(args.destination) != metadata:
                        raise ClientError("discovery_destination_exists_with_different_metadata") from None
        elif args.action == "install":
            result = client.install(args.destination)
        elif args.action == "init":
            result = client.initialize(
                args.key_file, args.destination, args.metrics_origin, args.agent_label, scope=args.scope
            )
        elif args.action == "enroll":
            result = client.enroll(args.destination, args.group, args.agent_label, scope=args.scope)
        elif args.action == "key-challenge":
            result = client.enroll(args.destination, scope=args.scope, purpose=args.purpose)
        elif args.action in {"enrollment-status", "finish-enrollment"}:
            name = getattr(args, "confirm_bot_name", None)
            result = client.check_enrollment(
                path,
                state,
                finish=args.action == "finish-enrollment",
                confirm_bot_name=enrollment_group(name) if name is not None else None,
            )
        elif args.action == "context":
            result = client.context()
        elif args.action == "resources":
            result = client.resources_with_hardware_notes(client.context())
        elif args.action == "recover":
            result = client.recover(args.destination, scope=args.scope)
        elif args.action == "resource-guidance":
            result = client.resource_guidance(args.reference)
        elif args.action == "personal-report":
            result = client.personal_report(args.extension)
        elif args.action == "panel-link":
            result = client.panel_link()
        elif args.action == "alias":
            result = client.alias(args.node, args.alias_value)
        elif args.action == "command":
            metadata = private_json(args.metadata_file) if args.metadata_file else {}
            result = client.command(
                args.operation,
                args.node,
                args.device,
                args.duration,
                metadata,
                same_devices_on_all_nodes=args.same_devices_on_all_nodes,
                device_count=args.device_count,
                model=args.model,
                minimum_model_count=args.minimum_model_count,
                group=args.group,
            )
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
        if exc.retry_after_seconds is not None:
            output["retry_after_seconds"] = exc.retry_after_seconds
        print(json.dumps(client.redacted(output) if client else output), file=sys.stderr)
        return 2
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OSError):
        print(json.dumps({"error": "invalid_server_or_local_data", "retry_mutation": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
