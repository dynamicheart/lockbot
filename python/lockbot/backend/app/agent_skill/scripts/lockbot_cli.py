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
from collections import Counter
from contextlib import ExitStack, contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path

SERVICE = "lockbot.resource-api"
API_VERSION = "1"
SCRIPT_PATH = "scripts/lockbot_cli.py"
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
MAX_CATALOG_RECORDS = 256
MAX_CATALOG_ENTRIES = 4096
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_SNAPSHOT_AGE = 5.0
MAX_OVERVIEW_ENTRIES = 256
MAX_OVERVIEW_BYTES = 16 * 1024
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


def format_lease_times(snapshot):
    """Replace raw holder expiry fields with a display time after verification."""
    age = metric_number(snapshot.get("age_seconds"))
    for node in snapshot.get("resources", []):
        for resource in (node, *node.get("devices", [])):
            for holder in resource.get("holders", []):
                remaining = metric_number(holder.pop("remaining_seconds", None))
                holder.pop("expires_at", None)
                if remaining is None or age is None or age < 0:
                    holder["remaining_time"] = None
                    continue
                seconds = remaining - age
                if seconds <= 0:
                    text = "due now"
                elif seconds < 60:
                    text = "<1m"
                else:
                    hours, minutes = divmod(math.ceil(seconds / 60), 60)
                    days, hours = divmod(hours, 24)
                    units = ((days, "d"), (hours, "h")) if days else ((hours, "h"), (minutes, "m"))
                    text = " ".join(f"{value}{unit}" for value, unit in units if value)
                holder["remaining_time"] = text


def read_snapshot_fresh(snapshot):
    """Read freshness does not grant write authority to a compatible read profile."""
    return snapshot.get("snapshot_fresh_for_read", snapshot.get("snapshot_fresh_for_write")) is True


def inventory_overview(snapshot, context):
    """Group observed IDs without inferring availability, hardware or policy."""
    overview = {
        "source": "verified_snapshot",
        "meaning": "Observed lock/queue state only, not proof of allocatability, "
        "policy approval or queue authorization. "
        "Queued IDs are not lock candidates. Unreported fields are unknown, not zero or empty. "
        "Full resources remain authoritative.",
        "snapshot_fresh": read_snapshot_fresh(snapshot),
        "nodes": [],
        "truncated": False,
        "omitted_entries": 0,
        "unidentified_entries": 0,
    }
    resources = snapshot.get("resources")
    if not isinstance(resources, list):
        overview["unidentified_entries"] = 1
        return overview

    def identifier(value):
        return (type(value) is int and value >= 0) or (isinstance(value, str) and bool(value))

    def state(unit, queued, valid):
        holders, queue = unit.get("holders"), unit.get("booking_list", None if queued else [])
        status = unit.get("lock_status")
        if (
            not valid
            or not overview["snapshot_fresh"]
            or not isinstance(status, str)
            or status not in {"idle", "exclusive", "shared"}
            or not isinstance(holders, list)
            or not isinstance(queue, list)
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("external_user_id"), str)
                or not row["external_user_id"]
                for row in holders + queue
            )
            or (status == "idle" and holders)
            or (status in {"exclusive", "shared"} and not holders)
            or (status == "exclusive" and len(holders) != 1)
        ):
            return "unknown"
        return "queued" if queue else "idle" if status == "idle" else "busy"

    node_ids = Counter(str(node["id"]) for node in resources if isinstance(node, dict) and identifier(node.get("id")))
    entries = 0
    for node in resources:
        if not isinstance(node, dict) or not identifier(node.get("id")):
            overview["unidentified_entries"] += 1
            continue
        cards = node.get("devices")
        if "devices" in node and not isinstance(cards, list):
            overview["unidentified_entries"] += 1
            continue
        units = cards if cards is not None else [node]
        count = 1 + len(units)
        if entries + count > MAX_OVERVIEW_ENTRIES:
            overview["truncated"] = True
            overview["omitted_entries"] += count
            continue
        item = {
            "node": node["id"],
            "alias": node.get("alias"),
            "allocation": "card" if cards is not None else "whole_node",
            "groups": [],
        }
        hardware = node.get("accelerators") if cards is None else None
        if cards is None:
            unreported = [field for field in ("accelerators", "lock_status", "booking_list") if node.get(field) is None]
            if unreported:
                item["not_reported_fields"] = unreported
            if isinstance(hardware, list) and len(hardware) > MAX_OVERVIEW_ENTRIES:
                overview["truncated"] = True
                overview["omitted_entries"] += count + len(hardware)
                continue
            item["accelerators"] = (
                [
                    {"model": row.get("model"), "count": row.get("count")}
                    if isinstance(row, dict)
                    else {"model": None, "count": None}
                    for row in hardware
                ]
                if isinstance(hardware, list)
                else None
            )
        known = (
            hardware[0].get("model")
            if isinstance(hardware, list)
            and len(hardware) == 1
            and isinstance(hardware[0], dict)
            and type(hardware[0].get("count")) is int
            and hardware[0]["count"] > 0
            else None
        )
        ids = Counter(str(unit["id"]) for unit in units if isinstance(unit, dict) and identifier(unit.get("id")))
        groups = {}
        bot_type = context.get("bot", {}).get("type")
        for unit in units:
            if not isinstance(unit, dict) or not identifier(unit.get("id")):
                overview["unidentified_entries"] += 1
                continue
            model = unit.get("dev_model") if cards is not None else known
            model = model if isinstance(model, str) and model.strip() else None
            if model not in groups:
                groups[model] = {"model": model, "idle": [], "busy": [], "queued": [], "unknown": []}
            valid = (
                node_ids[str(node["id"])] == 1
                and ids[str(unit["id"])] == 1
                and bot_type in ({"DEVICE", "QUEUE_DEVICE"} if cards is not None else {"NODE", "QUEUE"})
            )
            label = state(unit, bot_type in {"QUEUE", "QUEUE_DEVICE"}, valid)
            groups[model][label].append(unit["id"])
        item["groups"] = list(groups.values())
        candidate = {**overview, "nodes": [*overview["nodes"], item]}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_OVERVIEW_BYTES - 256:
            overview["truncated"] = True
            overview["omitted_entries"] += count
            continue
        overview["nodes"].append(item)
        entries += count
    return overview


def utilization_sample(values, success, stamps, now, stale_seconds, *, value_field="percent", maximum=100):
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
        or (values and (value is None or value < 0 or maximum is not None and value > maximum))
    ):
        status = "invalid"
    elif not success or age is None or not values:
        status = "missing"
    elif age > stale_seconds:
        status = "stale"
    else:
        status = "ok"
    return {"status": status, value_field: value if status == "ok" else None, "sample_age_seconds": age}


def utilization_summary(vector, mappings, now, stale_seconds):
    collectors = {}
    samples = {}
    devices = {}
    filesystems = {}
    for row in vector:
        labels = row["metric"]
        name, node = labels["__name__"], labels["node"]
        pair = row.get("value")
        value = metric_number(pair[1]) if isinstance(pair, list) and len(pair) == 2 else None
        if name in ("lockbot_sample_timestamp_seconds", "lockbot_collector_success"):
            collector = labels.get("collector")
            if collector in ("accelerator", "host"):
                collectors.setdefault((node, collector, name), []).append(value)
        elif name == "lockbot_host_cpu_utilization_percent" or name.startswith("lockbot_host_memory_"):
            samples.setdefault((node, "host", None, name), []).append(value)
        elif name.startswith("lockbot_host_filesystem_"):
            filesystem = labels.get("filesystem")
            if isinstance(filesystem, str) and filesystem:
                filesystems.setdefault(node, set()).add(filesystem)
                samples.setdefault((node, "host", filesystem, name), []).append(value)
        elif name.startswith("lockbot_accelerator_"):
            device = labels.get("device")
            if isinstance(device, str) and device:
                devices.setdefault(node, set()).add(device)
                samples.setdefault((node, "accelerator", device, name), []).append(value)

    def reading(node, collector, device=None, metric=None):
        metric = metric or (
            "lockbot_host_cpu_utilization_percent" if collector == "host" else "lockbot_accelerator_utilization_percent"
        )
        is_bytes = metric.endswith("_bytes")
        return utilization_sample(
            samples.get((node, collector, device, metric), []),
            collectors.get((node, collector, "lockbot_collector_success"), []),
            collectors.get((node, collector, "lockbot_sample_timestamp_seconds"), []),
            now,
            stale_seconds,
            value_field="bytes" if is_bytes else "percent",
            maximum=None if is_bytes else 100,
        )

    def memory(node, collector, device=None):
        fields = ("used", "total", "available", "cache") if collector == "host" else ("used", "total")
        prefix = "lockbot_host_memory_" if collector == "host" else "lockbot_accelerator_memory_"
        present = [field for field in fields if (node, collector, device, prefix + field + "_bytes") in samples]
        return (
            {"memory": {field: reading(node, collector, device, prefix + field + "_bytes") for field in present}}
            if present
            else {}
        )

    return [
        {
            "node": node,
            "source_node": target,
            "cpu": reading(target, "host"),
            **memory(target, "host"),
            "cards": [
                {"device": device, **reading(target, "accelerator", device), **memory(target, "accelerator", device)}
                for device in sorted(devices.get(target, ()))
            ],
            "cards_status": "reported" if target in devices else reading(target, "accelerator")["status"],
            **(
                {
                    "filesystems": [
                        {
                            "filesystem": name,
                            **{
                                field: reading(target, "host", name, "lockbot_host_filesystem_" + field + "_bytes")
                                for field in ("used", "total")
                            },
                        }
                        for name in sorted(filesystems[target])
                    ]
                }
                if target in filesystems
                else {}
            ),
        }
        for node, target in mappings.items()
    ]


class ClientError(Exception):
    def __init__(self, code, status=None, allowed_categories=None, *, retry_after_seconds=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.allowed_categories = allowed_categories
        self.retry_after_seconds = retry_after_seconds


class AutomaticScopeError(ClientError):
    """A proven pre-dispatch refusal; no command has been sent."""

    def __init__(self, code, question, **details):
        super().__init__(code)
        self.clarification = {"question_zh": question, **details}


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


class WindowsPrivateFiles:
    """Handle-based ACL checks and protected creation on a local Windows volume."""

    _ADMINISTRATORS = "S-1-5-32-544"
    _SYSTEM = "S-1-5-18"
    _READ = 0x80000000
    _WRITE = 0x40000000
    _READ_CONTROL = 0x20000
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _DISK = 1
    _DIRECTORY = 0x10
    _REPARSE_POINT = 0x400
    _OPEN_REPARSE_POINT = 0x00200000
    _BACKUP_SEMANTICS = 0x02000000
    _PERSISTENT_ACLS = 8

    def __init__(self):
        import ctypes as c
        import msvcrt
        from ctypes import wintypes as w

        self.c, self.msvcrt = c, msvcrt
        kernel = c.WinDLL("kernel32", use_last_error=True)
        security = c.WinDLL("advapi32", use_last_error=True)
        address, handle, word, material = c.c_void_p, w.HANDLE, w.DWORD, w.LPCWSTR
        self.address, self.word = address, word
        pointer, pointer_word = c.POINTER(address), c.POINTER(word)

        def bind(library, name, result, *arguments):
            operation = getattr(library, name)
            operation.restype = result
            operation.argtypes = arguments
            return operation

        self.close = bind(kernel, "CloseHandle", w.BOOL, handle)
        self.free = bind(kernel, "LocalFree", address, address)
        self.open = bind(kernel, "CreateFileW", handle, material, word, word, address, word, word, handle)
        self.make_directory = bind(kernel, "CreateDirectoryW", w.BOOL, material, address)
        self.drive_type = bind(kernel, "GetDriveTypeW", w.UINT, material)
        self.file_type = bind(kernel, "GetFileType", word, handle)
        self.final_path = bind(kernel, "GetFinalPathNameByHandleW", word, handle, w.LPWSTR, word, word)
        self.attributes = bind(kernel, "GetFileInformationByHandleEx", w.BOOL, handle, c.c_int, address, word)
        self.volume = bind(
            kernel,
            "GetVolumeInformationByHandleW",
            w.BOOL,
            handle,
            w.LPWSTR,
            word,
            pointer_word,
            pointer_word,
            pointer_word,
            w.LPWSTR,
            word,
        )
        self.get_security = bind(security, "GetSecurityInfo", word, handle, c.c_int, word, *[pointer] * 5)
        self.acl_info = bind(security, "GetAclInformation", w.BOOL, address, address, word, c.c_int)
        self.get_ace = bind(security, "GetAce", w.BOOL, address, word, pointer)
        self.convert_sid = bind(security, "ConvertSidToStringSidW", w.BOOL, address, pointer)
        self.convert_descriptor = bind(
            security,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            w.BOOL,
            material,
            word,
            pointer,
            pointer_word,
        )

        class SecurityAttributes(c.Structure):
            _fields_ = [("size", word), ("descriptor", address), ("inherit_handle", w.BOOL)]

        self.SecurityAttributes = SecurityAttributes
        get_process = bind(kernel, "GetCurrentProcess", handle)
        open_token = bind(security, "OpenProcessToken", w.BOOL, handle, word, c.POINTER(handle))
        token_info = bind(security, "GetTokenInformation", w.BOOL, handle, c.c_int, address, word, pointer_word)
        token, size = handle(), word()
        if not open_token(get_process(), 8, c.byref(token)):  # TOKEN_QUERY
            raise c.WinError(c.get_last_error())
        try:
            token_info(token, 1, None, 0, c.byref(size))  # TokenUser
            buffer = c.create_string_buffer(size.value)
            if not token_info(token, 1, buffer, size, c.byref(size)):
                raise c.WinError(c.get_last_error())
            self.user = self._sid(c.cast(buffer, pointer).contents)
        finally:
            self.close(token)
        self.allowed = {self.user, self._ADMINISTRATORS, self._SYSTEM}

    def _sid(self, sid):
        text = self.address()
        if not sid or not self.convert_sid(sid, self.c.byref(text)):
            raise ClientError("windows_private_acl_unavailable")
        try:
            return self.c.wstring_at(text)
        finally:
            self.free(text)

    def _local_path(self, path):
        path = Path(path).absolute()
        # Exclude network and device namespaces, and alternate data streams.
        if (
            not re.fullmatch(r"[A-Za-z]:", path.drive)
            or ":" in str(path)[2:]
            or self.drive_type(path.anchor) not in (2, 3)  # removable or fixed local drive
        ):
            raise ClientError("windows_private_local_path_required")
        return str(path)

    def _validate(self, handle, *, directory):
        c = self.c
        attributes = (self.word * 2)()
        flags = self.word()
        if (
            self.file_type(handle) != self._DISK
            or not self.attributes(handle, 9, attributes, c.sizeof(attributes))  # FileAttributeTagInfo
            or attributes[0] & self._REPARSE_POINT
            or bool(attributes[0] & self._DIRECTORY) != directory
            or not self.volume(handle, None, 0, None, None, c.byref(flags), None, 0)
            or not flags.value & self._PERSISTENT_ACLS
        ):
            raise ClientError("windows_private_acl_unavailable")
        resolved = c.create_unicode_buffer(32768)
        length = self.final_path(handle, resolved, len(resolved), 0)
        if not 0 < length < len(resolved) or not resolved.value.startswith("\\\\?\\"):
            raise ClientError("windows_private_local_path_required")
        self._local_path(resolved.value[4:])
        owner, acl, descriptor = self.address(), self.address(), self.address()
        # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
        error = self.get_security(handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor))
        try:
            if error or not acl or self._sid(owner) not in self.allowed:
                raise ClientError("windows_private_acl_required")
            sizes = (self.word * 3)()
            if not self.acl_info(acl, sizes, c.sizeof(sizes), 2):  # AclSizeInformation
                raise ClientError("windows_private_acl_required")
            for index in range(sizes[0]):
                ace = self.address()
                if not self.get_ace(acl, index, c.byref(ace)):
                    raise ClientError("windows_private_acl_required")
                kind = c.c_ubyte.from_address(ace.value).value
                if kind == 1:  # ACCESS_DENIED_ACE_TYPE
                    continue
                if kind != 0 or self._sid(ace.value + 8) not in self.allowed:  # ACCESS_ALLOWED_ACE_TYPE
                    raise ClientError("windows_private_acl_required")
        finally:
            if descriptor:
                self.free(descriptor)

    @contextmanager
    def _descriptor(self, directory):
        inheritance = "OICI" if directory else ""
        sddl = "D:P" + "".join(f"(A;{inheritance};FA;;;{sid})" for sid in sorted(self.allowed))
        descriptor = self.address()
        if not self.convert_descriptor(sddl, 1, self.c.byref(descriptor), None):
            raise self.c.WinError(self.c.get_last_error())
        try:
            attrs = self.SecurityAttributes(self.c.sizeof(self.SecurityAttributes), descriptor, False)
            yield self.c.byref(attrs)
        finally:
            self.free(descriptor)

    def _open_handle(self, path, *, directory=False, create=False, descriptor=None):
        handle = self.open(
            self._local_path(path),
            (self._WRITE if create else self._READ) | self._READ_CONTROL,
            1,
            descriptor,
            self._CREATE_NEW if create else self._OPEN_EXISTING,
            self._OPEN_REPARSE_POINT | (self._BACKUP_SEMANTICS if directory else 0),
            None,
        )
        if handle == self.address(-1).value or handle is None:
            raise self.c.WinError(self.c.get_last_error())
        try:
            self._validate(handle, directory=directory)
        except BaseException:
            self.close(handle)
            raise
        return handle

    def validate_directory(self, path):
        self.close(self._open_handle(path, directory=True))

    def mkdir(self, path, *, parents=False, exist_ok=False):
        if parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._descriptor(True) as descriptor:
            if not self.make_directory(self._local_path(path), descriptor):
                error = self.c.get_last_error()
                if error != 183 or not exist_ok:  # ERROR_ALREADY_EXISTS
                    raise self.c.WinError(error)
        self.validate_directory(path)

    def open_fd(self, path, *, create=False):
        if create:
            with self._descriptor(False) as descriptor:
                handle = self._open_handle(path, create=True, descriptor=descriptor)
        else:
            handle = self._open_handle(path)
        try:
            mode = os.O_WRONLY if create else os.O_RDONLY
            return self.msvcrt.open_osfhandle(handle, mode | os.O_BINARY)
        except BaseException:
            self.close(handle)
            raise


@lru_cache(maxsize=1)
def windows_private_files():
    return WindowsPrivateFiles()


def private_bytes(path):
    path = Path(path)
    try:
        # Check the opened file without following symlinks or waiting on a pipe.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        windows = os.name == "nt"
        descriptor = windows_private_files().open_fd(path) if windows else os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (not windows and info.st_mode & 0o077) or info.st_size > 16384:
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
    override = os.environ.get("LOCKBOT_HOME")
    if not override:
        return Path.home() / ".lockbot/connections"
    try:
        path = Path(override).expanduser()
    except RuntimeError as exc:
        raise ClientError("invalid_lockbot_home") from exc
    if not path.is_absolute():
        raise ClientError("invalid_lockbot_home")
    return path / "connections"


def private_directory(path, error):
    if os.name == "nt":
        windows_private_files().validate_directory(path)
        return
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise ClientError(error)


