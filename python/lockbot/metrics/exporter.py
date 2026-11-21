"""Low-frequency cached Prometheus exporter, independent of the Lockbot backend."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import ipaddress
import json
import logging
import math
import os
import pwd
import random
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_XML_BYTES = 4 * 1024 * 1024
MIN_INTERVAL = 60
logger = logging.getLogger(__name__)


def number(value, *, percent=False):
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(%|[KMGT]i?B)?", text, re.I)
    if not match:
        return None
    result = float(match.group(1))
    unit = (match.group(2) or "").lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    if unit in factors:
        result *= factors[unit]
    if not math.isfinite(result) or result < 0 or (percent and result > 100):
        return None
    return result


def text_at(element, *paths):
    for path in paths:
        text = element.findtext(path)
        if text and text.strip():
            return text.strip()
    return None


@dataclass(frozen=True)
class Card:
    device: str
    utilization: float | None
    memory_used: float | None
    memory_total: float | None
    processes: tuple[int, ...] | None
    model: str | None = None
    pci_bus: str | None = None
    uuid: str | None = None


def parse_xml(raw: str) -> list[Card]:
    """Read structured device records; unsupported/ambiguous layouts fail closed.

    NVIDIA -q -x uses gpu/product_name/uuid and fb_memory_usage. Vendor-specific
    aliases are explicit, never positional table columns or arbitrary numeric fields.
    """
    if len(raw.encode()) > MAX_XML_BYTES or re.search(r"<!ENTITY", raw, re.I):
        raise ValueError("invalid XML")
    root = ET.fromstring(raw)
    elements = root.findall("./gpu") or root.findall("./xpu") or root.findall("./device")
    if not elements or len(elements) > 256:
        raise ValueError("no structured devices")
    cards, identifiers = [], set()
    for element in elements:
        device = text_at(element, "minor_number", "device_id", "index")
        if device is None:
            raise ValueError("missing explicit device index")
        if not device.isascii() or not device.isdecimal() or str(int(device)) != device or device in identifiers:
            raise ValueError("ambiguous device index")
        identifiers.add(device)
        used = number(text_at(element, "fb_memory_usage/used", "memory_usage/used"))
        total = number(text_at(element, "fb_memory_usage/total", "memory_usage/total"))
        if total is not None and (total <= 0 or (used is not None and used > total)):
            used = total = None
        processes_element = element.find("processes")
        processes = None
        if processes_element is not None:
            pids = set()
            valid = True
            for process in list(processes_element):
                pid = process.findtext("pid")
                if not pid or not pid.isascii() or not pid.isdecimal() or int(pid) <= 0:
                    valid = False
                    break
                pids.add(int(pid))
            if valid:
                processes = tuple(sorted(pids))
        cards.append(
            Card(
                device,
                number(text_at(element, "utilization/gpu_util", "utilization/xpu_util"), percent=True),
                used,
                total,
                processes,
                text_at(element, "product_name", "device_name"),
                text_at(element, "pci/pci_bus_id"),
                text_at(element, "uuid"),
            )
        )
    return sorted(cards, key=lambda card: int(card.device))


def run_xml(command: list[str], timeout: float) -> str:
    """One bounded command; kill its process group on timeout, never log raw output."""
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(
            command, stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True
        )
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
        if child.returncode != 0 or output.tell() > MAX_XML_BYTES:
            raise ValueError("collector command failed")
        output.seek(0)
        return output.read(MAX_XML_BYTES + 1).decode("utf-8")


def cpu_counters(proc_root: Path):
    first = (proc_root / "stat").read_text().splitlines()[0].split()
    if first[0] != "cpu" or len(first) < 9:
        raise ValueError("invalid CPU counters")
    fields = [int(value) for value in first[1:9]]  # guest is already included in user/nice
    return sum(fields), fields[3] + fields[4]  # idle + iowait


def host_memory(proc_root: Path):
    fields = {}
    for line in (proc_root / "meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            fields[key] = int(value.split()[0]) * 1024
    total, available = fields["MemTotal"], fields["MemAvailable"]
    if not 0 <= available <= total or total <= 0:
        raise ValueError("invalid memory counters")
    return total - available, total


def host_memory_details(proc_root: Path):
    """Supplemental procfs counters; buff/cache is Buffers + Cached + SReclaimable.

    Availability and cache are distinct. These counters do not diagnose driver OOM.
    Missing counters remain unknown, including on older probe implementations.
    """
    fields = {}
    wanted = {"MemTotal", "MemAvailable", "Buffers", "Cached", "SReclaimable"}
    for line in (proc_root / "meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in wanted:
            fields[key] = int(value.split()[0]) * 1024
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable")
    cache_keys = ("Buffers", "Cached", "SReclaimable")
    cache = sum(fields[key] for key in cache_keys) if all(key in fields for key in cache_keys) else None
    return {
        "available_bytes": available if available is not None and 0 <= available <= total else None,
        "cache_bytes": cache if cache is not None and 0 <= cache <= total else None,
    }


def container_count(pids: tuple[int, ...] | None, proc_root: Path):
    if pids is None or len(pids) > 512:
        return None
    identities = set()
    for pid in pids:
        try:
            groups = (proc_root / str(pid) / "cgroup").read_text()
        except OSError:
            return None  # exited/inaccessible process is unknown, not zero containers
        ids = re.findall(r"(?:docker[-/]|cri-containerd[-/])([a-f0-9]{64})(?:\.scope)?", groups)
        if ("docker" in groups or "containerd" in groups) and not ids:
            return None
        identities.update(ids)
    return len(identities)


def safe_name(value, maximum=96):
    value = re.sub(r"[\x00-\x1f\x7f]", "", str(value))[:maximum]
    return re.sub(
        r"(?:lbr_|lbk_|sk-|ghp_)[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[redacted]",
        value,
    )


def command_summary(raw):
    """Publish an executable/script summary, never arbitrary flags or environment."""
    argv = raw[:16384].split(b"\0")
    executable = safe_name(os.path.basename(argv[0].decode("utf-8", "replace"))) if argv else ""
    parts = [executable] if executable else []
    if len(argv) > 1 and re.fullmatch(r"(?:python[0-9.]*|bash|sh|node)", executable):
        script = argv[1].decode("utf-8", "replace")
        if not script.startswith("-") and re.fullmatch(r"[^\s=]+\.(?:py|sh|js)", script):
            parts.append(safe_name(os.path.basename(script)))
    if len([item for item in argv if item]) > len(parts):
        parts.append("[arguments omitted]")
    return " ".join(parts)[:160]


def process_metadata(cards, proc_root):
    """Bounded, best-effort host metadata. POSIX user is not an authenticated IM identity."""
    pids = sorted({pid for card in cards for pid in (card.processes or ())})[:512]
    containers = {}
    if pids:
        try:
            output = subprocess.run(
                ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in output.stdout[:262144].splitlines():
                identity, _, name = line.partition(" ")
                if re.fullmatch(r"[a-f0-9]{64}", identity):
                    containers[identity] = safe_name(name)
        except (OSError, subprocess.TimeoutExpired):
            pass
    result = {}
    for pid in pids:
        directory = proc_root / str(pid)
        try:
            before = (directory / "stat").read_text().rpartition(") ")[2].split()[19]
            uid = directory.stat().st_uid
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            with (directory / "cmdline").open("rb") as source:
                command = command_summary(source.read(16384))
            groups = (directory / "cgroup").read_text()
            ids = re.findall(r"(?:docker[-/]|cri-containerd[-/])([a-f0-9]{64})(?:\.scope)?", groups)
            identity = ids[0] if len(set(ids)) == 1 else ""
            after = (directory / "stat").read_text().rpartition(") ")[2].split()[19]
            if before != after:
                continue
            result[pid] = {
                "pid": str(pid),
                "container_id": identity,
                "container_name": containers.get(identity, ""),
                "user": safe_name(user),
                "command": command,
            }
        except (OSError, ValueError, IndexError):
            continue
    return result


def labels(values):
    def quote(value):
        return json.dumps(str(value), ensure_ascii=True)

    return "{" + ",".join(f"{key}={quote(value)}" for key, value in values.items()) + "}"


def sample(name, value, tags, observed_at=None):
    if value is None:
        return ""
    encoded = str(value) if isinstance(value, int) else format(value, ".17g")
    return f"{name}{labels(tags)} {encoded}\n"


def _hardware_command(*args):
    try:
        result = subprocess.run(
            args, capture_output=True, check=True, text=True, timeout=2, env={**os.environ, "LC_ALL": "C"}
        )
        return result.stdout if len(result.stdout) <= 128 * 1024 else ""
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""


def _hardware_network():
    hostname = socket.gethostname()
    resolved = _hardware_command("hostname", "-f").strip().rstrip(".")
    labels = resolved.split(".")
    fqdn = (
        resolved
        if (
            len(labels) > 1
            and len(resolved) <= 253
            and not labels[-1].isdigit()
            and all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in labels)
        )
        else None
    )
    try:
        interfaces = json.loads(_hardware_command("ip", "-j", "address", "show"))
    except ValueError:
        interfaces = None
    addresses = [] if isinstance(interfaces, list) else None
    for interface in (interfaces if isinstance(interfaces, list) else [])[:64]:
        if not isinstance(interface, dict) or not isinstance(interface.get("ifname"), str):
            continue
        entries = interface.get("addr_info", [])
        if not isinstance(entries, list):
            continue
        for entry in entries[:32]:
            if not isinstance(entry, dict):
                continue
            try:
                address = ipaddress.ip_address(entry["local"])
                prefix = entry["prefixlen"]
            except (KeyError, ValueError):
                continue
            if address.is_loopback or type(prefix) is not int or not 0 <= prefix <= address.max_prefixlen:
                continue
            addresses.append(
                {
                    "interface": interface["ifname"],
                    "ip": str(address),
                    "prefix_bits": prefix,
                    "scope": entry.get("scope"),
                }
            )
    return {"hostname": hostname, "fqdn": fqdn, "addresses": addresses}


def _pci_address(bus):
    match = re.fullmatch(r"(?:0000)?([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])", (bus or "").lower())
    return match[1] if match else None


def _hardware_topology(bus):
    if not bus:
        return {}
    device = Path("/sys/bus/pci/devices") / bus
    topology = {}
    for field, filename in (
        ("numa_node", "numa_node"),
        ("pcie_speed", "current_link_speed"),
        ("pcie_width", "current_link_width"),
    ):
        try:
            value = (device / filename).read_text().strip()
            topology[field] = (
                int(value) if field != "pcie_speed" and value.isdecimal() else value if field == "pcie_speed" else None
            )
        except OSError:
            continue
    return topology


def _hardware_device(card, vendor):
    bus = _pci_address(card.pci_bus)
    identifier = card.uuid if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", card.uuid or "") else None
    if identifier and identifier.lower() in {"unknown", "none", "null"}:
        identifier = None
    return {
        "index": card.device,
        "vendor": vendor,
        "model": card.model,
        "memory_total_bytes": int(card.memory_total) if card.memory_total is not None else None,
        "device_uuid": identifier,
        "pci_bus": bus,
        **_hardware_topology(bus),
    }


def hardware_inventory(config, proc_root, cards, observed_at):
    """Private facts from one sampling scope, separate from metrics and allocatable resource IDs."""
    try:
        cpu = {
            item["field"].rstrip(":"): item["data"] for item in json.loads(_hardware_command("lscpu", "-J"))["lscpu"]
        }
    except (ValueError, KeyError, TypeError):
        cpu = {}
    try:
        _, memory_total = host_memory(proc_root)
    except (OSError, ValueError, KeyError, IndexError):
        memory_total = None

    def count(key):
        value = str(cpu.get(key, "")).strip()
        return int(value) if value.isdecimal() else None

    return {
        "schema_version": 1,
        "source_id": config["node"],
        "collector": "lockbot-exporter",
        "node": config["node"],
        "observed_at": datetime.fromtimestamp(observed_at, timezone.utc).isoformat(),
        "scope": config.get("inventory_scope", "unknown"),
        "coverage": "visible" if cards else "unavailable",
        "network": _hardware_network(),
        "cpu": {
            "arch": cpu.get("Architecture"),
            "model": cpu.get("Model name"),
            "logical_cpus": count("CPU(s)"),
            "sockets": count("Socket(s)"),
            "cores_per_socket": count("Core(s) per socket"),
        },
        "memory_total_bytes": memory_total,
        "devices": [_hardware_device(card, config["vendor"]) for card in cards],
    }


def _store_hardware_inventory(output, data):
    path = Path(output)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".hardware-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Collector:
    def __init__(self, config, *, clock=time.time, monotonic=time.monotonic):
        self.config, self.clock, self.monotonic = config, clock, monotonic
        self.lock = threading.Lock()
        self.host_body = self.accelerator_body = ""
        self.status = {}
        self.cpu_previous = None
        self.proc_root = Path(config.get("proc_root", "/proc"))
        self.node = config["node"]

    def _update(self, kind, body, success, started, observed_at):
        with self.lock:
            if success:
                setattr(self, kind + "_body", body)
            self.status[kind] = (
                success,
                self.monotonic() - started,
                observed_at if success else self.status.get(kind, (False, 0, 0))[2],
            )

    def collect_host(self):
        start, observed_at = self.monotonic(), self.clock()
        body, tags = "", {"node": self.node}
        try:
            current = cpu_counters(self.proc_root)
            if self.cpu_previous:
                total, idle = (a - b for a, b in zip(current, self.cpu_previous, strict=True))
                utilization = (total - idle) / total * 100 if total > 0 and 0 <= idle <= total else None
                body += sample("lockbot_host_cpu_utilization_percent", utilization, tags, observed_at)
            self.cpu_previous = current
            used, total = host_memory(self.proc_root)
            body += sample("lockbot_host_memory_used_bytes", used, tags, observed_at)
            body += sample("lockbot_host_memory_total_bytes", total, tags, observed_at)
            for name, value in host_memory_details(self.proc_root).items():
                body += sample("lockbot_host_memory_" + name, value, tags, observed_at)
            for alias, path in self.config.get("filesystems", {}).items():
                try:
                    disk = os.statvfs(path)
                except OSError:
                    continue
                total = disk.f_blocks * disk.f_frsize
                used = (disk.f_blocks - disk.f_bfree) * disk.f_frsize
                disk_tags = {**tags, "filesystem": alias}
                body += sample("lockbot_host_filesystem_used_bytes", used, disk_tags, observed_at)
                body += sample("lockbot_host_filesystem_total_bytes", total, disk_tags, observed_at)
            self._update("host", body, True, start, observed_at)
        except (OSError, ValueError, KeyError, IndexError):
            self._update("host", "", False, start, observed_at)

    def collect_accelerator(self):
        start, observed_at = self.monotonic(), self.clock()
        try:
            cards = parse_xml(run_xml(self.config["command"], self.config.get("timeout_seconds", 10)))
            body = ""
            details = process_metadata(cards, self.proc_root)
            for card in cards:
                tags = {"node": self.node, "device": card.device, "vendor": self.config["vendor"]}
                body += sample("lockbot_accelerator_utilization_percent", card.utilization, tags, observed_at)
                body += sample("lockbot_accelerator_memory_used_bytes", card.memory_used, tags, observed_at)
                body += sample("lockbot_accelerator_memory_total_bytes", card.memory_total, tags, observed_at)
                body += sample(
                    "lockbot_accelerator_process_count",
                    None if card.processes is None else len(card.processes),
                    tags,
                    observed_at,
                )
                body += sample(
                    "lockbot_accelerator_container_count",
                    container_count(card.processes, self.proc_root),
                    tags,
                    observed_at,
                )
                for pid in (card.processes or ())[:128]:
                    if pid in details:
                        body += sample("lockbot_accelerator_process_info", 1, {**tags, **details[pid]})
            self._update("accelerator", body, True, start, observed_at)
        except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired):
            self._update("accelerator", "", False, start, observed_at)
            return
        if output := self.config.get("inventory_output"):
            with suppress(OSError, ValueError, TypeError, KeyError):
                _store_hardware_inventory(output, hardware_inventory(self.config, self.proc_root, cards, observed_at))

    def render(self):
        """Scrapes read cached memory only; never invoke commands or procfs here."""
        with self.lock:
            body = self.host_body + self.accelerator_body
            for kind, (success, duration, last_success) in self.status.items():
                tags = {"node": self.node, "collector": kind}
                body += sample("lockbot_collector_success", int(success), tags)
                body += sample("lockbot_collector_duration_seconds", duration, tags)
                body += sample("lockbot_sample_timestamp_seconds", last_success, tags)
            return body.encode()

    def run(self, stop):
        """One sampler, no overlap or catch-up storm; host and SMI cadences are separate."""
        next_host = next_accelerator = 0
        while not stop.is_set():
            now = self.monotonic()
            if now >= next_host:
                self.collect_host()
                next_host = self.monotonic() + self.config.get("host_interval_seconds", 60)
            if now >= next_accelerator:
                self.collect_accelerator()
                next_accelerator = self.monotonic() + self.config.get("interval_seconds", 300)
            stop.wait(max(0.1, min(next_host, next_accelerator) - self.monotonic()))


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", config.get("node", "")):
        raise ValueError("node must be an explicit non-sensitive resource label")
    if config.get("vendor") not in {"xpu", "nvidia"}:
        raise ValueError("vendor must be xpu or nvidia")
    inventory = config.get("inventory_output")
    if inventory is not None and (not isinstance(inventory, str) or not Path(inventory).is_absolute()):
        raise ValueError("inventory_output must be an absolute file path")
    if config.get("inventory_scope", "unknown") not in {"unknown", "host", "container"}:
        raise ValueError("invalid inventory_scope")
    command = config.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
        raise ValueError("command must be an explicit argument list; no shell")
    for name in ("interval_seconds", "host_interval_seconds"):
        if not isinstance(config.get(name, 120), int) or config.get(name, 120) < MIN_INTERVAL:
            raise ValueError("sampling interval must be at least 60 seconds")
    timeout = config.get("timeout_seconds", 10)
    if not isinstance(timeout, int | float) or not 0 < timeout <= 30:
        raise ValueError("timeout must be within 30 seconds")
    filesystems = config.get("filesystems", {})
    if (
        not isinstance(filesystems, dict)
        or len(filesystems) > 16
        or any(
            not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", alias) or not Path(path).is_absolute()
            for alias, path in filesystems.items()
        )
    ):
        raise ValueError("configure at most 16 named absolute filesystem paths")
    return config


def probe(config):
    """One SSH probe; emit only normalized metrics, never XML, process IDs or names."""
    proc = Path(config.get("proc_root", "/proc"))
    result = {"host_success": False, "accelerator_success": False}
    started = time.monotonic()
    try:
        result["cpu"] = cpu_counters(proc)
        result["memory"] = host_memory(proc)
        result["memory_details"] = host_memory_details(proc)
        result["filesystems"] = {}
        for alias, path in config.get("filesystems", {}).items():
            try:
                disk = os.statvfs(path)
                result["filesystems"][alias] = (
                    (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
                    disk.f_blocks * disk.f_frsize,
                )
            except OSError:
                continue
        result["host_observed_at"] = time.time()
        result["host_success"] = True
    except (OSError, ValueError, KeyError, IndexError):
        pass
    result["host_duration"] = time.monotonic() - started
    started = time.monotonic()
    observed_at, cards = time.time(), []
    try:
        lock_path = config.get("probe_lock", "/tmp/lockbot-telemetry-probe.lock")
        flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), "a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cards = parse_xml(run_xml(config["command"], config.get("timeout_seconds", 10)))
            observed_at = time.time()
        result["cards"] = []
        details = process_metadata(cards, proc)
        for card in cards:
            item = dataclasses.asdict(card)
            item["process_count"] = len(card.processes) if card.processes is not None else None
            item["container_count"] = container_count(card.processes, proc)
            item["process_info"] = [details[pid] for pid in (card.processes or ())[:128] if pid in details]
            item["process_info_truncated"] = bool(
                card.processes and (len(card.processes) > 128 or any(pid not in details for pid in card.processes))
            )
            item.pop("processes", None)
            item.pop("model", None)
            item.pop("pci_bus", None)
            item.pop("uuid", None)
            result["cards"].append(item)
        result["accelerator_observed_at"] = observed_at
        result["accelerator_success"] = True
    except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired):
        pass
    result["accelerator_duration"] = time.monotonic() - started
    if config.get("collect_inventory") and result["accelerator_success"]:
        with suppress(OSError, ValueError, TypeError, KeyError):
            result["hardware_inventory"] = hardware_inventory(config, proc, cards, observed_at)
    return result


class FleetCollector:
    """Bounded SSH fanout over an explicit inventory; HTTP never schedules probes."""

    def __init__(self, config):
        self.config = config
        self.targets = config["targets"]
        self.lock = threading.Lock()
        self.bodies = {}
        self.statuses = {}
        self.previous_cpu = {}
        # Cache code once. It has no environment-specific values or credentials.
        self.probe_source = Path(__file__).read_text().split('\nif __name__ == "__main__":')[0]

    def collect(self, target):
        node = target["node"]
        try:
            remote_config = {
                key: value
                for key, value in target.items()
                if key in {"command", "timeout_seconds", "filesystems", "proc_root", "probe_lock"}
            }
            if target.get("inventory_output"):
                remote_config.update(
                    collect_inventory=True,
                    node=node,
                    vendor=target["vendor"],
                    inventory_scope=target.get("inventory_scope", "unknown"),
                )
            if target.get("ssh_target"):
                source = self.probe_source + "\nprint(json.dumps(probe(" + repr(remote_config) + ")))"
                remote = "python3 -c " + shlex.quote(source)
                command = [
                    "ssh",
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "ControlPath=none",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "ServerAliveInterval=5",
                    "-o",
                    "ServerAliveCountMax=1",
                    "-p",
                    str(target.get("ssh_port", 22)),
                    target["ssh_target"],
                    remote,
                ]
                if self.config.get("known_hosts"):
                    command[1:1] = ["-o", "UserKnownHostsFile=" + self.config["known_hosts"]]
                data = json.loads(run_xml(command, 25))
            else:
                data = probe(remote_config)
            if not isinstance(data, dict):
                raise ValueError("invalid probe response")
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            for kind in ("host", "accelerator"):
                self.failure(node, kind, 0)
            return False
        host_ok = self.record_sample(target, data, "host")
        accelerator_ok = self.record_sample(target, data, "accelerator")
        if accelerator_ok and target.get("inventory_output") and data.get("hardware_inventory"):
            with suppress(OSError, ValueError, TypeError, KeyError):
                _store_hardware_inventory(target["inventory_output"], data["hardware_inventory"])
        return host_ok or accelerator_ok

    def record_sample(self, target, data, kind):
        node = target["node"]
        duration = data.get(kind + "_duration", 0)
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            duration = 0
        if data.get(kind + "_success") is True:
            try:
                stamp = data[kind + "_observed_at"]
                if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                    raise ValueError("invalid observation time")
                body = self.render_data(target, data, kind)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                pass
            else:
                with self.lock:
                    self.bodies[node, kind] = body
                    self.statuses[node, kind] = (1, duration, stamp)
                return True
        self.failure(node, kind, duration)
        return False

    def failure(self, node, kind, duration):
        with self.lock:
            previous = self.statuses.get((node, kind), (0, 0, 0))[2]
            self.statuses[node, kind] = (0, duration, previous)

    def render_data(self, target, data, kind):
        node, body = target["node"], ""
        if kind == "accelerator":
            for card in data["cards"]:
                tags = {"node": node, "device": card["device"], "vendor": target["vendor"]}
                for suffix, key in [
                    ("utilization_percent", "utilization"),
                    ("memory_used_bytes", "memory_used"),
                    ("memory_total_bytes", "memory_total"),
                    ("process_count", "process_count"),
                    ("container_count", "container_count"),
                ]:
                    body += sample("lockbot_accelerator_" + suffix, card.get(key), tags)
                for detail in card.get("process_info", []):
                    body += sample("lockbot_accelerator_process_info", 1, {**tags, **detail})
                body += sample(
                    "lockbot_accelerator_process_info_truncated", int(card.get("process_info_truncated", False)), tags
                )
        else:
            tags = {"node": node}
            cpu = data["cpu"]
            previous = self.previous_cpu.get(node)
            if previous:
                total, idle = (a - b for a, b in zip(cpu, previous, strict=True))
                utilization = (total - idle) / total * 100 if total > 0 and 0 <= idle <= total else None
                body += sample("lockbot_host_cpu_utilization_percent", utilization, tags)
            self.previous_cpu[node] = cpu
            body += sample("lockbot_host_memory_used_bytes", data["memory"][0], tags)
            body += sample("lockbot_host_memory_total_bytes", data["memory"][1], tags)
            for name in ("available_bytes", "cache_bytes"):
                body += sample("lockbot_host_memory_" + name, data.get("memory_details", {}).get(name), tags)
            for alias, (used, total) in data["filesystems"].items():
                disk_tags = {**tags, "filesystem": alias}
                body += sample("lockbot_host_filesystem_used_bytes", used, disk_tags)
                body += sample("lockbot_host_filesystem_total_bytes", total, disk_tags)
        return body

    def render(self):
        with self.lock:
            body = "".join(self.bodies.values())
            for (node, kind), (success, duration, stamp) in sorted(self.statuses.items()):
                tags = {"node": node, "collector": kind}
                body += sample("lockbot_collector_success", success, tags)
                body += sample("lockbot_collector_duration_seconds", duration, tags)
                body += sample("lockbot_sample_timestamp_seconds", stamp, tags)
            return body.encode()

    def run(self, stop):
        interval = self.config.get("interval_seconds", 300)
        workers = min(2, self.config.get("max_workers", 2))
        # Spread the first acquisition across the interval, then preserve per-target cadence.
        deadlines = {
            target["node"]: time.monotonic() + (random.uniform(0, interval) if target.get("ssh_target") else 0)
            for target in self.targets
        }
        futures, consecutive_failures = {}, {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while not stop.is_set():
                now = time.monotonic()
                for node, future in list(futures.items()):
                    if not future.done():
                        continue
                    del futures[node]
                    try:
                        success = future.result()
                    except Exception as error:
                        logger.warning("Probe failed for %s (%s)", node, type(error).__name__)
                        for kind in ("host", "accelerator"):
                            self.failure(node, kind, 0)
                        success = False
                    failures = 0 if success else min(consecutive_failures.get(node, 0) + 1, 2)
                    consecutive_failures[node] = failures
                    deadlines[node] = now + interval * (2**failures) + random.uniform(0, interval * 0.1)
                for target in sorted(self.targets, key=lambda target: deadlines[target["node"]]):
                    node = target["node"]
                    if len(futures) >= workers or deadlines[node] > now:
                        break
                    if node not in futures:
                        futures[node] = executor.submit(self.collect, target)
                stop.wait(0.5)


def validate_fleet(config):
    targets = config.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise ValueError("configure 1 to 64 explicit targets")
    if len({target.get("node") for target in targets}) != len(targets):
        raise ValueError("node labels must be unique")
    if not isinstance(config.get("interval_seconds", 300), int) or config.get("interval_seconds", 300) < MIN_INTERVAL:
        raise ValueError("sampling interval must be at least 60 seconds")
    if type(config.get("max_workers", 2)) is not int or config.get("max_workers", 2) < 1:
        raise ValueError("max_workers must be a positive integer")
    for target in targets:
        validate_config(target)
        ssh_target = target.get("ssh_target")
        if ssh_target and (
            not isinstance(ssh_target, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+", ssh_target)
        ):
            raise ValueError("SSH targets must be explicit user@host values")
        if not isinstance(target.get("ssh_port", 22), int) or not 1 <= target.get("ssh_port", 22) <= 65535:
            raise ValueError("invalid SSH port")
    return config


def serve(config, listen="127.0.0.1", port=9911, lock_path="/tmp/lockbot-resource-exporter.lock"):
    collector = FleetCollector(validate_fleet(config)) if "targets" in config else Collector(validate_config(config))
    flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock_path, flags, 0o600), "a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        thread = threading.Thread(target=collector.run, args=(stop,), daemon=True)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in {"/metrics", "/health"}:
                    self.send_error(404)
                    return
                body = b"ok\n" if self.path == "/health" else collector.render()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer((listen, port), Handler)
        thread.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            server.server_close()
            thread.join(timeout=35)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9911)
    parser.add_argument("--lock-file", default="/tmp/lockbot-resource-exporter.lock")
    args = parser.parse_args()
    serve(json.loads(Path(args.config).read_text()), args.listen, args.port, args.lock_file)


if __name__ == "__main__":
    main()