def make_private_directory(path, *, parents=False, exist_ok=False):
    if os.name == "nt":
        windows_private_files().mkdir(path, parents=parents, exist_ok=exist_ok)
    else:
        path.mkdir(mode=0o700, parents=parents, exist_ok=exist_ok)


def new_catalog_entry(scope, name=None):
    root = connection_catalog(scope)
    make_private_directory(root, parents=True, exist_ok=True)
    private_directory(root, "private_connection_directory_required")
    return root.resolve() / (name or secrets.token_hex(16))


class CatalogScanTruncated(Exception):
    pass


class CatalogScanBudget:
    """Local discovery limits, independent of resource allocation allowances."""

    def __init__(
        self, deadline, clock, *, records=MAX_CATALOG_RECORDS, entries=MAX_CATALOG_ENTRIES, size=MAX_CATALOG_BYTES
    ):
        self.deadline, self.clock = deadline, clock
        self.record_limit, self.entry_limit, self.byte_limit = records, entries, size
        self.records = self.entries = self.bytes = self.candidates = 0
        self.reasons = []

    def stop(self, reason):
        if reason not in self.reasons:
            self.reasons.append(reason)
        raise CatalogScanTruncated

    def check(self):
        if self.clock() >= self.deadline:
            self.stop("deadline")

    def paths(self, root):
        found = {"connection.json": [], "enrollment.json": []}
        stack = [(root, 0)]
        try:
            while stack:
                directory, depth = stack.pop()
                self.check()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        self.check()
                        if self.entries >= self.entry_limit:
                            self.stop("directory_entry_budget")
                        self.entries += 1
                        if entry.name in found:
                            if self.candidates >= self.record_limit:
                                self.stop("record_budget")
                            self.candidates += 1
                            found[entry.name].append(Path(entry.path))
                        elif depth < 2 and entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
        except CatalogScanTruncated:
            pass
        except OSError:
            if "directory_unavailable" not in self.reasons:
                self.reasons.append("directory_unavailable")
        return sorted(found["connection.json"]), sorted(found["enrollment.json"])

    def reserve(self, path, *, record=True):
        self.check()
        if record and self.records >= self.record_limit:
            self.stop("record_budget")
        # private_bytes rejects larger files without reading them. Reserve the
        # maximum possible read, so oversized or changing files remain bounded.
        size = 16385
        if self.bytes + size > self.byte_limit:
            self.stop("byte_budget")
        self.records += int(record)
        self.bytes += size

    def summary(self):
        return {
            "complete": not self.reasons,
            "reasons": self.reasons,
            "records_examined": self.records,
            "bytes_reserved": self.bytes,
            "directory_entries_examined": self.entries,
        }


def listed_connections(directory=None, *, scan_budget=None, _observe_credential=None, _observe_record=None):
    """List connections and resumable enrollments without opening their Key files."""
    if directory is not None:
        catalogs = [("custom", Path(directory))]
    else:
        catalogs = [("user", connection_catalog("user"))]
        if not os.environ.get("LOCKBOT_HOME"):
            catalogs.extend(
                [("user", Path.home() / ".config/lockbot/connections"), ("project", connection_catalog("project"))]
            )
    connections = []
    enrollments = []
    unavailable_count = 0
    for scope, root in catalogs:
        try:
            private_directory(root, "private_connection_directory_required")
        except FileNotFoundError:
            continue
        root = root.resolve()
        if scan_budget:
            paths, pending_paths = scan_budget.paths(root)
        else:
            paths, pending_paths = catalog_records(root, "connection.json"), catalog_records(root, "enrollment.json")
        completed = set()
        for path in paths:
            try:
                if scan_budget:
                    scan_budget.reserve(path)
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
                if _observe_credential is not None:
                    key_path = Path(record["key_file"])
                    _observe_credential(
                        key_path if key_path.is_absolute() else path.parent / key_path, service, instance
                    )
                if _observe_record is not None:
                    _observe_record(path, record)
                connections.append({**entry, "scope": scope})
                completed.add(path.parent)
            except CatalogScanTruncated:
                break
            except (ClientError, OSError, KeyError, RuntimeError, ValueError):
                unavailable_count += 1
        for path in pending_paths:
            if path.parent in completed:
                continue
            try:
                if scan_budget:
                    scan_budget.reserve(path)
                if path.resolve() != path:
                    raise ValueError
                entry = pending_enrollment_record(path.parent)
                if _observe_credential is not None:
                    _observe_credential(path.parent / "key", entry["origin"], entry["instance_id"])
                enrollments.append({**entry, "scope": scope})
            except CatalogScanTruncated:
                break
            except (ClientError, OSError, KeyError, RuntimeError, ValueError):
                unavailable_count += 1
        if scan_budget and scan_budget.reasons:
            break
    result = {"connections": connections, "pending_enrollments": enrollments, "unavailable_count": unavailable_count}
    if scan_budget:
        result["scan"] = scan_budget.summary()
    return result


def catalog_records(root, filename):
    return sorted([*root.glob(filename), *root.glob(f"*/{filename}"), *root.glob(f"*/*/{filename}")])


def known_key_scopes(key_file, budget, *, scope="user"):
    """Check existing catalog provenance without opening any credential contents.

    External custom connections and copies of a Key in a different file are not
    discoverable here. No arbitrary JSON search or persistent Key index is used.
    """
    try:
        selected = Path(key_file).resolve()
        identity = selected.stat()
        if not stat.S_ISREG(identity.st_mode):
            raise ClientError("private_file_permissions_or_size")
    except (OSError, RuntimeError) as exc:
        raise ClientError("private_file_unavailable") from exc
    matches = set()

    def observe(path, service, instance):
        budget.check()
        candidate = path.resolve()
        same = candidate == selected
        if not same:
            try:
                other = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            same = stat.S_ISREG(other.st_mode) and os.path.samestat(identity, other)
        if same:
            matches.add((service, instance))

    try:
        catalogs = [None]
        if scope == "project" and os.environ.get("LOCKBOT_HOME"):
            catalogs.append(connection_catalog("project"))
        for directory in catalogs:
            listing = listed_connections(directory, scan_budget=budget, _observe_credential=observe)
            if listing["unavailable_count"] or not listing["scan"]["complete"]:
                raise ClientError("key_origin_catalog_incomplete")
    except (ClientError, OSError, RuntimeError, CatalogScanTruncated) as exc:
        raise ClientError("key_origin_catalog_incomplete") from exc
    return matches


def connection_scope(record):
    """A local identity claim becomes evidence only after online verification."""
    actor = record.get("actor")
    if not isinstance(actor, dict):
        raise ClientError("connection_actor_not_pinned")
    fields = [record.get("instance_id"), record.get("bot_uuid"), actor.get("platform"), actor.get("external_user_id")]
    if any(not isinstance(value, str) or not value or len(value) > 512 for value in fields):
        raise ClientError("connection_identity_not_pinned")
    if "agent_label" in record:
        normalize_agent_label(record["agent_label"])
    return (origin(record.get("approved_origin")), *fields)


def resource_scope_digest(record):
    """A locator for a pinned principal and native scope, never an authority grant."""
    identity = [SERVICE, *connection_scope(record)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def resource_reference(record, node, *, width=12):
    """Describe an exact node without depending on a bot name or private file path."""
    if (
        not isinstance(node, str)
        or not 0 < len(node) <= 256
        or not node.strip()
        or not node.isprintable()
        or type(width) is not int
        or not 12 <= width <= 64
    ):
        raise ClientError("invalid_resource_reference")
    try:
        encoded = urllib.parse.quote(node, safe="", encoding="utf-8", errors="strict")
    except UnicodeError:
        raise ClientError("invalid_resource_reference") from None
    return f"s-{resource_scope_digest(record)[:width]}/{encoded}"


def parse_resource_reference(value):
    """Parse canonical spelling only; fuzzy matching belongs to discovery, not routing."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ClientError("invalid_resource_reference")
    prefix, separator, encoded = value.partition("/")
    if not separator or not re.fullmatch(r"s-[0-9a-f]{12,64}", prefix):
        raise ClientError("invalid_resource_reference")
    try:
        node = urllib.parse.unquote(encoded, encoding="utf-8", errors="strict")
        if (
            not 0 < len(node) <= 256
            or not node.strip()
            or not node.isprintable()
            or urllib.parse.quote(node, safe="", encoding="utf-8", errors="strict") != encoded
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ClientError("invalid_resource_reference") from None
    return prefix[2:], node


def resolve_resource_reference(value, directory=None):
    """Resolve local candidates without reading a Key, network I/O or cached resources.

    The caller must still verify the selected service, principal and current node
    through Client before reading protected data or dispatching one resource command.
    Equivalent scope records remain credential candidates, not a union of permissions.
    """
    token, node = parse_resource_reference(value)
    budget = CatalogScanBudget(time.monotonic() + 60, time.monotonic)
    try:
        listing = listed_connections(directory, scan_budget=budget)
        if listing["unavailable_count"] or not listing["scan"]["complete"]:
            raise ClientError("resource_reference_catalog_incomplete")
        matches = {}
        for locator in listing["connections"]:
            record = private_json(locator["connection"])
            identity = connection_scope(record)
            if resource_scope_digest(record).startswith(token):
                matches.setdefault(identity, []).append(locator)
    except (OSError, KeyError, TypeError, ValueError, CatalogScanTruncated):
        raise ClientError("resource_reference_catalog_incomplete") from None
    if not matches:
        raise ClientError("resource_reference_scope_unavailable")
    if len(matches) != 1:
        raise ClientError("resource_reference_scope_ambiguous")
    identity, candidates = next(iter(matches.items()))
    return {
        "node": node,
        "scope": identity,
        "connections": sorted(candidates, key=lambda item: item["connection"]),
        "online_verified": False,
    }


def write_private(path, content, *, replace=False):
    path = Path(path)
    temporary = None
    try:
        with ExitStack() as streams:
            if os.name == "nt":
                storage = windows_private_files()
                storage.validate_directory(path.parent)
                target = path.parent / (".lockbot-" + secrets.token_hex(16))
                stream = streams.enter_context(os.fdopen(storage.open_fd(target, create=True), "wb"))
                temporary = target
            else:
                stream = streams.enter_context(tempfile.NamedTemporaryFile(dir=path.parent, delete=False))
                temporary = Path(stream.name)
            stream.write(content)
        if replace:
            temporary.replace(path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
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
    private_directory(path, "private_enrollment_directory_required")
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
    if status == "pending":
        entry["status_source"] = "local"
        entry["next_command"] = ["enrollment-status", "--enrollment-dir", str(path)]
        entry["next_step"] = (
            "Check whether the group already applied the reset. This command only reads its status."
            if purpose == "reset"
            else "Check the service status after chat confirmation. "
            "If confirmed, finish this enrollment and continue the request."
        )
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
                f"- Complete installation ZIP: `{base}/api/agent-skill/lockbot-agent.zip`",
                f"- API docs: [{base}/api/public-docs/agent]({base}/api/public-docs/agent)",
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
        self,
        connection=None,
        *,
        approved_origin=None,
        root=None,
        opener=None,
        clock=time.monotonic,
        sleep=time.sleep,
        legacy_read=False,
        _discovery_cache=None,
    ):
        self._discovery_cache = _discovery_cache
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
        self.legacy_read = legacy_read
        self.api_profile = None
        self.instruction_state = "unknown"
        self._legacy_resource_url = None
        self.manifest = None
        self._discovery_headers = {}
        self._prepared_resources = None
        self._resource_request_started = None
        self._last_mutation = None
        self._panel_home = None
        self._verified_contexts = []

    def _file(self, name):
        value = self.config.get(name)
        if not isinstance(value, str) or not value:
            raise ClientError("missing_" + name)
        path = Path(value)
        return path if path.is_absolute() else self.connection_dir / path

    def _legacy_metadata(self, headers, value=None):
        """Validate additive metadata even on HTTP failures; never claim a loaded revision."""
        if not self.legacy_read:
            return
        if headers.get("X-Lockbot-Service-ID") not in (None, SERVICE):
            raise ClientError("unsupported_service")
        if (
            headers.get("X-Lockbot-API-Version") not in (None, API_VERSION)
            or headers.get("X-Lockbot-API-Compatibility") == "incompatible"
        ):
            raise ClientError("api_version_changed")
        if headers.get("X-Lockbot-Skill-Update") == "update_available":
            self.instruction_state = "changed"
        if not isinstance(value, dict):
            return
        for metadata in (value, value.get("server")):
            if not isinstance(metadata, dict):
                continue
            if metadata.get("service_id", SERVICE) != SERVICE:
                raise ClientError("unsupported_service")
            if (
                metadata.get("api_version", API_VERSION) != API_VERSION
                or metadata.get("api_compatibility") == "incompatible"
            ):
                raise ClientError("api_version_changed")
            pinned = self.config.get("instance_id")
            if pinned and metadata.get("instance_id", pinned) != pinned:
                raise ClientError("instance_mismatch")
        update = value.get("skill_update")
        if update is not None and not isinstance(update, dict):
            raise ClientError("invalid_legacy_update_metadata")
        if isinstance(update, dict):
            if update.get("api_compatibility") == "incompatible":
                raise ClientError("api_version_changed")
            if update.get("status") == "update_available":
                self.instruction_state = "changed"
        # A server's current/unknown response cannot certify instructions loaded locally.

    def _response_bytes(self, response):
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
        return raw

    def _require_modern_action(self):
        if self.legacy_read:
            raise ClientError("legacy_read_only")

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
        _response_headers=None,
    ):
        if self.legacy_read and (
            method != "GET"
            or body is not None
            or self.api_profile != "native-modern-v1"
            and url
            not in {
                self.base + "/api/agent-skill",
                self.base + "/api/agent/context",
                self._legacy_resource_url,
            }
        ):
            raise ClientError("legacy_read_only")
        if self.legacy_read and token and (self.manifest is None or url == self.base + "/api/agent-skill"):
            raise ClientError("legacy_profile_not_verified")
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
            if self.legacy_read:
                headers["X-Lockbot-Client-API-Version"] = API_VERSION
            if self.version and self.api_profile != "native-legacy-read-v1":
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
                if not readonly:
                    self._last_mutation = {"request_attempted": True, "response_received": False, "http_status": None}
                with self.opener.open(request, timeout=min(10, self.deadline - started)) as response:
                    if not readonly:
                        self._last_mutation.update(
                            response_received=True, http_status=getattr(response, "status", None)
                        )
                    self._legacy_metadata(response.headers)
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
                    raw = self._response_bytes(response)
                    if binary:
                        return raw
                    try:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError
                        self._legacy_metadata(response.headers, value)
                        current = (value.get("skill_update") or {}).get("current_version")
                        if (
                            self.version
                            and current
                            and self._version_differs(
                                current, (value.get("skill_update") or {}).get("current_package_version")
                            )
                        ):
                            self.needs_update = True
                        if _response_headers is not None:
                            for name in (
                                "X-Lockbot-Service-ID",
                                "X-Lockbot-API-Version",
                                "X-Lockbot-API-Compatibility",
                                "X-Lockbot-Skill-Version",
                                "X-Lockbot-Package-Version",
                                "X-Lockbot-Skill-Update",
                            ):
                                if response.headers.get(name) is not None:
                                    _response_headers[name] = response.headers[name]
                        return (value, started) if include_started else value
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise ClientError("invalid_json_response") from exc
            except urllib.error.HTTPError as exc:
                if self.legacy_read:
                    try:
                        self._legacy_metadata(exc.headers)
                        raw = self._response_bytes(exc)
                        try:
                            error_body = json.loads(raw)
                        except (ValueError, UnicodeError, RecursionError):
                            error_body = None
                        self._legacy_metadata(exc.headers, error_body)
                    except BaseException:
                        exc.close()
                        raise
                retry = exc.headers.get("Retry-After", "")
                status = exc.code
                if not readonly:
                    self._last_mutation.update(response_received=True, http_status=status)
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
        # Only anonymous, validated descriptors may cross scope boundaries. The
        # cache belongs to one resources_all invocation, never a Client or disk.
        pinned = self.config.get("instance_id")
        cache_key = (self.base, pinned, "legacy-read" if self.legacy_read else "modern")
        cache = self._discovery_cache
        cached = cache.get(cache_key) if cache is not None and isinstance(pinned, str) and pinned else None
        headers = {}
        if cached is None:
            result = self._request(self.base + "/api/agent-skill", _response_headers=headers)
        else:
            result, headers = deepcopy(cached)
        self._validate_manifest(result)
        # Replay additive discovery notices as well as validating the descriptor
        # against this connection, before its Key is opened.
        self._legacy_metadata(headers, result)
        if (
            headers.get("X-Lockbot-API-Version") not in (None, API_VERSION)
            or headers.get("X-Lockbot-API-Compatibility") == "incompatible"
        ):
            raise ClientError("api_version_changed")
        self.manifest = result
        self._discovery_headers = headers
        if cache is not None and isinstance(pinned, str) and pinned and cached is None:
            cache[cache_key] = deepcopy((result, headers))
        return result

    def _validate_manifest(self, result):
        if (
            result.get("service_id") != SERVICE
            or result.get("api_version") != API_VERSION
            or result.get("skill_id")
            not in (("lockbot-agent", "lockbot-resource-agent") if self.legacy_read else ("lockbot-agent",))
            or not isinstance(result.get("instance_id"), str)
            or not result["instance_id"]
            or not re.fullmatch(r"[a-f0-9]{16}", str(result.get("version", "")))
        ):
            raise ClientError("unsupported_service")
        if result["skill_id"] == "lockbot-resource-agent":
            capabilities = result.get("capabilities")
            if (
                result.get("credential_scheme") != "lbr_bot_scoped"
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)
                or not {"credential_context", "namespaced_skill", "skill_version_headers"}.issubset(capabilities)
                or "coherent_resource_snapshot" in capabilities
                or result.get("package_format") is not None
                or result.get("package_version") is not None
                or same_origin_url(self.base, result.get("context_url")) != self.base + "/api/agent/context"
            ):
                raise ClientError("unsupported_legacy_read_profile")
            self.api_profile = "native-legacy-read-v1"
            self.needs_update = True
            self.node_access = None
            self.hardware_notes = None
        else:
            self.api_profile = "native-modern-v1"
        pinned = self.config.get("instance_id")
        if pinned is not None and pinned != result["instance_id"]:
            raise ClientError("instance_mismatch")

    def _version_differs(self, rendered, package=None):
        if self.portable_package and package is not None:
            return package != self.package_version
        return rendered != self.version

    def _check_installed_version(self):
        if self.version is None:
            self.version = installed_version(self.root)
            self.package_version = installed_package_version(self.root)
        if self.api_profile == "native-legacy-read-v1":
            # These hashes certify this CLI package, not the server's Markdown-only ZIP.
            # No legacy instruction revision has been loaded by this executable.
            self.needs_update = True
            return
        self.portable_package = bool(
            self.package_version
            and self.manifest.get("package_format") in (LEGACY_BUNDLE_FORMAT, BUNDLE_FORMAT)
            and re.fullmatch(r"[a-f0-9]{16}", str(self.manifest.get("package_version", "")))
        )
        self.needs_update |= self._version_differs(self.manifest.get("version"), self.manifest.get("package_version"))
        # Discovery precedes loading local revision stamps. Retain its header and
        # JSON notices for this comparison, including when discovery was reused.
        revision = self._discovery_headers.get("X-Lockbot-Skill-Version")
        if revision:
            self.needs_update |= self._version_differs(
                revision, self._discovery_headers.get("X-Lockbot-Package-Version")
            )
        update = self.manifest.get("skill_update") or {}
        if update.get("current_version"):
            self.needs_update |= self._version_differs(update["current_version"], update.get("current_package_version"))

    def _read_context(self, *, initializing=False, with_resources=False):
        if with_resources and self.node_access and not self.legacy_read:
            self.node_access.invalidate()
        if self.manifest is None:
            self.discover()  # no credentials have been read or sent yet
        if not self.config.get("instance_id") or (not initializing and not self.config.get("bot_uuid")):
            raise ClientError("connection_identity_not_pinned")
        if self.api_profile == "native-legacy-read-v1":
            actor = self.config.get("actor")
            if (
                initializing
                or not isinstance(self.config.get("bot_uuid"), str)
                or not isinstance(actor, dict)
                or not all(
                    isinstance(actor.get(field), str) and 0 < len(actor[field]) <= 512
                    for field in ("platform", "external_user_id")
                )
            ):
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
        combined = with_resources and "coherent_resource_snapshot" in self.manifest.get("capabilities", ())
        if combined:
            snapshot, started = self._request(self.base + "/api/agent/resources", token=self.key, include_started=True)
            result = snapshot.get("context")
            if not isinstance(result, dict):
                raise ClientError("resource_context_missing")
        else:
            result = self._request(same_origin_url(self.base, self.manifest["context_url"]), token=self.key)
        self._validate_context(result, initializing=initializing)
        self._verified_contexts.append((result, self._context_digest(result)))
        if combined:
            self._prepared_resources = (result, snapshot, started)
        return result

    def _validate_context(self, result, *, initializing=False):
        if self.api_profile == "native-legacy-read-v1" and (
            not isinstance(result.get("server"), dict)
            or not isinstance(result.get("bot"), dict)
            or "skill" in result
            and not isinstance(result["skill"], dict)
        ):
            raise ClientError("invalid_legacy_context")
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
        if self.api_profile == "native-legacy-read-v1":
            bot = result.get("bot", {})
            if (
                type(bot.get("id")) is not int
                or bot["id"] <= 0
                or bot.get("type") not in {"NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"}
                or self.config.get("bot_type", bot["type"]) != bot["type"]
                or type(result.get("agent_access_enabled")) is not bool
                or not isinstance(result.get("links"), dict)
                or server.get("credential_scheme", "lbr_bot_scoped") != "lbr_bot_scoped"
            ):
                raise ClientError("invalid_legacy_context")
            endpoint = same_origin_url(self.base, result["links"].get("resources"))
            if endpoint not in {
                self.base + "/api/agent/resources",
                self.base + f"/api/bots/{bot['id']}/resources",
            }:
                raise ClientError("unsupported_legacy_resource_endpoint")
            self._legacy_resource_url = endpoint
            result["api_profile"] = self.api_profile
            result["instruction_state"] = self.instruction_state
            result["instruction_refresh_required"] = True
        self.needs_update |= self._version_differs(
            result.get("skill", {}).get("version"), result.get("skill", {}).get("package_version")
        )

    def context(self):
        if self.node_access and not self.legacy_read:
            self.node_access.invalidate()
        return self._read_context()

    def initialize(self, key_file, destination, metrics_origin=None, agent_label=None, *, scope="user"):
        self._require_modern_action()
        if self.config or self.connection_dir is not None:
            raise ClientError("init_requires_new_connection")
        bindings = known_key_scopes(key_file, CatalogScanBudget(self.deadline, self.clock), scope=scope)
        if any(service != self.base for service, _ in bindings):
            raise ClientError("key_origin_mismatch")
        manifest = self.discover()
        if any(instance != manifest["instance_id"] for _, instance in bindings):
            raise ClientError("key_instance_mismatch")
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
            make_private_directory(directory)
            destination = directory / "connection.json"
            try:
                self._save_connection(context, destination)
            except BaseException:
                with suppress(OSError):
                    directory.rmdir()
                raise
        result = {
            "connection_created": True,
            "connection": str(destination),
            "bot": context["bot"],
            "actor": context["actor"],
            "key_unchanged": True,
            "update_required": self.needs_update,
        }

        if "own_binding_recovery" in manifest.get("capabilities", ()):
            try:
                endpoint = self.base + RECOVERY_PATH
                verified = (
                    same_origin_url(self.base, manifest.get("binding_recovery_url")) == endpoint
                    and same_origin_url(self.base, context.get("links", {}).get("binding_recovery")) == endpoint
                )
            except ClientError:
                verified = False
            if verified:
                result["next_command"] = ["--connection", str(destination), "recover", "--scope", scope]
                result["next_step"] = (
                    "For a resource-discovery request, run next_command once, then query the returned connections. "
                    "Do not enroll again or replace the seed."
                )
        return result

    def _save_connection(self, context, destination):
        self.config["bot_uuid"] = context["bot"]["uuid"]
        self.config["actor"] = {field: context["actor"][field] for field in ("platform", "external_user_id")}
        write_private(destination, json.dumps(self.config).encode())

    def recover(self, directory, *, scope="user"):
        self._require_modern_action()
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
        if path is not None:
            try:
                make_private_directory(path)
            except FileExistsError as exc:
                raise ClientError("recovery_directory_exists") from exc
        response = self._request(endpoint, method="POST", token=self.key, readonly=False)
        items = self._recovery_items(response, context)
        actor = self.config["actor"]
        reusable = []
        budget = CatalogScanBudget(self.deadline, self.clock)
        if directory is None:
            catalog = listed_connections(connection_catalog(scope), scan_budget=budget)
            if not catalog["scan"]["complete"]:
                raise ClientError("recovery_catalog_scan_incomplete")
            for locator in catalog["connections"]:
                candidate = Path(locator["connection"])
                try:
                    budget.reserve(candidate, record=False)
                    record = private_json(candidate)
                    reusable.append((candidate, record, connection_scope(record)))
                except CatalogScanTruncated:
                    raise ClientError("recovery_catalog_scan_incomplete") from None
                except (ClientError, OSError, KeyError, ValueError):
                    continue
        connections = []
        for bot, key, current in items:
            entry = {"bot": bot, "status": "existing" if current else "unavailable"}
            if key is not None and not current:
                self.secrets.append(key)
                identity = connection_scope({**self.config, "bot_uuid": bot["uuid"]})
                for candidate, record, claimed in reusable:
                    if claimed != identity:
                        continue
                    try:
                        source = Path(record["key_file"])
                        source = source if source.is_absolute() else candidate.parent / source
                        budget.reserve(source, record=False)
                        material = private_bytes(source).decode().strip()
                        if not re.fullmatch(r"lbr_[A-Za-z0-9_-]{20,200}", material):
                            continue
                        self.secrets.append(material)
                        if secrets.compare_digest(material, key):
                            entry.update(status="existing", connection=str(candidate))
                            break
                    except CatalogScanTruncated:
                        raise ClientError("recovery_catalog_scan_incomplete") from None
                    except (ClientError, OSError, KeyError, ValueError, UnicodeError, TypeError):
                        continue
                if "connection" in entry:
                    connections.append(entry)
                    continue
                if path is None:
                    path = new_catalog_entry(scope)
                    make_private_directory(path)
                target = path / bot["uuid"]
                make_private_directory(target)
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
        self._require_modern_action()
        if self.config:
            raise ClientError("enrollment_requires_new_connection")
        group = enrollment_group(group) if group is not None else None
        label = normalize_agent_label(agent_label) if agent_label is not None else None
        if purpose == "bind" and "chat_credential_challenges" in self.discover().get("capabilities", ()):
            raise ClientError("use_key_challenge")
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
            make_private_directory(path)
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
        bind_command = f"bind {code}"
        return {
            "status": "pending",
            "purpose": purpose,
            "target_group": group,
            "bind_command": bind_command,
            "user_instruction": (
                f"In the group you want to connect, use your own chat account to @mention the LockBot robot "
                f"and send `{bind_command}`."
            ),
            "user_instruction_zh": (f"请用你本人的聊天账号，在要连接的群里 @LockBot 机器人，并发送 `{bind_command}`。"),
            "bind_expires_at": expires_at,
            "enrollment_dir": str(path),
        }

    def check_enrollment(self, path, state, *, finish=False, confirm_bot_name=None):
        self._require_modern_action()
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
        if purpose == "recover" and response.get("outcome") in ("created", "recovered"):
            result["outcome"] = response["outcome"]
        self.secrets.append(key)
        if purpose == "reset":
            result["chat_action"] = "reset_applied"
        if not finish:
            if purpose == "reset":
                result["next_command"] = ["finish-enrollment", "--enrollment-dir", str(path)]
                result["next_step"] = (
                    "The group already applied the reset. If the current request needs access to this service, "
                    "run next_command now. It verifies and saves the already-confirmed sign-in locally."
                )
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

    def inventory(self):
        """One authorized inventory observation includes its identity and rules."""
        context = self._read_context(with_resources=True)
        snapshot = self.resources_with_hardware_notes(context)
        snapshot["inventory_overview"] = inventory_overview(snapshot, context)
        if "context" not in snapshot:
            snapshot = {**snapshot, "context": context, "context_source": "separate_verified_read"}
        return snapshot

    def own_inventory(self, *, exact_lease_times=False, node=None):
        """Project only the verified actor's holdings and queued bookings."""
        context = self._read_context(with_resources=True)
        snapshot = self.resources_with_hardware_notes(context)
        if node is not None:
            matches = [row for row in snapshot.get("resources", []) if isinstance(row, dict) and row.get("id") == node]
            if len(matches) != 1:
                raise ClientError("resource_reference_node_unavailable")
            snapshot = {**snapshot, "resources": matches}
        if not exact_lease_times:
            format_lease_times(snapshot)
        actor = context["actor"]
        holdings, bookings = [], []
        if not isinstance(snapshot.get("resources"), list):
            raise ClientError("invalid_resource_summary")
        for node in snapshot["resources"]:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise ClientError("invalid_resource_summary")
            cards = node.get("devices")
            if cards is not None and not isinstance(cards, list):
                raise ClientError("invalid_resource_summary")
            for unit in cards if cards is not None else [node]:
                if not isinstance(unit, dict) or not isinstance(unit.get("holders"), list):
                    raise ClientError("invalid_resource_summary")
                entry = {
                    "node": node["id"],
                    "alias": node.get("alias"),
                    "device": unit.get("id") if cards is not None else None,
                    "allocation": "card" if cards is not None else "whole_node",
                    "model": unit.get("dev_model") if cards is not None else None,
                    "accelerators": node.get(
                        "accelerators",
                        node.get("local_hardware_note", {}).get("accelerators")
                        if isinstance(node.get("local_hardware_note"), dict)
                        else None,
                    )
                    if cards is None
                    else None,
                }
                for field, destination in (("holders", holdings), ("booking_list", bookings)):
                    rows = unit.get(field, [])
                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                        raise ClientError("invalid_resource_summary")
                    own = self._own(
                        [row for row in rows if row.get("platform", actor["platform"]) == actor["platform"]],
                        actor["external_user_id"],
                    )
                    if own is None:
                        continue
                    if field == "holders":
                        timing = (
                            {key: own.get(key) for key in ("remaining_seconds", "expires_at")}
                            if exact_lease_times
                            else {"remaining_time": own.get("remaining_time")}
                        )
                        destination.append({**entry, "mode": unit.get("lock_status"), **timing})
                    else:
                        position = own.get("position")
                        destination.append(
                            {
                                **entry,
                                # The holder's mode/time do not describe an unclaimed booking.
                                "mode": None,
                                "queue_rank": position + 1 if type(position) is int and position >= 0 else None,
                                "claimable_by_you": unit.get("claimable_by_you")
                                if type(unit.get("claimable_by_you")) is bool
                                else None,
                            }
                        )
        return {
            **{key: value for key, value in snapshot.items() if key != "resources"},
            "context": context,
            "actor": actor,
            "bot_id": context["bot"]["id"],
            "bot_type": context["bot"]["type"],
            "scope": "own",
            "ownership_coverage": ownership_coverage(snapshot, context),
            "holdings": holdings,
            "bookings": bookings,
        }

    def resources(self, context):
        if self.api_profile == "native-legacy-read-v1" and context.get("agent_access_enabled") is not True:
            raise ClientError("agent_access_disabled")
        prepared = self._prepared_resources
        self._prepared_resources = None
        if prepared is not None and prepared[0] is context:
            _, snapshot, started = prepared
        else:
            snapshot, started = self._request(
                same_origin_url(self.base, context["links"]["resources"]), token=self.key, include_started=True
            )
        return self._remember_resources(snapshot, context, started)

    def _remember_resources(self, snapshot, context, started):
        if self.api_profile == "native-legacy-read-v1":
            self._validate_legacy_resources(snapshot, context)
        coherent = "context" in snapshot
        expected = {
            "bot_id": context["bot"]["id"],
            "bot_type": context["bot"]["type"],
            "bot_uuid": context["bot"]["uuid"],
            "instance_id": context["server"]["instance_id"],
            "platform": context["actor"]["platform"],
            "external_user_id": context["actor"]["external_user_id"],
        }
        if any(
            (key in snapshot or coherent and key in {"bot_id", "bot_type"}) and snapshot.get(key) != value
            for key, value in expected.items()
        ):
            raise ClientError("resource_snapshot_identity_mismatch")
        scopes = [snapshot]
        if coherent:
            if not isinstance(snapshot["context"], dict):
                raise ClientError("resource_snapshot_identity_mismatch")
            scopes.append(snapshot["context"])
        for reported_scope in scopes:
            for field, identity in (
                ("actor", context["actor"]),
                ("bot", context["bot"]),
                ("server", context["server"]),
            ):
                if field not in reported_scope and not (coherent and field == "actor"):
                    continue  # Older resource payloads can omit identity fields.
                reported = reported_scope.get(field)
                required = {"platform", "external_user_id"} if coherent and field == "actor" else set()
                if not isinstance(reported, dict) or any(
                    (key in reported or key in required) and reported.get(key) != value
                    for key, value in identity.items()
                    if key
                    in {
                        "platform",
                        "external_user_id",
                        "id",
                        "uuid",
                        "type",
                        "instance_id",
                        "service_id",
                        "api_version",
                    }
                ):
                    raise ClientError("resource_snapshot_identity_mismatch")
        generation = self.node_access.invalidate() if self.node_access else None
        if generation:
            nodes = self._current_node_access(snapshot, context, self.clock() - started)
            scope = {
                "approved_origin": self.base,
                "instance_id": self.config["instance_id"],
                "bot_uuid": context["bot"]["uuid"],
                "actor": {field: context["actor"][field] for field in ("platform", "external_user_id")},
            }
            self.node_access.save(generation, nodes, self.redacted(scope))
        self._resource_request_started = started
        snapshot["snapshot_fresh_for_write"] = (
            self.api_profile != "native-legacy-read-v1" and self._snapshot_fresh_for_write(snapshot, started)
        )
        if self.api_profile == "native-legacy-read-v1":
            snapshot["snapshot_fresh_for_read"] = snapshot.get(
                "captured_at"
            ) is not None and self._snapshot_fresh_for_write(snapshot, started)
            snapshot["api_profile"] = self.api_profile
            snapshot["instruction_state"] = self.instruction_state
            snapshot["instruction_refresh_required"] = True
        return snapshot

    def _validate_legacy_resources(self, snapshot, context):
        rows = snapshot.get("resources")
        if not isinstance(rows, list) or "context" in snapshot:
            raise ClientError("invalid_legacy_resource_snapshot")
        ids = set()
        for node in rows:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"] or node["id"] in ids:
                raise ClientError("invalid_legacy_resource_snapshot")
            ids.add(node["id"])
            cards = node.get("devices")
            card_bot = context["bot"]["type"] in {"DEVICE", "QUEUE_DEVICE"}
            if "devices" in node and not isinstance(cards, list) or card_bot != (cards is not None):
                raise ClientError("invalid_legacy_resource_snapshot")
            devices = set()
            for unit in cards if cards is not None else [node]:
                if not isinstance(unit, dict):
                    raise ClientError("invalid_legacy_resource_snapshot")
                if cards is not None:
                    device = unit.get("id")
                    if (
                        not (type(device) is int and device >= 0 or isinstance(device, str) and bool(device))
                        or str(device) in devices
                    ):
                        raise ClientError("invalid_legacy_resource_snapshot")
                    devices.add(str(device))
                for field in ("holders", "booking_list"):
                    if field in unit and (
                        not isinstance(unit[field], list) or any(not isinstance(row, dict) for row in unit[field])
                    ):
                        raise ClientError("invalid_legacy_resource_snapshot")
        # Timing is optional in this profile. Bad/missing timing remains unknown.
        age = metric_number(snapshot.get("age_seconds"))
        if age is None or age < 0:
            snapshot["age_seconds"] = None
        captured = snapshot.get("captured_at")
        try:
            instant = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError
        except (AttributeError, ValueError, TypeError, OverflowError):
            snapshot["captured_at"] = None

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
        self._require_modern_action()
        """Issue one private handoff without following or consuming its browser ticket."""
        self._panel_home = None
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
        ):
            raise ClientError("native_personal_panel_unavailable")
        account_path = "/api/personal-panel/account-links"
        if management["version"] == 1 and management.get("list_path") == "/api/personal-panel/connections":
            if management.get("account_issue_path") == account_path:
                issue_path = account_path
        elif management["version"] == 2:
            native_handoff = management.get("native_key_handoff")
            if (
                not isinstance(native_handoff, dict)
                or type(native_handoff.get("version")) is not int
                or native_handoff["version"] != 1
                or native_handoff.get("account_issue_path") != account_path
            ):
                raise ClientError("native_personal_panel_unavailable")
            issue_path = account_path
        else:
            raise ClientError("native_personal_panel_unavailable")
        context = self._read_context()
        if context.get("agent_access_enabled") is not True:
            raise ClientError("agent_access_disabled")
        if self.needs_update:
            raise ClientError("update_bundle_before_mutation")
        self._panel_home = home  # Expose only after current context identity was verified.
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
        _selection_precondition=None,
    ):
        self._require_modern_action()
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
        context = self._read_context(with_resources=True)
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
        if not card_bot and devices:
            raise ClientError("group_supports_whole_nodes_only")
        if card_bot != bool(devices) or len(set(devices)) != len(devices):
            raise ClientError("explicit_device_selection_required")
        if any(not re.fullmatch(r"0|[1-9][0-9]{0,4}", device) for device in devices):
            raise ClientError("invalid_device")
        use_group_duration = duration is None and operation in {"lock", "slock", "book"}
        if use_group_duration:
            rules = context.get("bot_rules")
            seconds = rules.get("default_duration_seconds") if isinstance(rules, dict) else None
            if type(seconds) is not int or seconds <= 0:
                raise ClientError("group_default_duration_unavailable")
            ceiling = rules.get("effective_max_lock_duration_seconds")
            if type(ceiling) is int and ceiling >= 0 and seconds > ceiling:
                raise ClientError("group_default_duration_exceeds_limit")
        if operation == "claim":
            if context["bot"]["type"] not in {"QUEUE", "QUEUE_DEVICE"} or len(nodes) != 1 or len(devices) > 1:
                raise ClientError("claim_requires_one_queued_resource")
            if duration is not None:
                raise ClientError("claim_uses_booked_duration")
        elif (
            operation != "release"
            and not use_group_duration
            and (
                not duration
                or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?)[smhd]", duration)
                or float(duration[:-1]) <= 0
            )
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
        if _selection_precondition is not None:
            _selection_precondition(self, context, before)
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
        if _selection_precondition is not None:
            _selection_precondition(self, context, before, final=True)
        started = self.clock()
        result = self._post_command(context, command, metadata)
        try:
            if "coherent_resource_snapshot" in self.manifest.get("capabilities", ()):
                after = result.get("resource_snapshot")
                if not isinstance(after, dict) or not isinstance(after.get("context"), dict):
                    raise ClientError("command_observation_missing")
                self._validate_context(after["context"])
                after = self._remember_resources(after, context, started)
            else:
                after = self.resources(context)
            self._require_fresh_snapshot(after)
            targets = self._selected(after, nodes, devices)
            if _selection_precondition is not None:
                # Revalidate exact target state after dispatch. Do not re-run a
                # precondition that assumes the resource is still unallocated.
                _automatic_resources(
                    {**after, "resources": [row for row in after["resources"] if row["id"] in nodes]}, context
                )
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
                    and all(
                        item.get("lock_status") == ("shared" if wire == "slock" else "exclusive") for item in targets
                    )
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
            **({"duration_source": "group-default"} if use_group_duration else {}),
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
        if value is not None:
            self._require_modern_action()
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
        self._require_modern_action()
        self._last_mutation = None
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
            if self._last_mutation is None or (exc.status is not None and 400 <= exc.status < 500):
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
        indexed = {}
        for row in snapshot.get("resources", []):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise ClientError("invalid_resource_identity")
            if row["id"] in indexed:
                raise ClientError("ambiguous_resource_identity")
            indexed[row["id"]] = row
        result = []
        for node in nodes:
            if node not in indexed:
                raise ClientError("unknown_resource")
            item = indexed[node]
            if devices:
                cards = {}
                for card in item.get("devices", []):
                    if not isinstance(card, dict) or not _summary_identifier(card.get("id")):
                        raise ClientError("invalid_device_identity")
                    if str(card["id"]) in cards:
                        raise ClientError("ambiguous_device_identity")
                    cards[str(card["id"])] = card
                if set(devices) - set(cards):
                    raise ClientError("unknown_device")
                result.extend(cards[device] for device in devices)
            else:
                result.append(item)
        return result

    @staticmethod
    def _context_digest(context):
        return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).digest()

    def _metrics_context(self, context):
        # Reuse is invocation-local and only accepts an unchanged response read by this client.
        verified = next((digest for observed, digest in self._verified_contexts if observed is context), None)
        try:
            if verified is None or self._context_digest(context) != verified:
                raise ClientError("metrics_context_not_verified")
            self._validate_context(context)
            latest, digest = self._verified_contexts[-1]
            if self._context_digest(latest) != digest:
                raise ClientError("metrics_context_not_verified")
            self._validate_context(latest)
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ClientError("metrics_context_not_verified") from exc
        return latest  # A preceding batch may already have refreshed a short grant.

    def metrics(self, nodes, *, context=None):
        context = self.context() if context is None else self._metrics_context(context)
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
                credential = access.get("token")
                if access.get("scope") != "metrics:read" or not isinstance(credential, str) or not credential:
                    raise ClientError("metrics_grant_unavailable")
            elif source.get("accessMode") == "independent":
                credential = private_bytes(self._file("metrics_key_file")).decode().strip()
            else:
                raise ClientError("unsupported_metrics_authentication")
            if credential.startswith(("lbr_", "lbk_", "lbc1.")) or credential == self.key:
                raise ClientError("resource_key_for_metrics_refused")
            self.secrets.append(credential)
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
                    token=credential,
                    method="POST",
                    form=True,
                    body={"query": selector, "time": str(time.time()), "timeout": "4s"},
                )
                source_data = data.get("data")
                vector = source_data.get("result") if isinstance(source_data, dict) else None
                if (
                    data.get("status") != "success"
                    or not isinstance(source_data, dict)
                    or source_data.get("resultType") != "vector"
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
        return redacted_values(value, self.secrets)

    def install(self, destination):
        self._require_modern_action()
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


def redacted_values(value, private_values=()):
    if isinstance(value, dict):
        return {
            redacted_values(key, private_values): (
                "[redacted]"
                if key.lower() in {"credential", "token", "secret", "api_key", "authorization", "proof"}
                else redacted_values(item, private_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted_values(item, private_values) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\b(?:lbr_|lbp_|lbk_)[A-Za-z0-9_-]{20,200}|lbc1\.[\w-]{22}\.[\w-]{43}", "[redacted]", value)
        for secret in private_values:
            value = value.replace(secret, "[redacted]")
    return value


SUMMARY_NODE_LIMIT = 20
SUMMARY_DEVICE_LIMIT = 20
SUMMARY_BYTE_LIMIT = 8192


def _summary_identifier(value):
    return (type(value) is int and value >= 0) or (
        isinstance(value, str) and bool(value) and len(value) <= 256 and value.isprintable()
    )


def _summary_unit_state(unit, *, fresh, queued, valid):
    """Mutually exclusive display categories; queued units may also be held."""
    holders = unit.get("holders")
    queue = unit.get("booking_list", None if queued else [])
    status = unit.get("lock_status")
    if (
        not fresh
        or not valid
        or status not in ("idle", "exclusive", "shared")
        or not isinstance(holders, list)
        or not isinstance(queue, list)
        or any(
            not isinstance(row, dict) or not isinstance(row.get("external_user_id"), str) or not row["external_user_id"]
            for row in holders + queue
        )
        or status == "idle"
        and holders
        or status in ("exclusive", "shared")
        and not holders
        or status == "exclusive"
        and len(holders) != 1
    ):
        return "unknown"
    return "queued" if queue else "busy" if status == "exclusive" else status


def _summary_model(value):
    return (
        value.strip()
        if isinstance(value, str) and value.strip() and len(value) <= 80 and value.strip().isprintable()
        else None
    )


def ownership_coverage(snapshot, context):
    """Missing owner/queue evidence cannot prove the caller has no resources."""
    resources = snapshot.get("resources", [])
    counts = Counter(row.get("id") for row in resources if isinstance(row, dict) and isinstance(row.get("id"), str))
    kind = context.get("bot", {}).get("type")
    card_bot, queued = kind in {"DEVICE", "QUEUE_DEVICE"}, kind in {"QUEUE", "QUEUE_DEVICE"}
    unidentified, unknown_units = 0, int(kind not in {"NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"})
    ambiguous = sorted(node for node, count in counts.items() if count > 1)
    for node in resources:
        identifier = node.get("id") if isinstance(node, dict) else None
        if (
            not isinstance(identifier, str)
            or not identifier
            or len(identifier) > 256
            or not identifier.isprintable()
            or counts[identifier] != 1
        ):
            unidentified += 1
            continue
        cards = node.get("devices")
        if kind not in {"NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"} or (
            card_bot and not isinstance(cards, list) or not card_bot and "devices" in node
        ):
            unknown_units += 1
            continue
        units = cards if card_bot else [node]
        ids = Counter(
            str(unit["id"]) for unit in units if isinstance(unit, dict) and _summary_identifier(unit.get("id"))
        )
        for unit in units:
            if not isinstance(unit, dict):
                unknown_units += 1
                continue
            valid = not card_bot or _summary_identifier(unit.get("id")) and ids[str(unit["id"])] == 1
            state = _summary_unit_state(unit, fresh=True, queued=queued, valid=valid)
            owners = [
                owner
                for field in ("holders", "booking_list")
                for owner in (unit.get(field) if isinstance(unit.get(field), list) else [])
            ]
            bad_platform = any(
                isinstance(owner, dict)
                and "platform" in owner
                and (not isinstance(owner["platform"], str) or not owner["platform"])
                for owner in owners
            )
            duplicate_owners = False
            for field in ("holders", "booking_list"):
                rows = unit.get(field, [])
                if not isinstance(rows, list):
                    continue
                identities = [
                    (owner.get("platform", context.get("actor", {}).get("platform")), owner.get("external_user_id"))
                    for owner in rows
                    if isinstance(owner, dict)
                    and isinstance(owner.get("external_user_id"), str)
                    and isinstance(owner.get("platform", context.get("actor", {}).get("platform")), str)
                ]
                duplicate_owners |= len(identities) != len(set(identities))
            unknown_units += int(state == "unknown" or bad_platform or duplicate_owners)
    return {
        "complete": not unidentified and not unknown_units,
        "unidentified_nodes": unidentified,
        "unknown_units": unknown_units,
        "ambiguous_node_ids": ambiguous,
    }


def own_summary_rows(
    snapshot, context=None, *, reference_factory, nodes=(), model=None, device_limit=SUMMARY_DEVICE_LIMIT
):
    """Bound the verified caller's records without exposing other people's rows."""
    if not isinstance(snapshot, dict) or snapshot.get("scope") != "own":
        raise ClientError("invalid_own_resource_summary")
    if type(device_limit) is not int or not 1 <= device_limit <= MAX_OVERVIEW_ENTRIES:
        raise ClientError("invalid_summary_device_limit")
    context = context if isinstance(context, dict) else snapshot.get("context", {})
    coverage = snapshot.get("ownership_coverage", {})
    if not isinstance(coverage, dict) or any(
        type(coverage.get(field, 0)) is not int or coverage.get(field, 0) < 0
        for field in ("unidentified_nodes", "unknown_units")
    ):
        raise ClientError("invalid_own_resource_summary")
    ambiguous = set(coverage.get("ambiguous_node_ids", []))
    kind = context.get("bot", {}).get("type")
    allocation = "card" if kind in {"DEVICE", "QUEUE_DEVICE"} else "whole_node" if kind in {"NODE", "QUEUE"} else None
    cleaned, unidentified, invalid_units = {}, coverage.get("unidentified_nodes", 0), 0
    for field in ("holdings", "bookings"):
        entries = snapshot.get(field)
        if not isinstance(entries, list):
            raise ClientError("invalid_own_resource_summary")
        keys = Counter(
            (entry.get("node"), str(entry.get("device")))
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("node"), str)
        )
        cleaned[field] = []
        for entry in entries:
            node = entry.get("node") if isinstance(entry, dict) else None
            if not isinstance(node, str) or not node or len(node) > 256 or not node.isprintable():
                unidentified += 1
                continue
            if node in ambiguous:
                continue
            if (
                allocation is None
                or entry.get("allocation") != allocation
                or allocation == "card"
                and not _summary_identifier(entry.get("device"))
                or allocation == "whole_node"
                and entry.get("device") is not None
                or keys[(node, str(entry.get("device")))] != 1
            ):
                invalid_units += 1
                continue
            cleaned[field].append(entry)
    selected = select_inventory({**snapshot, **cleaned}, nodes=nodes, model=model)
    grouped, unknown = {}, set(selected.get("selection", {}).get("unknown_hardware_nodes", []))
    for field in ("holdings", "bookings"):
        for entry in sorted(selected[field], key=lambda item: (item["node"], str(item.get("device")))):
            node = entry["node"]
            row = grouped.setdefault(
                node,
                {
                    "ref": reference_factory(node),
                    "node": node,
                    "unit": allocation,
                    "holding_units": 0,
                    "booking_units": 0,
                    "holdings": [],
                    "bookings": [],
                    "snapshot_fresh": read_snapshot_fresh(snapshot) and snapshot.get("health") == "running",
                },
            )
            alias = entry.get("alias")
            if isinstance(alias, str) and alias:
                row.update(alias=alias[:160], alias_truncated=len(alias) > 160)
            counter = "holding_units" if field == "holdings" else "booking_units"
            row[counter] += 1
            child = {"device": entry.get("device"), "model": _summary_model(entry.get("model"))}
            if allocation == "card" and child["model"] is None:
                unknown.add(node)
            if allocation == "whole_node" and not entry.get("accelerators"):
                unknown.add(node)
            if field == "holdings":
                child["mode"] = entry.get("mode") if entry.get("mode") in {"exclusive", "shared"} else None
                if "remaining_time" in entry:
                    text = entry["remaining_time"]
                    child["remaining_time"] = (
                        text if isinstance(text, str) and len(text) <= 80 and text.isprintable() else None
                    )
                else:
                    seconds, expiry = metric_number(entry.get("remaining_seconds")), entry.get("expires_at")
                    child["remaining_seconds"] = seconds if seconds is not None and seconds >= 0 else None
                    child["expires_at"] = (
                        expiry if isinstance(expiry, str) and len(expiry) <= 80 and expiry.isprintable() else None
                    )
            else:
                position = entry.get("queue_rank")
                child["queue_rank"] = position if type(position) is int and position > 0 else None
                child["claimable_by_you"] = (
                    entry.get("claimable_by_you") if type(entry.get("claimable_by_you")) is bool else None
                )
            if len(row[field]) < device_limit:
                row[field].append(child)
    for row in grouped.values():
        row["holdings_has_more"] = row["holding_units"] > len(row["holdings"])
        row["bookings_has_more"] = row["booking_units"] > len(row["bookings"])
    return {
        "rows": list(grouped.values()),
        "unknown_hardware_nodes": sorted(unknown),
        "unidentified_nodes": unidentified,
        "unknown_ownership_units": max(invalid_units, coverage.get("unknown_units", 0)),
        "projection_complete": coverage.get("complete") is True
        and not coverage.get("unknown_units", 0)
        and not unidentified
        and not invalid_units
        and allocation is not None
        and read_snapshot_fresh(snapshot)
        and snapshot.get("health") == "running",
    }


def node_summary_rows(snapshot, context=None, *, reference_factory, model=None, device_limit=SUMMARY_DEVICE_LIMIT):
    """Project verified observations only; summaries never authorize allocation.

    The callback receives the exact node ID and must bind it to the caller's
    verified connection. Device identifiers come from records, never positions.
    Unknown model nodes are counted separately from definite filtered matches.
    """
    if not isinstance(snapshot, dict):
        raise ClientError("invalid_resource_summary")
    if type(device_limit) is not int or not 1 <= device_limit <= MAX_OVERVIEW_ENTRIES:
        raise ClientError("invalid_summary_device_limit")
    if model is not None and _summary_model(model) is None:
        raise ClientError("invalid_query_model")
    wanted = model.strip().casefold() if model is not None else None
    context = context if isinstance(context, dict) else snapshot.get("context", {})
    context = context if isinstance(context, dict) else {}
    bot = context.get("bot", {})
    bot_type = bot.get("type") if isinstance(bot, dict) else None
    resources = snapshot.get("resources")
    if not isinstance(resources, list):
        raise ClientError("invalid_resource_summary")
    node_ids = Counter(row.get("id") for row in resources if isinstance(row, dict) and isinstance(row.get("id"), str))
    result = {
        "rows": [],
        "unknown_match_nodes": 0,
        "unknown_hardware_nodes": [],
        "unidentified_nodes": 0,
        "projection_complete": True,
    }
    fresh = read_snapshot_fresh(snapshot) and snapshot.get("health") == "running"
    for node in resources:
        identifier = node.get("id") if isinstance(node, dict) else None
        if (
            not isinstance(identifier, str)
            or not identifier
            or len(identifier) > 256
            or not identifier.isprintable()
            or node_ids[identifier] != 1
        ):
            result["unidentified_nodes"] += 1
            continue
        cards = node.get("devices")
        card_bot = bot_type in {"DEVICE", "QUEUE_DEVICE"}
        malformed_cards = card_bot and not isinstance(cards, list)
        inconsistent_type = bot_type not in {"NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"} or (
            not card_bot and "devices" in node
        )
        hardware, matched_ids, idle_ids = {}, [], []
        valid_ids_count, idle_ids_count, unidentified_devices = 0, 0, 0
        states = dict.fromkeys(("idle", "busy", "shared", "queued", "unknown"), 0)
        if card_bot:
            units = cards if isinstance(cards, list) else []
            ids = Counter(
                str(card["id"]) for card in units if isinstance(card, dict) and _summary_identifier(card.get("id"))
            )
            unidentified_devices = sum(
                not isinstance(card, dict) or not _summary_identifier(card.get("id")) or ids[str(card["id"])] != 1
                for card in units
            )
            unknown_model, matched_count, matched_cards = malformed_cards or bool(unidentified_devices), 0, 0
            matching_unidentified = 0
            for card in units:
                card = card if isinstance(card, dict) else {}
                label = _summary_model(card.get("dev_model"))
                if label is None:
                    unknown_model = True
                else:
                    key = label.casefold()
                    hardware.setdefault(key, {"model": label, "count": 0})["count"] += 1
                if wanted is not None and (label is None or label.casefold() != wanted):
                    continue
                matched_count += 1
                matched_cards += 1
                card_id = card.get("id")
                valid = _summary_identifier(card_id) and ids[str(card_id)] == 1 and not inconsistent_type
                category = _summary_unit_state(card, fresh=fresh, queued=bot_type == "QUEUE_DEVICE", valid=valid)
                states[category] += 1
                valid_ids_count += int(valid)
                idle_ids_count += int(valid and category == "idle")
                matching_unidentified += int(not valid)
                if valid and len(matched_ids) < device_limit:
                    matched_ids.append(card_id)
                if valid and category == "idle" and len(idle_ids) < device_limit:
                    idle_ids.append(card_id)
            total_cards = len(units) if not malformed_cards and not unidentified_devices else None
            total_units = total_cards
            matched_count = None if malformed_cards else matched_count
            matched_cards = matched_count
            if matching_unidentified:
                matched_count = matched_cards = None
                total_cards = total_units = None
                unknown_model = True
            if malformed_cards:
                states = dict.fromkeys(states, None)
            definite = wanted is None or any(key == wanted for key in hardware)
        else:
            inventory = node.get("accelerators")
            note = node.get("local_hardware_note")
            if inventory is None and isinstance(note, dict):
                inventory = note.get("accelerators")
            unknown_model = not isinstance(inventory, list)
            total_cards = 0 if isinstance(inventory, list) else None
            matched_cards = 0 if isinstance(inventory, list) else None
            for row in inventory if isinstance(inventory, list) else []:
                row = row if isinstance(row, dict) else {}
                label, count = _summary_model(row.get("model")), row.get("count")
                valid_count = type(count) is int and count > 0
                if label is None or not valid_count:
                    unknown_model = True
                    total_cards = None
                    if label is None or wanted is None or label.casefold() == wanted:
                        matched_cards = None
                    continue
                key = label.casefold()
                hardware.setdefault(key, {"model": label, "count": 0})["count"] += count
                if total_cards is not None:
                    total_cards += count
                if matched_cards is not None and (wanted is None or key == wanted):
                    matched_cards += count
            definite = wanted is None or wanted in hardware
            total_units = 1
            matched_count = 1 if definite else 0
            category = _summary_unit_state(node, fresh=fresh, queued=bot_type == "QUEUE", valid=not inconsistent_type)
            if definite:
                states[category] = 1
        if unknown_model:
            result["unknown_hardware_nodes"].append(identifier)
        if not definite:
            if unknown_model:
                result["unknown_match_nodes"] += 1
            continue
        reference = reference_factory(identifier)
        if not isinstance(reference, str) or not reference:
            raise ClientError("invalid_summary_reference")
        alias = node.get("alias") if isinstance(node.get("alias"), str) else None
        row = {
            "ref": reference,
            "node": identifier,
            "unit": "unknown" if inconsistent_type else "card" if card_bot else "whole_node",
            "hardware": sorted(hardware.values(), key=lambda item: item["model"].casefold())[:device_limit],
            "hardware_has_more": len(hardware) > device_limit,
            "hardware_incomplete": unknown_model,
            "total_units": total_units,
            "matched_units": matched_count,
            "total_cards": total_cards,
            "matched_cards": matched_cards,
            "states": states,
            "snapshot_fresh": fresh,
        }
        if alias:
            row.update(alias=alias[:160], alias_truncated=len(alias) > 160)
        if card_bot:
            row.update(
                matched_device_ids=matched_ids,
                idle_device_ids=idle_ids,
                devices_has_more=valid_ids_count > len(matched_ids),
                idle_devices_has_more=idle_ids_count > len(idle_ids),
                unidentified_devices=unidentified_devices,
                device_inventory_complete=not malformed_cards and unidentified_devices == 0,
            )
        result["rows"].append(row)
    result["projection_complete"] = result["unidentified_nodes"] == 0
    return result


def summary_page(
    rows, *, offset=0, limit=SUMMARY_NODE_LIMIT, byte_limit=SUMMARY_BYTE_LIMIT, scope_complete=True, ensure_ascii=False
):
    """Return a bounded, deterministic local projection, not server pagination.

    A deferred record retains its exact reference in order. An offset is only a
    display position in this supplied observation and has no authorization role.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_OVERVIEW_ENTRIES
        or type(byte_limit) is not int
        or not 1024 <= byte_limit <= MAX_BYTES
        or type(offset) is not int
        or offset < 0
        or type(scope_complete) is not bool
        or type(ensure_ascii) is not bool
    ):
        raise ClientError("invalid_summary_page")
    if not isinstance(rows, list) or offset > len(rows):
        raise ClientError("invalid_summary_page")
    references = []
    for row in rows:
        reference = row.get("ref") if isinstance(row, dict) else None
        if not isinstance(reference, str) or not reference:
            raise ClientError("invalid_summary_reference")
        references.append(reference)
    if len(set(references)) != len(references):
        raise ClientError("duplicate_summary_reference")
    ordered = sorted(rows, key=lambda row: row["ref"])

    def size(value):
        try:
            return len(json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"), allow_nan=False).encode())
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ClientError("invalid_summary_value") from exc

    def envelope(values, position, deferred):
        return {
            "rows": values,
            "displayed_nodes": len(values),
            "matched_nodes": len(ordered) if scope_complete else None,
            "has_more": position < len(ordered),
            "next_offset": position if position < len(ordered) else None,
            "scope_complete": scope_complete,
            "detail_deferred_nodes": deferred,
            "page_kind": "local_observation_projection",
        }

    result = envelope([], offset, 0)
    for position in range(offset, len(ordered)):
        if len(result["rows"]) >= limit:
            break
        row = ordered[position]
        deferred = False
        if size(envelope([row], position + 1, 0)) > byte_limit:
            row = {"ref": row["ref"], "detail_deferred": True, "reason": "row_exceeds_output_budget"}
            deferred = True
        candidate = envelope([*result["rows"], row], position + 1, result["detail_deferred_nodes"] + int(deferred))
        if size(candidate) > byte_limit:
            if not result["rows"]:
                raise ClientError("summary_reference_exceeds_output_budget")
            break
        # Detach nested values so later formatting cannot rewrite the observation.
        candidate["rows"][-1] = json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False))
        result = candidate
    return result


def _query_summary_projection(snapshot, context, reference_factory, *, nodes=(), model=None):
    if snapshot.get("scope") == "own":
        return own_summary_rows(snapshot, context, reference_factory=reference_factory, nodes=nodes, model=model)
    selected = select_inventory(snapshot, nodes=nodes)
    ambiguous = set(selected.get("selection", {}).get("ambiguous_node_ids", []))
    if ambiguous:
        selected = {**selected, "resources": [row for row in selected["resources"] if row.get("id") not in ambiguous]}
    return node_summary_rows(selected, context, reference_factory=reference_factory, model=model)


def query_summary_output(
    output, *, fields=None, limit=SUMMARY_NODE_LIMIT, offset=0, nodes=(), model=None, private_values=()
):
    """An opt-in bounded response; never replaces a snapshot used for a write."""
    value = output.get("result") if isinstance(output, dict) else None
    if not isinstance(value, dict):
        raise ClientError("invalid_resource_summary")
    groups = value.get("groups")
    if groups is None:
        groups = [{"snapshot": value}]
    rows, unknown, unidentified = [], set(), 0
    unknown_ownership_units = 0
    own_sets, own_scope_conflicts = {}, set()
    metrics_sources = []
    raw_observations, raw_conflicts = {}, set()
    complete = value.get("complete", True) is True
    for group in groups:
        snapshot = group.get("snapshot", {})
        context = snapshot.get("context", {})
        server, bot, actor = context.get("server", {}), context.get("bot", {}), context.get("actor")
        approved = group.get("identity", {}).get("origin") or group.get("_approved_origin")
        if approved is None and len(groups) == 1:
            approved = output.get("_approved_origin")
        record = {
            "approved_origin": approved,
            "instance_id": server.get("instance_id"),
            "bot_uuid": bot.get("uuid"),
            "actor": actor,
        }
        if isinstance(snapshot.get("metrics"), dict):
            metrics_sources.append((resource_scope_digest(record), snapshot["metrics"]))

        def reference_factory(node, record=record):
            return resource_reference(record, node, width=64)

        unfiltered = _query_summary_projection(snapshot, context, reference_factory)
        if snapshot.get("scope") == "own":
            # An observed empty own set is evidence too. Comparing only visible
            # rows would hide "owned" versus "not owned" for the same scope.
            scope = resource_scope_digest(record)
            membership = [
                json.dumps([field, entry.get("node"), entry.get("device"), entry.get("allocation")], sort_keys=True)
                for field in ("holdings", "bookings")
                for entry in snapshot[field]
            ]
            digest = hashlib.sha256("\n".join(sorted(membership)).encode()).hexdigest()
            if scope in own_sets and own_sets[scope] != digest:
                own_scope_conflicts.add(scope)
            own_sets[scope] = digest
        for row in unfiltered["rows"]:
            if row["ref"] in raw_observations and raw_observations[row["ref"]] != row:
                raw_conflicts.add(row["ref"])
            raw_observations[row["ref"]] = row
        projected = (
            unfiltered
            if model is None and not nodes
            else _query_summary_projection(snapshot, context, reference_factory, nodes=nodes, model=model)
        )
        rows.extend(projected["rows"])
        unknown.update(
            reference_factory(node)
            for node in set(projected["unknown_hardware_nodes"])
            | set(snapshot.get("selection", {}).get("unknown_hardware_nodes", []))
        )
        unidentified += unfiltered["unidentified_nodes"]
        unknown_ownership_units += unfiltered.get("unknown_ownership_units", 0)
        complete &= unfiltered["projection_complete"]
    # Distinct credentials can observe the same exact scope. Display identical
    # evidence once; conflicting observations supply only a locator, never a
    # union of states, devices or capabilities. Raw source observations stay intact.
    distinct, duplicates, conflicts = {}, 0, set()
    for row in rows:
        reference = row["ref"]
        if reference not in distinct:
            distinct[reference] = row
        else:
            duplicates += 1
            if distinct[reference] != row:
                conflicts.add(reference)
                distinct[reference] = {"ref": reference, "node": row["node"], "observation_conflict": True}
    for reference in raw_conflicts.intersection(distinct):
        conflicts.add(reference)
        distinct[reference] = {"ref": reference, "node": distinct[reference]["node"], "observation_conflict": True}
    for reference, row in distinct.items():
        if any(reference.startswith("s-" + scope + "/") for scope in own_scope_conflicts):
            conflicts.add(reference)
            distinct[reference] = {"ref": reference, "node": row["node"], "observation_conflict": True}
    metric_refs, ambiguous_metrics = set(), False
    for scope, metrics in metrics_sources:
        candidates = [
            row
            for reference, row in distinct.items()
            if reference.startswith("s-" + scope + "/") and not row.get("observation_conflict")
        ]
        for row in merge_summary_metrics(candidates, metrics):
            if row["ref"] in metric_refs:
                ambiguous_metrics = True
                row["metrics"] = {
                    "status": "invalid",
                    "error": "ambiguous_node_metrics",
                    "best_effort": True,
                    "does_not_authorize_resources": True,
                }
            distinct[row["ref"]] = row
            metric_refs.add(row["ref"])
    rows = list(distinct.values())
    complete &= not conflicts and not own_scope_conflicts
    if fields is not None:
        # Exact references remain available even when selecting fewer display fields.
        rows = [{key: item[key] for key in ("ref", "observation_conflict", *fields) if key in item} for item in rows]
    unread = value.get("unread", [])
    notes = {
        "view": value.get("view", "own" if value.get("scope") == "own" else "resources"),
        "unknown_hardware_nodes": len(unknown),
        "unidentified_nodes": unidentified,
        "unknown_ownership_units": unknown_ownership_units,
        "duplicate_observations": duplicates,
        "conflicting_observations": len(conflicts),
        "conflicting_scopes": len(own_scope_conflicts),
        "unread_scopes": sum(item.get("count", 1) for item in unread),
        "unread_reasons": sorted({item.get("error", "unknown") for item in unread})[:8],
        "retry_after_seconds": max(
            (item["retry_after_seconds"] for item in unread if type(item.get("retry_after_seconds")) in (int, float)),
            default=None,
        ),
        "update_required": bool(output.get("update_required") or any(group.get("update_required") for group in groups)),
        "http_requests": value.get("http_requests", output.get("_http_requests")),
        "max_snapshot_age_seconds": max(
            (metric_number(group.get("snapshot", {}).get("age_seconds")) or 0 for group in groups), default=None
        )
        if all(metric_number(group.get("snapshot", {}).get("age_seconds")) is not None for group in groups)
        else None,
    }
    legacy_snapshots = [
        group.get("snapshot", {})
        for group in groups
        if group.get("snapshot", {}).get("api_profile") == "native-legacy-read-v1"
    ]
    if legacy_snapshots:
        notes.update(
            api_profiles=["native-legacy-read-v1"],
            instruction_state="changed"
            if any(snapshot.get("instruction_state") == "changed" for snapshot in legacy_snapshots)
            else "unknown",
            instruction_refresh_required=True,
        )
    if metrics_sources:
        notes.update(
            metrics_requested=True,
            metrics_read_complete=not ambiguous_metrics
            and all(metrics.get("complete") is True for _, metrics in metrics_sources),
        )
    if "scope_page" in value:
        notes["scope_page"] = value["scope_page"]
    # Use the final serializer for both pagination and framing, including Windows
    # ASCII escapes. Reserve metadata, the outer wrapper and the printed newline.
    ascii_json = os.name == "nt"
    allowance = (
        SUMMARY_BYTE_LIMIT - len(json.dumps(notes, ensure_ascii=ascii_json, separators=(",", ":")).encode()) - 32
    )
    if allowance < 1024:
        raise ClientError("summary_output_exceeds_byte_budget")
    page = summary_page(
        rows, offset=offset, limit=limit, byte_limit=allowance, scope_complete=bool(complete), ensure_ascii=ascii_json
    )
    page.update(notes)
    encoded = json.dumps(
        redacted_values({"result": page}, private_values), ensure_ascii=ascii_json, separators=(",", ":")
    )
    if len(encoded.encode()) + 1 > SUMMARY_BYTE_LIMIT:
        raise ClientError("summary_output_exceeds_byte_budget")
    return encoded


SUMMARY_FIELDS = (
    "ref",
    "node",
    "unit",
    "hardware",
    "hardware_has_more",
    "hardware_incomplete",
    "total_units",
    "matched_units",
    "total_cards",
    "matched_cards",
    "states",
    "snapshot_fresh",
    "alias",
    "alias_truncated",
    "matched_device_ids",
    "idle_device_ids",
    "devices_has_more",
    "idle_devices_has_more",
    "unidentified_devices",
    "device_inventory_complete",
    "observation_conflict",
    "holding_units",
    "booking_units",
    "holdings",
    "bookings",
    "holdings_has_more",
    "bookings_has_more",
    "metrics",
)


def inventory_output(snapshot, *, full=False):
    """Project the model-facing view, keeping the verified observation untouched."""
    if full or not isinstance(snapshot, dict):
        return snapshot
    result = dict(snapshot)
    context = snapshot.get("context")
    if isinstance(context, dict):
        result["context"] = {
            key: value for key, value in context.items() if key not in {"skill", "links", "utilization"}
        }
        if isinstance(context.get("server"), dict):
            result["context"]["server"] = {
                key: value
                for key, value in context["server"].items()
                if key in {"service_id", "instance_id", "api_version", "server_version"}
            }
    return result


def aggregate_output(output, *, full=False):
    if full:
        return output
    result = output["result"]
    return {
        **output,
        "result": {
            **result,
            "groups": [{**group, "snapshot": inventory_output(group["snapshot"])} for group in result["groups"]],
        },
    }


def command_output(result, *, full=False):
    """Keep one verified state; retain the server observation identity beside its outcome."""
    if full or not isinstance(result, dict) or result.get("verified") is not True:
        return result
    snapshot = result.get("verified_snapshot")
    outcome = result.get("command_result")
    projected = {**result, "verified_snapshot": inventory_output(snapshot)}
    supplied = outcome.get("resource_snapshot") if isinstance(outcome, dict) else None
    if not isinstance(snapshot, dict) or supplied != snapshot:
        return projected
    context = snapshot.get("context")
    if not isinstance(context, dict):
        return projected
    server, bot = context.get("server"), context.get("bot")
    if not isinstance(server, dict) or not isinstance(bot, dict):
        return projected
    if not all((snapshot.get("captured_at"), snapshot.get("bot_id"), server.get("instance_id"), bot.get("uuid"))):
        return projected
    identity = {key: snapshot[key] for key in ("captured_at", "bot_id", "actor") if key in snapshot}
    identity.update(
        reference="verified_snapshot",
        context={"server": {"instance_id": server["instance_id"]}, "bot": {"uuid": bot["uuid"]}},
    )
    projected["command_result"] = {**outcome, "resource_snapshot": identity}
    return projected


def metrics_output(result, *, full=False):
    # The raw Prometheus vector duplicates the normalized utilization readings.
    return result if full else {key: value for key, value in result.items() if key != "data"}


def select_inventory(snapshot, *, nodes=(), model=None):
    """Filter an observation for display without changing the verified write snapshot."""
    if not nodes and model is None:
        return snapshot
    if any(not isinstance(node, str) or not 0 < len(node) <= 256 or not node.isprintable() for node in nodes):
        raise ClientError("invalid_query_node")
    if model is not None and (not model.strip() or len(model) > 80 or not model.isprintable()):
        raise ClientError("invalid_query_model")
    wanted = {node.casefold() for node in nodes}
    model = model.strip().casefold() if model is not None else None
    unknown, observed, matched = set(), set(), set()
    unknown_details = {}

    def selected(item, *, own=False):
        identifier = str(item.get("node") if own else item.get("id"))
        observed.add(identifier)
        if wanted and not wanted.intersection({identifier.casefold(), str(item.get("alias", "")).casefold()}):
            return False
        if model is not None:
            cards = item.get("devices")
            if own and item.get("model") is not None:
                models = [item["model"]]
            elif isinstance(cards, list):
                models = [card.get("dev_model") if isinstance(card, dict) else None for card in cards]
            else:
                note = item.get("local_hardware_note")
                inventory = item.get("accelerators", note.get("accelerators") if isinstance(note, dict) else None)
                models = (
                    [row.get("model") if isinstance(row, dict) else None for row in inventory]
                    if isinstance(inventory, list)
                    else [None]
                )
            if not models or any(not isinstance(value, str) or not value.strip() for value in models):
                unknown.add(identifier)
                raw_id = item.get("node") if own else item.get("id")
                if isinstance(raw_id, str) and raw_id:
                    unknown_details[identifier] = {
                        "node": identifier,
                        "alias": item.get("alias") if isinstance(item.get("alias"), str) else None,
                        "lock_status": item.get("lock_status"),
                        "requested_model": model,
                        "model_match": "unknown",
                    }
            if model not in {value.strip().casefold() for value in models if isinstance(value, str)}:
                return False
        matched.add(identifier)
        return True

    result = dict(snapshot)
    if snapshot.get("scope") == "own":
        for field in ("holdings", "bookings"):
            result[field] = [item for item in snapshot[field] if selected(item, own=True)]
    else:
        result["resources"] = [item for item in snapshot["resources"] if selected(item)]
        result["inventory_overview"] = inventory_overview(result, snapshot.get("context", {}))
    result["selection"] = {
        "nodes": list(nodes),
        "model": model,
        "observed_nodes": len(observed),
        "matched_nodes": len(matched),
        "unknown_hardware_nodes": sorted(unknown),
        "full_inventory": False,
    }
    if snapshot.get("scope") != "own":
        counts = Counter(row.get("id") for row in snapshot["resources"] if isinstance(row.get("id"), str))
        ambiguous = sorted(node for node, count in counts.items() if count > 1)
        if ambiguous:
            # Filtering must never turn two distinct records into one exact node.
            result["selection"].update(
                ambiguous_node_ids=ambiguous,
                ambiguous_node_records=sum(counts[node] for node in ambiguous),
            )
    excluded_unknown = [unknown_details[node] for node in sorted(unknown_details.keys() - matched)]
    if excluded_unknown:
        result["selection"]["unknown_hardware_resources"] = excluded_unknown
    return result


def merge_summary_metrics(rows, metrics, *, device_limit=20):
    """Attach bounded telemetry to observed rows without changing resource authority."""
    if type(device_limit) is not int or not 1 <= device_limit <= 20:
        raise ClientError("invalid_summary_device_limit")
    metrics = metrics if isinstance(metrics, dict) else {}
    readings = metrics.get("utilization")
    readings = readings if isinstance(readings, list) else []
    indexed = {}
    for reading in readings:
        if isinstance(reading, dict) and isinstance(reading.get("node"), str):
            indexed.setdefault(reading["node"], []).append(reading)

    def sample(value, *, field="percent"):
        value = value if isinstance(value, dict) else {}
        status = value.get("status")
        status = status if status in {"ok", "missing", "stale", "failed", "invalid"} else "invalid"
        number, age = metric_number(value.get(field)), metric_number(value.get("sample_age_seconds"))
        if status == "ok" and (number is None or number < 0 or field == "percent" and number > 100):
            status = "invalid"
        return {
            "status": status,
            field: number if status == "ok" else None,
            "sample_age_seconds": age if age is not None and age >= 0 else None,
        }

    def memory(value):
        value = value if isinstance(value, dict) else {}
        return {
            key: sample(value[key], field="bytes") for key in ("used", "total", "available", "cache") if key in value
        }

    def identifier(value):
        return str(value) if _summary_identifier(value) else None

    result = []
    for row in rows:
        node = row.get("node")
        matches = indexed.get(node, [])
        reading = matches[0] if len(matches) == 1 else {}
        projected = {
            "best_effort": True,
            "does_not_authorize_resources": True,
            "status": "reported" if len(matches) == 1 else "missing" if not matches else "invalid",
        }
        errors = []
        if len(matches) > 1:
            errors.append({"error": "ambiguous_node_metrics"})
        unread = metrics.get("unread")
        for error in unread if isinstance(unread, list) else []:
            if not isinstance(error, dict) or not isinstance(error.get("nodes"), list) or node not in error["nodes"]:
                continue
            code = error.get("error")
            entry = {
                "error": code
                if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,159}", code)
                else "metrics_unavailable"
            }
            if type(error.get("http_status")) is int and 100 <= error["http_status"] <= 599:
                entry["http_status"] = error["http_status"]
            retry = metric_number(error.get("retry_after_seconds"))
            if retry is not None and retry >= 0:
                entry["retry_after_seconds"] = retry
            errors.append(entry)
        if errors:
            projected.update(errors=errors[:20], errors_has_more=len(errors) > 20)
        if reading:
            projected["cpu"] = sample(reading.get("cpu"))
            if "memory" in reading:
                projected["memory"] = memory(reading["memory"])
            allowed = set()
            if row.get("unit") == "card":
                ids = row.get("matched_device_ids")
                if isinstance(ids, list):
                    allowed.update(identifier(value) for value in ids if identifier(value) is not None)
                for field in ("holdings", "bookings"):
                    children = row.get(field)
                    for child in children if isinstance(children, list) else []:
                        device = identifier(child.get("device")) if isinstance(child, dict) else None
                        if device is not None:
                            allowed.add(device)
            cards = reading.get("cards")
            cards = cards if isinstance(cards, list) else []
            counts = Counter(identifier(card.get("device")) for card in cards if isinstance(card, dict))
            selected = []
            for card in cards:
                device = identifier(card.get("device")) if isinstance(card, dict) else None
                if device is None or row.get("unit") != "whole_node" and device not in allowed or counts[device] != 1:
                    continue
                selected.append(
                    {
                        "device": device,
                        **sample(card),
                        **({"memory": memory(card["memory"])} if "memory" in card else {}),
                    }
                )
            status = reading.get("cards_status")
            projected.update(
                cards=selected[:device_limit],
                cards_status=status
                if status in {"reported", "ok", "missing", "stale", "failed", "invalid"}
                else "invalid",
                cards_has_more=len(selected) > device_limit,
                reported_cards=len(selected),
            )
            filesystems = reading.get("filesystems")
            selected = []
            for filesystem in filesystems if isinstance(filesystems, list) else []:
                name = filesystem.get("filesystem") if isinstance(filesystem, dict) else None
                if not isinstance(name, str) or not name or not name.isprintable():
                    continue
                selected.append(
                    {
                        "filesystem": name[:160],
                        "filesystem_truncated": len(name) > 160,
                        **{field: sample(filesystem.get(field), field="bytes") for field in ("used", "total")},
                    }
                )
            if isinstance(filesystems, list):
                projected.update(
                    filesystems=selected[:device_limit],
                    filesystems_has_more=len(selected) > device_limit,
                    reported_filesystems=len(selected),
                )
        result.append({**row, "metrics": projected})
    return result


def attach_metrics(client, snapshot, *, full=False, context=None):
    """Batch only selected nodes; optional telemetry failure never erases ownership."""
    rows = snapshot.get("resources", snapshot.get("holdings", []) + snapshot.get("bookings", []))
    nodes = list(dict.fromkeys(str(row.get("id", row.get("node"))) for row in rows))
    result = {"scope": "selected_nodes", "complete": True, "utilization": [], "unread": []}
    if full:
        result["responses"] = []
    for offset in range(0, len(nodes), 64):
        batch = nodes[offset : offset + 64]
        try:
            reading = (
                client.metrics(batch, context=context)
                if context is not None and isinstance(client, Client)
                else client.metrics(batch)
            )
            result["utilization"].extend(reading["utilization"])
            if full:
                result["responses"].append(reading)
        except ClientError as error:
            result["complete"] = False
            result["unread"].append({"nodes": batch, "error": error.code})
            if error.status is not None:
                result["unread"][-1]["http_status"] = error.status
            if error.retry_after_seconds is not None:
                result["unread"][-1]["retry_after_seconds"] = error.retry_after_seconds
            # A failed scope, source or request budget is not a reason to keep polling.
            if offset + len(batch) < len(nodes):
                result["unread"].append({"nodes": nodes[offset + len(batch) :], "error": "metrics_not_read"})
            break
        except (AttributeError, KeyError, TypeError, ValueError):
            result["complete"] = False
            result["unread"].append({"nodes": batch, "error": "invalid_metrics_response"})
            if offset + len(batch) < len(nodes):
                result["unread"].append({"nodes": nodes[offset + len(batch) :], "error": "metrics_not_read"})
            break
    return {**snapshot, "metrics": result}


def attach_summary_metrics(client, snapshot, *, limit=SUMMARY_NODE_LIMIT, offset=0, nodes=(), model=None):
    """Read telemetry only for the bounded display prefix of one verified scope."""
    preview = json.loads(
        query_summary_output(
            {"result": snapshot, "_approved_origin": client.base},
            fields=[],
            limit=limit,
            offset=offset,
            nodes=nodes,
            model=model,
            private_values=client.secrets,
        )
    )["result"]
    wanted = {parse_resource_reference(row["ref"])[1] for row in preview["rows"] if not row.get("observation_conflict")}
    fields = ("holdings", "bookings") if snapshot.get("scope") == "own" else ("resources",)
    selected = {
        **snapshot,
        **{
            field: [row for row in snapshot[field] if row.get("node" if field != "resources" else "id") in wanted]
            for field in fields
        },
    }
    attached = attach_metrics(client, selected, context=snapshot.get("context"))
    return {**snapshot, "metrics": attached["metrics"]}


def observation_at_completion(client, snapshot):
    """Age a single query after optional telemetry; aggregation has its own final pass."""
    result = dict(snapshot)
    age = metric_number(snapshot.get("age_seconds"))
    started = client._resource_request_started
    elapsed = client.clock() - started if started is not None else None
    result["age_seconds"] = (
        age + elapsed if age is not None and age >= 0 and elapsed is not None and elapsed >= 0 else None
    )
    result["snapshot_fresh_for_write"] = bool(
        snapshot.get("snapshot_fresh_for_write")
        and result["age_seconds"] is not None
        and result["age_seconds"] <= MAX_RESOURCE_SNAPSHOT_AGE
    )
    if "snapshot_fresh_for_read" in snapshot:
        result["snapshot_fresh_for_read"] = bool(
            snapshot["snapshot_fresh_for_read"]
            and result["age_seconds"] is not None
            and result["age_seconds"] <= MAX_RESOURCE_SNAPSHOT_AGE
        )
    if "inventory_overview" in result:
        result["inventory_overview"] = inventory_overview(result, snapshot.get("context", {}))
    return result


def scope_cursor_start(token, fingerprint, total):
    """A read traversal position, never evidence of globally fresh uniqueness."""
    if token is None:
        return 0
    match = re.fullmatch(r"sc1\.([0-9a-f]{64})\.(0|[1-9][0-9]{0,3})", token) if isinstance(token, str) else None
    if match is None:
        raise ClientError("invalid_scope_cursor")
    if match[1] != fingerprint:
        raise ClientError("scope_cursor_catalog_changed")
    position = int(match[2])
    if position > total:
        raise ClientError("invalid_scope_cursor")
    return position


def resources_all(
    directory=None,
    *,
    mine=False,
    exact_lease_times=False,
    nodes=(),
    model=None,
    with_metrics=False,
    full=False,
    summary=False,
    after_scope=None,
    legacy_read=False,
    root=None,
    opener=None,
    clock=time.monotonic,
    _completed_at=None,
):
    """Read scopes independently, reusing only invocation-local anonymous discovery."""
    discovery_cache = {}
    deadline, calls, clients = clock() + 60, 0, []
    observations = []
    snapshots, unread = [], []
    verified, coalesced = {}, 0
    scan_budget = CatalogScanBudget(deadline, clock)
    descriptors = []

    def observe_record(path, record):
        actor = record.get("actor")
        actor = actor if isinstance(actor, dict) else {}
        # Only locator/provenance metadata. Constructing a cursor never opens a Key.
        descriptors.append(
            [
                str(path),
                record.get("approved_origin"),
                record.get("instance_id"),
                record.get("bot_uuid"),
                actor.get("platform"),
                actor.get("external_user_id"),
                record.get("key_file"),
            ]
        )

    def forget_discovery(client):
        if client:
            # A replacement server or contradictory protected response must
            # force fresh discovery before another Key at this origin.
            for cache_key in list(discovery_cache):
                if cache_key[0] == client.base:
                    del discovery_cache[cache_key]

    def gap(item):
        if len(unread) < MAX_CALLS:
            unread.append(item)
        elif unread[-1].get("error") == "additional_unread_records":
            unread[-1]["count"] += item.get("count", 1)
        else:
            unread.append({"error": "additional_unread_records", "count": item.get("count", 1)})

    try:
        catalog = listed_connections(directory, scan_budget=scan_budget, _observe_record=observe_record)
    except (ClientError, OSError, ValueError, KeyError, TypeError, RuntimeError):
        catalog = {"connections": [], "pending_enrollments": [], "unavailable_count": 1}
    scan = catalog.get("scan", {**scan_budget.summary(), "complete": False})
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                "scope-cursor-v1",
                descriptors,
                catalog["connections"],
                catalog["pending_enrollments"],
                catalog["unavailable_count"],
            ],
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if after_scope is not None and not scan["complete"]:
        raise ClientError("scope_cursor_catalog_incomplete")
    start_index = scope_cursor_start(after_scope, fingerprint, len(catalog["connections"]))
    next_index = start_index
    if not scan["complete"]:
        gap({"error": "catalog_scan_incomplete", "scan": scan, "remaining_records": "unknown"})
    if catalog["unavailable_count"]:
        gap({"error": "catalog_records_unavailable", "count": catalog["unavailable_count"]})
    for pending in catalog["pending_enrollments"]:
        gap({"enrollment": pending, "error": "enrollment_unfinished"})
    if not catalog["connections"] and not unread:
        unread.append({"error": "no_saved_connections"})
    for index in range(start_index, len(catalog["connections"])):
        locator = catalog["connections"][index]
        if clock() >= deadline:
            gap({"error": "request_budget_exhausted", "count": len(catalog["connections"]) - index})
            break
        client = None
        next_index = index + 1
        try:
            client = Client(
                locator["connection"],
                root=root,
                opener=opener,
                clock=clock,
                legacy_read=legacy_read,
                _discovery_cache=discovery_cache,
            )
            clients.append(client)
            client.calls, client.deadline = calls, deadline
            # Catalog aggregation does not invalidate/save local address caches.
            client.node_access = None
            if calls >= MAX_CALLS:
                gap({"error": "request_budget_exhausted", "count": len(catalog["connections"]) - index})
                next_index = index
                break
            client.discover()  # Anonymous validation always precedes opening this Key.
            protected_reads = 1 if "coherent_resource_snapshot" in client.manifest.get("capabilities", ()) else 2
            if client.calls + protected_reads > MAX_CALLS or clock() >= deadline:
                # Do not half-start a separate context/resource pair or advance
                # the continuation past a scope whose protected read never began.
                gap({"error": "request_budget_exhausted", "count": len(catalog["connections"]) - index})
                next_index = index
                break
            try:
                identity = connection_scope(client.config)
            except ClientError:
                identity = None
            if identity in verified:
                # Only an already online-verified identical scope gates this
                # local comparison. Protected context is never shared across Keys.
                try:
                    budget_path = client._file("key_file")
                    scan_budget.reserve(budget_path, record=False)
                    material = private_bytes(budget_path).decode().strip()
                    duplicate = None
                    if re.fullmatch(r"lbr_[A-Za-z0-9_-]{20,200}", material):
                        client.secrets.append(material)
                        duplicate = next(
                            (group for key, group in verified[identity] if secrets.compare_digest(key, material)), None
                        )
                except (ClientError, OSError, UnicodeError):
                    duplicate = None
                if duplicate is not None:
                    duplicate.setdefault("equivalent_connections", [duplicate["connection"]]).append(locator)
                    coalesced += 1
                    continue
            if mine:
                # Keep precise source timing until every group has been read.
                snapshot = client.own_inventory(exact_lease_times=True)
                context = snapshot["context"]
            else:
                context = client._read_context(with_resources=True)
                snapshot = client.resources_with_hardware_notes(context)
                if not isinstance(snapshot.get("resources"), list):
                    raise ClientError("invalid_resource_summary")
                if "context" not in snapshot:
                    snapshot = {**snapshot, "context": context, "context_source": "separate_verified_read"}
            if not summary:
                snapshot = select_inventory(snapshot, nodes=nodes, model=model)
            if with_metrics:
                snapshot = attach_metrics(client, snapshot, full=full)
            snapshots.append(
                {
                    "connection": locator,
                    "identity": {
                        "origin": client.base,
                        "instance_id": context["server"]["instance_id"],
                        "bot": context["bot"],
                        "actor": context["actor"],
                    },
                    "snapshot": snapshot,
                    "update_required": client.needs_update,
                }
            )
            observations.append((client, snapshot, locator, context))
            verified.setdefault(connection_scope(client.config), []).append((client.key, snapshots[-1]))
        except CatalogScanTruncated:
            gap({"error": "catalog_scan_incomplete", "scan": scan_budget.summary(), "remaining_records": "unknown"})
            break
        except ClientError as error:
            forget_discovery(client)
            gap(
                {
                    "connection": locator,
                    "error": error.code,
                    **(
                        {"retry_after_seconds": error.retry_after_seconds}
                        if error.retry_after_seconds is not None
                        else {}
                    ),
                }
            )
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OSError):
            forget_discovery(client)
            gap({"connection": locator, "error": "invalid_server_or_local_data"})
        finally:
            if client:
                calls = client.calls
    completed_at = clock()
    if _completed_at is not None:
        _completed_at.append(completed_at)
    if "scan" in catalog:
        scan = scan_budget.summary()
    exhausted = next_index >= len(catalog["connections"])
    scope_page = {
        "prior_connections": start_index,
        "attempted_connections": next_index - start_index,
        "exhausted": exhausted,
        "next_after_scope": f"sc1.{fingerprint}.{next_index}" if scan["complete"] and not exhausted else None,
        "resumable_catalog": scan["complete"],
    }
    for client, snapshot, locator, context in observations:
        age = metric_number(snapshot.get("age_seconds"))
        started = client._resource_request_started
        elapsed = completed_at - started if started is not None else None
        snapshot["age_seconds"] = (
            age + elapsed if age is not None and age >= 0 and elapsed is not None and elapsed >= 0 else None
        )
        snapshot["snapshot_fresh_for_write"] = bool(
            snapshot.get("snapshot_fresh_for_write")
            and snapshot["age_seconds"] is not None
            and snapshot["age_seconds"] <= MAX_RESOURCE_SNAPSHOT_AGE
        )
        if "snapshot_fresh_for_read" in snapshot:
            snapshot["snapshot_fresh_for_read"] = bool(
                snapshot["snapshot_fresh_for_read"]
                and snapshot["age_seconds"] is not None
                and snapshot["age_seconds"] <= MAX_RESOURCE_SNAPSHOT_AGE
            )
        if not read_snapshot_fresh(snapshot):
            gap({"connection": locator, "error": "stale_or_unverifiable_snapshot"})
        if not exact_lease_times:
            if mine:
                format_lease_times(
                    {"age_seconds": snapshot["age_seconds"], "resources": [{"holders": snapshot["holdings"]}]}
                )
            else:
                format_lease_times(snapshot)
        if not mine:
            snapshot["inventory_overview"] = inventory_overview(snapshot, context)
    return redacted_values(
        {
            "result": {
                "scope": "saved_connections_only",
                "view": "own" if mine else "resources",
                "complete": not unread and after_scope is None and exhausted,
                "scope_page": scope_page,
                "groups": snapshots,
                "unread": unread,
                "catalog_connections": len(catalog["connections"]),
                "coalesced_catalog_connections": coalesced,
                "catalog_count_exact": scan["complete"] and catalog["unavailable_count"] == 0,
                "catalog_scan": scan,
                "http_requests": calls,
            },
            "update_required": any(client.needs_update for client in clients),
        },
        [value for client in clients for value in client.secrets],
    )


def _automatic_resources(snapshot, context):
    """Validate full observations; a compact overview cannot prove uniqueness."""
    resources = snapshot.get("resources")
    kind = context.get("bot", {}).get("type")
    if (
        snapshot.get("health") != "running"
        or context.get("agent_access_enabled") is not True
        or not isinstance(kind, str)
        or kind not in {"NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"}
    ):
        raise AutomaticScopeError("automatic_scope_unhealthy", "资源状态暂时无法确认，请稍后再试。")
    card_bot = kind in {"DEVICE", "QUEUE_DEVICE"}
    queued = kind in {"QUEUE", "QUEUE_DEVICE"}
    if not isinstance(resources, list):
        raise AutomaticScopeError("automatic_inventory_invalid", "资源列表不完整，无法确定目标。")
    operations = snapshot.get("supported_operations")
    if not isinstance(operations, list) or any(not isinstance(item, str) or not item for item in operations):
        raise AutomaticScopeError("automatic_operations_invalid", "资源操作范围无法确认，未发送命令。")
    try:
        captured = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
        if captured.utcoffset() is None:
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise AutomaticScopeError("automatic_snapshot_unverifiable", "资源观察时间无法确认，未发送命令。") from None
    indexed = {}
    for resource in resources:
        identifier = resource.get("id") if isinstance(resource, dict) else None
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[^\s,\x00-\x1f\x7f]+", identifier)
            or identifier in indexed
            or ("alias" in resource and resource["alias"] is not None and not isinstance(resource["alias"], str))
        ):
            raise AutomaticScopeError("automatic_inventory_invalid", "资源标识有冲突，无法确定目标。")
        cards = resource.get("devices")
        if card_bot and (not isinstance(cards, list) or not cards) or not card_bot and "devices" in resource:
            raise AutomaticScopeError("automatic_inventory_invalid", "资源分配方式暂时无法确认。")
        units = cards if card_bot else [resource]
        seen = set()
        for unit in units:
            device = unit.get("id") if isinstance(unit, dict) else None
            if card_bot and (
                type(device) not in (str, int)
                or not re.fullmatch(r"0|[1-9][0-9]{0,4}", str(device))
                or str(device) in seen
            ):
                raise AutomaticScopeError("automatic_inventory_invalid", "卡标识有冲突，无法确定完整选择。")
            if card_bot:
                seen.add(str(device))
            holders = unit.get("holders")
            bookings = unit.get("booking_list", None if queued else [])
            state = unit.get("lock_status")
            if (
                not isinstance(state, str)
                or state not in {"idle", "exclusive", "shared"}
                or not isinstance(holders, list)
                or not isinstance(bookings, list)
                or any(
                    not isinstance(row, dict)
                    or not isinstance(row.get("external_user_id"), str)
                    or not row["external_user_id"]
                    for row in holders + bookings
                )
                or (state == "idle" and holders)
                or (state in {"exclusive", "shared"} and not holders)
                or (state == "exclusive" and len(holders) != 1)
            ):
                raise AutomaticScopeError("automatic_inventory_invalid", "占用或队列状态不完整，无法安全操作。")
        indexed[identifier] = resource
    return indexed


def _automatic_addresses(resource):
    addresses = set()
    for unit in (resource, *resource.get("devices", [])):
        access = _node_address(unit.get("node_access"))
        if access:
            for field in ("ip", "host"):
                with suppress(ValueError):
                    addresses.add(str(ipaddress.IPv4Address(access.get(field))))
    return addresses


def _automatic_ipv4_alias(resource):
    """An IPv4-shaped public label is a selector, never node-access evidence."""
    alias = resource.get("alias")
    if isinstance(alias, str):
        with suppress(ValueError):
            address = str(ipaddress.IPv4Address(alias))
            if address == alias:
                return address
    return None


def _automatic_match(resource, hint):
    kinds = []
    if resource["id"].casefold() == hint.casefold():
        kinds.append("id")
    if isinstance(resource.get("alias"), str) and resource["alias"].casefold() == hint.casefold():
        kinds.append("alias")
    octet = re.fullmatch(r"0|[1-9][0-9]{0,2}", hint) and int(hint) <= 255
    public_alias = _automatic_ipv4_alias(resource)
    if octet and public_alias is not None and public_alias.rsplit(".", 1)[1] == hint:
        kinds.append("public_ipv4_alias_octet")
    addresses = _automatic_addresses(resource)
    if hint in addresses:
        kinds.append("observed_ipv4")
    elif octet and any(address.rsplit(".", 1)[1] == hint for address in addresses):
        kinds.append("observed_ipv4_octet")
    return kinds


def _automatic_scope_identity(identity):
    return (
        origin(identity["origin"]),
        identity["instance_id"],
        identity["bot"]["uuid"],
        identity["actor"]["platform"],
        identity["actor"]["external_user_id"],
    )


def _automatic_selector_evidence(resource, hint):
    kinds = _automatic_match(resource, hint)
    addresses = tuple(
        sorted(
            address
            for address in _automatic_addresses(resource)
            if address == hint or "observed_ipv4_octet" in kinds and address.rsplit(".", 1)[1] == hint
        )
    )
    return kinds, resource.get("alias") if {"alias", "public_ipv4_alias_octet"}.intersection(kinds) else None, addresses


def _automatic_policy(client, context, snapshot, selected, args):
    rules = context.get("bot_rules", {})
    if not isinstance(rules, dict):
        raise AutomaticScopeError("automatic_policy_invalid", "资源规则暂时无法确认，未发送命令。")
    if any(
        field in rules and (type(rules[field]) is not int or rules[field] < -1)
        for field in ("max_resources_per_user", "effective_max_lock_duration_seconds")
    ):
        raise AutomaticScopeError("automatic_policy_invalid", "资源限制无法确认，未发送命令。")
    cap = rules.get("max_resources_per_user")
    if args.operation in {"lock", "slock", "book"} and type(cap) is int and cap >= 0:
        claimed = snapshot.get("resource_policy", {}).get("claimed_resource_count")
        if type(claimed) is not int or claimed < 0 or claimed + len(selected) > cap:
            raise AutomaticScopeError("automatic_resource_limit", "无法确认完整申请满足当前资源数量限制。")
    ceiling = rules.get("effective_max_lock_duration_seconds")
    if args.operation in {"release", "claim"}:
        # Claim duration is deliberately not exposed by booking snapshots. The
        # existing claim guard sends no duration and preserves the server's booking.
        return
    if args.duration is None:
        seconds = rules.get("default_duration_seconds")
    else:
        seconds = float(args.duration[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[args.duration[-1]]
    actor = context["actor"]["external_user_id"]
    for unit in selected:
        total = seconds
        if args.operation == "renew":
            holding = client._own(unit["holders"], actor)
            try:
                remaining = client._expiry(holding) - client._expiry({"expires_at": snapshot["captured_at"]})
                remaining -= snapshot["age_seconds"] + client.clock() - client._resource_request_started
                if remaining <= 0:
                    raise ValueError
                total += remaining
            except (KeyError, TypeError, ValueError):
                raise AutomaticScopeError(
                    "automatic_lease_unverifiable", "无法确认当前租期，未发送续期命令。"
                ) from None
        if (
            type(total) not in (float, int)
            or not math.isfinite(total)
            or total <= 0
            or (type(ceiling) is int and ceiling >= 0 and total > ceiling)
        ):
            raise AutomaticScopeError("automatic_duration_limit", "所需租期无法满足当前时长限制。")


def _automatic_relock_plan(context, snapshot, selected, args, elapsed):
    """Translate only a new explicit, same-mode request on a fully owned lease."""
    plan = {"operation": args.operation, "duration": args.duration, "leases": None}
    if args.operation not in {"lock", "slock"}:
        return plan
    actor = context["actor"]["external_user_id"]
    owned = [[row for row in unit["holders"] if row["external_user_id"] == actor] for unit in selected]
    if not any(owned):
        return plan
    mode = "exclusive" if args.operation == "lock" else "shared"
    if any(len(rows) != 1 or unit["lock_status"] != mode for rows, unit in zip(owned, selected, strict=True)):
        raise AutomaticScopeError(
            "automatic_relock_selection_conflict", "目标并非全部由你以相同模式持有，未拆分申请或续期。"
        )
    rules = context.get("bot_rules")
    if not isinstance(rules, dict) or rules.get("forbid_relock") is not False:
        raise AutomaticScopeError("automatic_relock_not_allowed", "尚未确认当前规则允许续期，未发送命令。")
    ceiling = rules.get("effective_max_lock_duration_seconds")
    if type(ceiling) is not int or ceiling < -1:
        raise AutomaticScopeError("automatic_policy_invalid", "当前续期时长限制无法确认，未发送命令。")
    if any(unit.get("booking_list", []) for unit in selected):
        raise AutomaticScopeError("automatic_resource_conflict", "目标存在排队，未自动延长当前占用。")
    leases = []
    try:
        reference = Client._expiry({"expires_at": snapshot["captured_at"]}) + snapshot["age_seconds"] + elapsed
        for (holding,) in owned:
            expiry = Client._expiry(holding)
            remaining = metric_number(holding.get("remaining_seconds"))
            if not math.isfinite(expiry) or expiry <= reference or remaining is None or remaining <= 0:
                raise ValueError
            leases.append((expiry, holding.get("start_time")))
    except (ClientError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise AutomaticScopeError(
            "automatic_lease_unverifiable", "无法确认原占用仍在有效期内，未发送续期命令。"
        ) from None
    duration = args.duration
    if duration is None:
        default = rules.get("default_duration_seconds")
        if type(default) is not int or default <= 0:
            raise AutomaticScopeError("group_default_duration_unavailable", "无法确认默认续期时长，请指定增加多久。")
        # Resource commands accept minutes/hours/days, not a seconds suffix.
        # Preserve arbitrary integer defaults without truncating a fractional minute.
        try:
            minutes = default / 60
            if minutes * 60 < default:
                minutes = math.nextafter(minutes, math.inf)
            text = format(minutes, ".17f").rstrip("0").rstrip(".")
            if not math.isfinite(minutes) or int(float(text) * 60) != default:
                raise ValueError
        except (OverflowError, ValueError):
            raise AutomaticScopeError(
                "group_default_duration_unavailable", "默认续期时长无法精确表达，请指定增加多久。"
            ) from None
        duration = text + "m"
    return {"operation": "renew", "duration": duration, "leases": leases}


def resolve_command_scope(args, *, clock=None):
    """Resolve literal caller hints, never infer or authenticate the user's intent."""
    clock = clock or time.monotonic
    deadline = clock() + 60
    hints = args.node
    if (
        not hints
        or len(hints) > 64
        or len(set(hints)) != len(hints)
        or any(not hint or len(hint) > 256 or not hint.isprintable() or hint != hint.strip() for hint in hints)
    ):
        raise AutomaticScopeError("explicit_unique_node_hints_required", "请给出明确且不重复的节点标识。")
    finished = []
    catalog = resources_all(exact_lease_times=True, clock=clock, _completed_at=finished)
    result = catalog["result"]
    budget = {"http_limit": MAX_CALLS, "used_http_requests": result["http_requests"], "deadline_seconds": 60}
    if (
        result.get("complete") is not True
        or result.get("unread")
        or result.get("catalog_count_exact") is not True
        or catalog.get("update_required")
    ):
        raise AutomaticScopeError(
            "automatic_scope_incomplete",
            "部分资源未读到或状态过旧，尚不能唯一确定目标。",
            request_budget=budget,
        )
    completed = finished[0]
    groups = result["groups"]
    inventories = [_automatic_resources(group["snapshot"], group["snapshot"]["context"]) for group in groups]
    matches = []
    for hint in hints:
        candidates = []
        for index, (group, inventory) in enumerate(zip(groups, inventories, strict=True)):
            if args.group is not None and group["identity"]["bot"]["name"] != args.group:
                continue
            for resource in inventory.values():
                kinds = _automatic_match(resource, hint)
                if kinds:
                    candidates.append((index, resource["id"], kinds))
        if len(candidates) != 1:
            public = [
                {"group": groups[index]["identity"]["bot"]["name"], "node": node, "scope": groups[index]["identity"]}
                for index, node, _ in candidates[:8]
            ]
            raise AutomaticScopeError(
                "automatic_target_ambiguous" if candidates else "automatic_target_missing",
                "找到多个匹配，请指定其中一个。" if candidates else "没有找到可验证的匹配，请给节点名或别名。",
                hint=hint,
                candidates=public,
                candidate_count=len(candidates),
            )
        matches.append(candidates[0])
    if len({index for index, _, _ in matches}) != 1:
        raise AutomaticScopeError("automatic_cross_scope_selection", "这些目标属于不同资源范围，请分别明确选择。")
    nodes = [node for _, node, _ in matches]
    if len(set(nodes)) != len(nodes):
        raise AutomaticScopeError("automatic_duplicate_target", "多个标识指向同一节点，请明确所需目标。")
    index = matches[0][0]
    group, inventory = groups[index], inventories[index]
    expected_scope = _automatic_scope_identity(group["identity"])
    card_bot = group["identity"]["bot"]["type"] in {"DEVICE", "QUEUE_DEVICE"}
    devices = list(args.device)
    if card_bot:
        if args.whole_node:
            raise AutomaticScopeError("automatic_resource_mode_mismatch", "该目标按卡分配，不能改为整机申请。")
        if not devices:
            if args.operation not in {"lock", "slock", "book"}:
                raise AutomaticScopeError("unsupported_automatic_operation", "此按卡操作需要明确卡标识。")
            devices = [str(card["id"]) for card in inventory[nodes[0]]["devices"]]
            if any({str(card["id"]) for card in inventory[node]["devices"]} != set(devices) for node in nodes):
                raise AutomaticScopeError(
                    "automatic_device_sets_differ", "各节点的卡集合不同，无法作为一条完整命令执行。"
                )
    elif devices:
        raise AutomaticScopeError("automatic_resource_mode_mismatch", "该目标按整机分配，不能改为卡申请。")
    if devices and len(nodes) > 1 and not args.same_devices_on_all_nodes:
        raise AutomaticScopeError(
            "automatic_multinode_devices_unspecified", "多个节点需要明确相同卡集合，不能自动拆分。"
        )
    if len(set(devices)) != len(devices) or any(not re.fullmatch(r"0|[1-9][0-9]{0,4}", device) for device in devices):
        raise AutomaticScopeError("automatic_device_selection_invalid", "请给出明确且不重复的卡标识。")
    if args.device_count is not None and args.device_count != len(nodes) * len(devices):
        raise AutomaticScopeError("automatic_device_count_mismatch", "完整选择与所需卡数不一致，未缩小或替换目标。")
    for node in nodes:
        if devices and set(devices) - {str(card["id"]) for card in inventory[node]["devices"]}:
            raise AutomaticScopeError("automatic_device_missing", "所需卡不在该节点中，未替换为其他卡。")
    signatures = {
        node: sorted((str(card["id"]), card.get("dev_model")) for card in inventory[node].get("devices", []))
        for node in nodes
    }
    selector_evidence = [
        _automatic_selector_evidence(inventory[node], hint) for hint, node in zip(hints, nodes, strict=True)
    ]
    evidence = {
        "scope": "saved_connections_only",
        "complete": True,
        "observed_scopes": len(groups),
        "selected_scope": group["identity"],
        "matches": [
            {"hint": hint, "node": node, "match_kinds": kinds}
            for hint, (_, node, kinds) in zip(hints, matches, strict=True)
        ],
        "resolved_devices": devices,
        "selection_checked_before_post": False,
    }
    client = Client(group["connection"]["connection"], clock=clock)
    client.node_access = None  # All automatic observations remain in memory, including selected pre-reads.
    if connection_scope(client.config) != expected_scope:
        raise AutomaticScopeError("automatic_scope_changed", "连接身份发生变化，请重新确认目标。")
    initial_selected = client._selected(group["snapshot"], nodes, devices)
    relock_plan = _automatic_relock_plan(
        group["snapshot"]["context"], group["snapshot"], initial_selected, args, clock() - completed
    )
    effective_args = argparse.Namespace(**vars(args))
    effective_args.operation, effective_args.duration = relock_plan["operation"], relock_plan["duration"]
    evidence.update(requested_operation=args.operation, effective_operation=effective_args.operation)
    if relock_plan["leases"] is not None:
        evidence.update(
            operation_mapping_reason="fresh_own_same_mode_relock",
            requested_duration=args.duration,
            effective_duration=effective_args.duration,
            duration_source="group-default" if args.duration is None else "user-request",
        )
    client.calls, client.deadline = result["http_requests"], deadline
    if client.calls + 3 > MAX_CALLS or clock() >= deadline:
        raise AutomaticScopeError(
            "automatic_request_budget_exhausted",
            "查询预算不足，未发送资源命令。",
            request_budget={**budget, "used_http_requests": client.calls},
            resolution=evidence,
        )
    client.discover()  # This is the selected command's existing anonymous manifest check, not an extra request.
    coherent = "coherent_resource_snapshot" in client.manifest.get("capabilities", ())
    # Legacy verification may retry its read once; reserve that before dispatch as well.
    reserve = 2 if coherent else 5
    if client.calls + reserve > MAX_CALLS or clock() >= deadline:
        raise AutomaticScopeError(
            "automatic_request_budget_exhausted",
            "查询预算不足，未发送资源命令。",
            request_budget={**budget, "used_http_requests": client.calls},
            resolution=evidence,
        )

    evidence["http_requests_before_selected_read"] = client.calls

    def precondition(selected_client, context, snapshot, *, final=False):
        fresh_identity = {
            "origin": selected_client.base,
            "instance_id": context["server"]["instance_id"],
            "bot": context["bot"],
            "actor": context["actor"],
        }
        if _automatic_scope_identity(fresh_identity) != expected_scope:
            raise AutomaticScopeError("automatic_scope_changed", "目标身份发生变化，未发送资源命令。")
        now = clock()
        if any(
            not group["snapshot"].get("snapshot_fresh_for_write")
            or metric_number(group["snapshot"].get("age_seconds")) is None
            or group["snapshot"]["age_seconds"] + now - completed > MAX_RESOURCE_SNAPSHOT_AGE
            for group in groups
        ) or not selected_client._snapshot_fresh_for_write(snapshot, selected_client._resource_request_started):
            raise AutomaticScopeError("automatic_snapshot_stale", "资源状态已过旧，未发送资源命令。")
        needed = 1 if coherent else 3
        if selected_client.calls + needed > MAX_CALLS or now >= deadline:
            raise AutomaticScopeError(
                "automatic_request_budget_exhausted",
                "剩余预算不足以完成验证，未发送命令。",
                request_budget={**budget, "used_http_requests": selected_client.calls},
                resolution=evidence,
            )
        fresh = _automatic_resources(snapshot, context)
        for hint, node, original in zip(hints, nodes, selector_evidence, strict=True):
            if [item["id"] for item in fresh.values() if _automatic_match(item, hint)] != [node]:
                raise AutomaticScopeError("automatic_target_changed", "标识对应的目标发生变化，未发送资源命令。")
            if _automatic_selector_evidence(fresh[node], hint) != original:
                raise AutomaticScopeError("automatic_target_changed", "别名或地址证据发生变化，未发送资源命令。")
            if (
                sorted((str(card["id"]), card.get("dev_model")) for card in fresh[node].get("devices", []))
                != signatures[node]
            ):
                raise AutomaticScopeError("automatic_device_set_changed", "节点的完整卡集合发生变化，未发送资源命令。")
        selected = selected_client._selected(snapshot, nodes, devices)
        if args.model is not None and not all(
            selected_client._matches_model(item, args.model.strip().casefold(), card_bot, args.minimum_model_count)
            for item in selected
        ):
            raise AutomaticScopeError(
                "selected_resource_model_unverified_or_insufficient", "所需型号或数量无法确认，未申请未知硬件。"
            )
        if (
            _automatic_relock_plan(
                context, snapshot, selected, args, clock() - selected_client._resource_request_started
            )
            != relock_plan
        ):
            raise AutomaticScopeError("automatic_relock_changed", "原占用、续期模式或默认时长发生变化，未发送命令。")
        for unit in selected:
            bookings = unit.get("booking_list", [])
            if effective_args.operation in {"lock", "slock"} and (
                bookings
                or unit["lock_status"] not in ({"idle"} if effective_args.operation == "lock" else {"idle", "shared"})
            ):
                raise AutomaticScopeError("automatic_resource_conflict", "完整目标存在占用或排队冲突，未选择部分资源。")
            if args.operation == "release" and not selected_client._own(
                unit["holders"] + bookings, context["actor"]["external_user_id"]
            ):
                raise AutomaticScopeError("automatic_release_not_owned", "未确认持有或预约该资源，未发送释放命令。")
        _automatic_policy(selected_client, context, snapshot, selected, effective_args)
        if final:
            evidence["selection_checked_before_post"] = True
            evidence["http_requests_before_post"] = selected_client.calls

    return client, {
        "nodes": nodes,
        "devices": devices,
        "operation": effective_args.operation,
        "duration": effective_args.duration,
        "precondition": precondition,
        "resolution": evidence,
    }


def reference_command_precondition(reference, args):
    """Qualify one explicit target; never broaden it to aliases or other scopes."""
    token, node = parse_resource_reference(reference)

    def check(client, context, snapshot, *, final=False):
        record = {
            "approved_origin": client.base,
            "instance_id": context["server"]["instance_id"],
            "bot_uuid": context["bot"]["uuid"],
            "actor": context["actor"],
        }
        if not resource_scope_digest(record).startswith(token) or not resource_scope_digest(client.config).startswith(
            token
        ):
            raise ClientError("resource_reference_scope_mismatch")
        if not client._snapshot_fresh_for_write(snapshot, client._resource_request_started):
            raise ClientError("resource_snapshot_stale_or_unverifiable")
        matches = [row for row in snapshot.get("resources", []) if isinstance(row, dict) and row.get("id") == node]
        if len(matches) != 1:
            raise ClientError("resource_reference_node_unavailable")
        # Validate exact IDs and complete state evidence before choosing devices.
        # Do not infer that an unknown/busy unit is eligible, or substitute an alias.
        _automatic_resources({**snapshot, "resources": matches}, context)
        selected = client._selected(snapshot, [node], args.device)
        if args.operation in {"lock", "slock"} and any(
            unit.get("booking_list", [])
            or unit["lock_status"] not in ({"idle"} if args.operation == "lock" else {"idle", "shared"})
            for unit in selected
        ):
            raise ClientError("resource_reference_resource_conflict")
        if args.operation == "release" and any(
            not client._own(unit["holders"] + unit.get("booking_list", []), context["actor"]["external_user_id"])
            for unit in selected
        ):
            raise ClientError("resource_reference_release_not_owned")
        _automatic_policy(client, context, snapshot, selected, args)

    return check


def runtime_compatible():
    return sys.version_info >= (3, 10)


def main(argv=None):
    if not runtime_compatible():
        print(json.dumps({"error": "helper_requires_python_3_10_or_newer", "retry_mutation": False}), file=sys.stderr)
        return 2
    parser = SafeParser(
        description="Install the LockBot Skill, manage private connections, and query or allocate compute resources.",
        epilog="Run SUBCOMMAND --help for its options. Use resources-all to query saved groups. command without "
        "--connection resolves node hints across saved groups. Other scoped actions need --connection FILE. "
        "Supply Key file paths only.",
    )
    parser.add_argument(
        "--connection", help="Verified private connection JSON file (POSIX 0600 or Windows private ACL)"
    )
    parser.add_argument(
        "--legacy-read",
        action="store_true",
        help="Opt in to the tested native legacy profile for saved-connection reads only; all writes are blocked",
    )
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
    challenge = sub.add_parser(
        "key-challenge",
        help="Recover or create a personal Key through chat confirmation",
        description=(
            "Request the personal Key flow. Chat confirmation identifies the bot and recovers an active Key "
            "or creates the first Key. Reset requires explicit user approval."
        ),
    )
    challenge.add_argument("--origin", required=True)
    challenge.add_argument(
        "--purpose", choices=["recover", "reset"], default="recover", help="Default: recover or create"
    )
    challenge.add_argument("--destination", help="Explicit new private challenge directory")
    challenge.add_argument("--scope", choices=["user", "project"], default="user")
    challenge.add_argument("--authorized", action="store_true", required=True, help="User authorized requesting a code")
    enroll = sub.add_parser(
        "enroll",
        help="Compatibility binding for older services; use key-challenge for personal Keys",
        description="Compatibility binding for services without chat_credential_challenges.",
    )
    enroll.add_argument("--origin", required=True)
    enroll.add_argument("--group", help="Optional group explicitly named by the user")
    enroll.add_argument("--destination", help="Explicit new private enrollment directory")
    enroll.add_argument(
        "--scope", choices=["user", "project"], default="user", help="Credential storage scope (default: user)"
    )
    enroll.add_argument("--agent-label", help="Optional stable name for this Agent installation")
    enroll.add_argument("--authorized", action="store_true", required=True, help="User authorized requesting a code")
    for action, help_text in (
        ("enrollment-status", "Check a previously requested binding code"),
        ("finish-enrollment", "Verify chat confirmation and save the scoped connection"),
    ):
        command = sub.add_parser(action, help=help_text, description=help_text)
        command.add_argument("--enrollment-dir", required=True, help="Private directory returned with the chat code")
        if action == "finish-enrollment":
            command.add_argument("--confirm-bot-name", help="Bot name explicitly confirmed by the user")
    catalog = sub.add_parser("connections", help="List project and user connections plus unfinished enrollments")
    catalog.add_argument("--directory", help="Read only this private connection catalog")
    sub.add_parser("context", help="Verify the selected connection's identity, bot and rules")
    resources = sub.add_parser("resources", help="Read the selected bot's current resources and queue")
    resources.add_argument("--mine", action="store_true", help="Separate only your verified holdings and bookings")
    exact_times_help = "Include precise lease timestamps and seconds for scheduling or an exact deadline."
    resources.add_argument("--exact-lease-times", action="store_true", help=exact_times_help)
    details_help = "Include complete verified response details for explicit debugging; do not persist a cache."
    resources.add_argument("--full", action="store_true", help=details_help)
    resources.add_argument("--ref", help="Exact resource reference returned by an observed node summary (read only)")
    aggregate = sub.add_parser("resources-all", help="Read all existing private catalog connections without enrollment")
    aggregate.add_argument("--directory", help="Read only this private connection catalog")
    aggregate.add_argument("--mine", action="store_true", help="Separate your holdings and bookings in every group")
    aggregate.add_argument("--exact-lease-times", action="store_true", help=exact_times_help)
    aggregate.add_argument("--full", action="store_true", help=details_help)
    aggregate.add_argument(
        "--after-scope",
        help="Continue a bounded saved-connection read using scope_page.next_after_scope; no write discovery",
    )
    for query in (resources, aggregate):
        query.add_argument("--summary", action="store_true", help="Opt in to a bounded node-level JSON summary")
        query.add_argument("--limit", type=int, help="Maximum summary nodes (default 20; implies --summary)")
        query.add_argument(
            "--offset", type=int, help="Local summary offset in a newly read observation; not a server cursor"
        )
        query.add_argument(
            "--json",
            nargs="?",
            const="",
            metavar="FIELDS",
            help="Select summary fields, comma-separated; ref is retained. Without fields, list them without requests.",
        )
        query.add_argument(
            "--node", action="append", default=[], help="Filter by exact observed node ID or alias; repeat as needed."
        )
        query.add_argument(
            "--model", help="Filter by reported accelerator model; unknown hardware is reported separately."
        )
        query.add_argument(
            "--with-metrics",
            action="store_true",
            help="Also batch current utilization for selected nodes using separately approved source access.",
        )
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
        description="Send one resource operation. Without --connection, resolve unique node hints across saved "
        "groups and recheck the selection before writing. With --connection, use exact observed node IDs.",
        epilog="Examples: command --operation lock --node 19; "
        "--connection CONNECTION command --operation lock --node node-01 --device 0 --duration 30m",
    )
    command.add_argument("--full", action="store_true", help=details_help)
    command.add_argument("--operation", required=True, choices=["lock", "slock", "book", "claim", "renew", "release"])
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--node",
        action="append",
        default=[],
        help="Exact node ID, or an alias/observed IPv4 suffix when --connection is omitted. Repeat for more nodes.",
    )
    target.add_argument("--ref", help="Exact resource reference; use its saved scope without scanning unrelated groups")
    command.add_argument(
        "--device", action="append", default=[], help="Exact device ID on the selected node. Repeat for more cards."
    )
    command.add_argument(
        "--whole-node", action="store_true", help="Select entire nodes only when the user asked for or approved them."
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
        "--duration",
        help="Lease for lock/slock/book or time to add on renew (e.g. 30m). Omit to use the group default for "
        "lock/slock/book, the booked duration for claim, or no duration for release.",
    )
    command.add_argument("--metadata-file", help="Private JSON: task_category, purpose, agent_label only")
    command.add_argument("--exact-lease-times", action="store_true", help=exact_times_help)
    command = sub.add_parser("metrics", help="Read current utilization for exact nodes from an approved source")
    command.add_argument("--full", action="store_true", help=details_help)
    command.add_argument(
        "--node", action="append", required=True, help="Exact node ID from resources. Repeat as needed."
    )
    args = parser.parse_args(argv)
    if args.action == "command":
        args.device = [re.sub(r"(?i)^dev(0|[1-9][0-9]{0,4})$", r"\1", value) for value in args.device]
    client = None
    automatic = None
    summary_requested = False
    summary_fields = None
    reference_node = None
    reference_token = None
    try:
        if args.legacy_read and args.action not in {"discover", "context", "resources", "resources-all"}:
            raise ClientError("legacy_read_only")
        if args.legacy_read and getattr(args, "with_metrics", False):
            raise ClientError("legacy_read_only")
        if args.action in {"resources", "resources-all"}:
            if args.json == "":
                print(json.dumps({"json_fields": list(SUMMARY_FIELDS), "required_row_fields": ["ref"]}))
                return 0
            summary_requested = (
                args.summary or args.json is not None or args.limit is not None or args.offset is not None
            )
            if args.json is not None:
                summary_fields = list(dict.fromkeys(args.json.split(",")))
                if not summary_fields or any(field not in SUMMARY_FIELDS for field in summary_fields):
                    raise ClientError("invalid_summary_fields")
            if summary_requested:
                args.limit = SUMMARY_NODE_LIMIT if args.limit is None else args.limit
                args.offset = 0 if args.offset is None else args.offset
                if not 1 <= args.limit <= MAX_OVERVIEW_ENTRIES or args.offset < 0:
                    raise ClientError("invalid_summary_page")
                if args.full:
                    raise ClientError("summary_combination_not_supported")
                if args.with_metrics and args.action == "resources-all":
                    raise ClientError("summary_aggregate_metrics_not_supported")
        if args.action in {"resources", "command"} and args.ref:
            if args.action == "resources" and args.node:
                raise ClientError("resource_reference_conflicts_with_node_filter")
            reference_token, reference_node = parse_resource_reference(args.ref)
            if args.action == "command" and not re.fullmatch(r"[^\s,\x00-\x1f\x7f]+", reference_node):
                raise ClientError("invalid_node")
            if args.connection:
                if not resource_scope_digest(private_json(args.connection)).startswith(reference_token):
                    raise ClientError("resource_reference_scope_mismatch")
            else:
                located = resolve_resource_reference(args.ref)
                if len(located["connections"]) != 1:
                    raise ClientError("resource_reference_connection_ambiguous")
                args.connection = located["connections"][0]["connection"]
            args.node = [reference_node]
        if args.action == "command" and (
            args.whole_node and args.device or args.connection is not None and args.whole_node == bool(args.device)
        ):
            raise ClientError("select_devices_or_explicit_whole_nodes")
        if args.action == "connections":
            if args.connection:
                raise ClientError("connection_not_used_for_listing")
            print(
                json.dumps(
                    {"result": listed_connections(args.directory), "update_required": False},
                    ensure_ascii=os.name == "nt",
                )
            )
            return 0
        if args.action == "resources-all":
            if args.connection:
                raise ClientError("connection_not_used_for_aggregation")
            output = resources_all(
                args.directory,
                mine=args.mine,
                exact_lease_times=args.exact_lease_times,
                nodes=args.node,
                model=args.model,
                with_metrics=args.with_metrics,
                full=args.full,
                summary=summary_requested,
                after_scope=args.after_scope,
                legacy_read=args.legacy_read,
            )
            if summary_requested:
                print(
                    query_summary_output(
                        output,
                        fields=summary_fields,
                        limit=args.limit,
                        offset=args.offset,
                        nodes=args.node,
                        model=args.model,
                    )
                )
                return 0
            print(
                json.dumps(
                    aggregate_output(output, full=args.full),
                    ensure_ascii=os.name == "nt",
                )
            )
            return 0
        enrollment_actions = {"enroll", "key-challenge", "enrollment-status", "finish-enrollment"}
        if args.action in enrollment_actions and args.connection:
            raise ClientError("enrollment_requires_new_connection")
        if args.action == "command" and args.connection is None:
            client, automatic = resolve_command_scope(args)
        elif args.action in {"enrollment-status", "finish-enrollment"}:
            path, state = enrollment_state(args.enrollment_dir)
            client = Client(approved_origin=state.get("approved_origin"))
        elif args.action in {"discover", "install", "init", "enroll", "key-challenge"} or args.connection:
            client = Client(
                args.connection, approved_origin=getattr(args, "origin", None), legacy_read=args.legacy_read
            )
        else:
            raise ClientError("private_connection_required")
        if reference_token is not None and not resource_scope_digest(client.config).startswith(reference_token):
            # The file can change between catalog lookup and Client construction.
            # Recheck the actual loaded pins before any Key read or network request.
            raise ClientError("resource_reference_scope_mismatch")
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
            if client.api_profile == "native-legacy-read-v1":
                result = inventory_output({"context": result})["context"]
        elif args.action == "resources":
            result = (
                client.own_inventory(exact_lease_times=True, node=reference_node) if args.mine else client.inventory()
            )
            if reference_node is not None and not args.mine:
                exact = [row for row in result["resources"] if row.get("id") == reference_node]
                if len(exact) != 1:
                    raise ClientError("resource_reference_node_unavailable")
                result = {**result, "resources": exact}
            if not summary_requested:
                result = select_inventory(result, nodes=args.node, model=args.model)
            if args.with_metrics:
                result = (
                    attach_summary_metrics(
                        client, result, limit=args.limit, offset=args.offset, nodes=args.node, model=args.model
                    )
                    if summary_requested
                    else attach_metrics(client, result, full=args.full)
                )
            result = observation_at_completion(client, result)
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
                automatic["operation"] if automatic else args.operation,
                automatic["nodes"] if automatic else args.node,
                automatic["devices"] if automatic else args.device,
                automatic["duration"] if automatic else args.duration,
                metadata,
                same_devices_on_all_nodes=args.same_devices_on_all_nodes,
                device_count=args.device_count,
                model=args.model,
                minimum_model_count=args.minimum_model_count,
                group=args.group,
                **(
                    {"_selection_precondition": automatic["precondition"]}
                    if automatic
                    else {"_selection_precondition": reference_command_precondition(args.ref, args)}
                    if args.ref
                    else {}
                ),
            )
            if automatic:
                result["resolution"] = automatic["resolution"]
                if automatic["resolution"].get("duration_source") == "group-default":
                    result["duration_source"] = "group-default"
            elif args.ref:
                result["target_reference"] = args.ref
        else:
            result = client.metrics(args.node)
        if args.action in {"resources", "command"} and not args.exact_lease_times:
            if args.action == "resources" and args.mine:
                format_lease_times(
                    {"age_seconds": result["age_seconds"], "resources": [{"holders": result["holdings"]}]}
                )
            else:
                format_lease_times(result if args.action == "resources" else result["verified_snapshot"])
        if args.action == "resources":
            if summary_requested:
                print(
                    query_summary_output(
                        {
                            "result": result,
                            "update_required": client.needs_update,
                            "_approved_origin": client.base,
                            "_http_requests": client.calls,
                        },
                        fields=summary_fields,
                        limit=args.limit,
                        offset=args.offset,
                        nodes=args.node,
                        model=args.model,
                        private_values=client.secrets,
                    )
                )
                return 0
            result = inventory_output(result, full=args.full)
        elif args.action == "command":
            result = command_output(result, full=args.full)
        elif args.action == "metrics":
            result = metrics_output(result, full=args.full)
        print(
            json.dumps(
                client.redacted(
                    {
                        "result": result,
                        "update_required": client.needs_update,
                        **(
                            {
                                "api_profile": client.api_profile,
                                "instruction_state": client.instruction_state,
                                "instruction_refresh_required": True,
                            }
                            if client.api_profile == "native-legacy-read-v1"
                            else {}
                        ),
                    }
                ),
                ensure_ascii=os.name == "nt",
            )
        )
        return 0
    except ClientError as exc:
        output = {"error": exc.code, "retry_mutation": False}
        if args.action == "command" and args.ref and (client is None or client._last_mutation is None):
            output["command_sent"] = False
        if client is not None and client.api_profile == "native-legacy-read-v1":
            output.update(
                api_profile=client.api_profile,
                instruction_state=client.instruction_state,
                instruction_refresh_required=True,
            )
        if isinstance(exc, AutomaticScopeError):
            output.update(command_sent=False, clarification=redacted_values(exc.clarification))
        elif args.action == "command" and args.connection is None:
            questions = {
                "resource_snapshot_stale_or_unverifiable": "资源状态已过旧，未发送资源命令。",
                "request_budget_exhausted": "查询预算已耗尽，未发送资源命令。",
                "context_actor_mismatch_reinitialize_connection_without_rotating_key": "连接身份无法确认，未发送命令。",
                "context_identity_mismatch": "目标身份无法确认，未发送命令。",
                "resource_snapshot_identity_mismatch": "资源身份无法确认，未发送命令。",
            }
            # These raw errors originate in pre-reads. The same failures after
            # dispatch are wrapped by command() as an unverified/uncertain write.
            if exc.code in questions:
                output.update(command_sent=False, clarification={"question_zh": questions[exc.code]})
                if exc.code == "request_budget_exhausted":
                    output["clarification"]["request_budget"] = {
                        "http_limit": MAX_CALLS,
                        "used_http_requests": client.calls if client else None,
                        "deadline_seconds": 60,
                    }
        if args.action == "command" and client and client._last_mutation is not None:
            attempt = client._last_mutation
            output["mutation_attempted"] = True
            if attempt["response_received"]:
                output["command_sent"] = True  # A response proves a request, not execution.
            rejected = exc.status is not None and 400 <= exc.status < 500
            output["command_outcome"] = "rejected" if rejected else "unknown"
            output["next_command"] = (
                ["resources", "--ref", args.ref, "--mine", "--summary"] if args.ref else ["resources-all", "--mine"]
            )
            if not rejected:
                output["message_zh"] = (
                    "这次操作结果暂无法确认。即使当前空闲或没有记录，也不能证明它未执行。先只核对当前占用，不重新提交。"
                )
            output["next_step"] = (
                "Read next_command only and report the rejected request; do not replay or switch connections."
                if rejected
                else "Read next_command only; execution remains UNKNOWN even if idle or absent. "
                "Do not retry, switch connections, or offer resubmission; report only verified observations."
            )
            if automatic and automatic["resolution"].get("selection_checked_before_post"):
                output["resolution"] = automatic["resolution"]
        if args.action == "panel-link" and client and client._panel_home is not None:
            output["home"] = client._panel_home
            output["next_step"] = (
                "Do not request another panel-link or consume a ticket. Open home in the browser, "
                "or give the fixed address to the user if browser access is unavailable. "
                "Do not claim the page was opened unless the browser confirms it."
            )
        if args.action == "enroll" and exc.code == "use_key_challenge":
            command = ["key-challenge", "--origin", args.origin, "--scope", args.scope, "--authorized"]
            if args.destination:
                command.extend(["--destination", args.destination])
            output["next_command"] = command
            output["next_step"] = "Run next_command to request a code. The user's chat confirmation identifies the bot."
        elif args.action == "enroll" and exc.code in {"invalid_enrollment_group", "http_403"}:
            output["next_step"] = (
                "Use key-challenge on services that advertise chat_credential_challenges. "
                "The user's chat confirmation identifies the bot."
            )
        if exc.code == "group_supports_whole_nodes_only":
            output["next_step"] = "Ask the user before switching to whole nodes or changing groups."
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
