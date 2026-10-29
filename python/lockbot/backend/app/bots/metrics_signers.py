"""Deployment-only, source-specific metrics signing keys. Never read paths from HTTP input."""

import hashlib
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_CONFIG_BYTES = 64 * 1024
MAX_SOURCES = 64
MAX_KEY_BYTES = 4096
REGISTRY_ENV = "METRICS_GRANT_SIGNERS_FILE"


@dataclass(frozen=True, repr=False)
class MetricsSigner:
    audience: str
    secret: bytes


def canonical_audience(value):
    """Require one unambiguous spelling; never move grants between URL aliases."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or any(char in value for char in ("?", "#", "\\", "%"))
    ):
        raise ValueError("Invalid metrics audience")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid metrics audience")
    host = parsed.hostname
    if ":" in host:
        host = "[" + ipaddress.IPv6Address(host).compressed + "]"
    else:
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
        ):
            raise ValueError("Invalid metrics audience")
        # Browsers accept alternative numeric IP spellings; require dotted decimal.
        if all(re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label) for label in host.split(".")):
            host = str(ipaddress.IPv4Address(host))
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid metrics audience")
    authority = host + (f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else "")
    path = parsed.path.rstrip("/")
    if (
        not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", path)
        or any(part in {".", ".."} for part in path.split("/"))
        or "//" in parsed.path
    ):
        raise ValueError("Invalid metrics audience")
    canonical = urlunsplit((parsed.scheme, authority, path, "", ""))
    if canonical != value:
        raise ValueError("Use the canonical metrics audience")
    return canonical


def _private_file(value, maximum):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Private file requires an absolute deployment path")
    path = Path(value)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Private file must be regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid not in {0, os.geteuid()}
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
            or not 1 <= info.st_size <= maximum
        ):
            raise ValueError("Unsafe private file")
        body = bytearray()
        while len(body) <= maximum:
            chunk = os.read(descriptor, min(8192, maximum + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(body) != info.st_size
            or len(body) > maximum
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        ):
            raise ValueError("Private file changed during read")
        return bytes(body), (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate metrics signer configuration field")
        result[key] = value
    return result


def _registry_signer(endpoint):
    audience = canonical_audience(endpoint)
    body, registry_identity = _private_file(os.environ[REGISTRY_ENV], MAX_CONFIG_BYTES)
    config = json.loads(body, object_pairs_hook=_unique_object)
    if (
        not isinstance(config, dict)
        or set(config) != {"version", "sources"}
        or type(config["version"]) is not int
        or config["version"] != 1
        or not isinstance(config["sources"], list)
        or not 1 <= len(config["sources"]) <= MAX_SOURCES
    ):
        raise ValueError("Invalid metrics signer configuration")
    audiences, identities, digests = set(), {registry_identity}, set()
    selected = None
    for source in config["sources"]:
        if not isinstance(source, dict) or set(source) != {"audience", "key_file"}:
            raise ValueError("Invalid metrics signer source")
        target = canonical_audience(source["audience"])
        secret, identity = _private_file(source["key_file"], MAX_KEY_BYTES)
        digest = hashlib.sha256(secret).digest()
        if len(secret) < 32 or target in audiences or identity in identities or digest in digests:
            raise ValueError("Metrics signer sources must be distinct")
        audiences.add(target)
        identities.add(identity)
        digests.add(digest)
        if target == audience:
            selected = MetricsSigner(target, secret)
    return selected


def select_metrics_signer(endpoint):
    """Reload on every issuance; configured registry errors must never fall back."""
    if not isinstance(endpoint, str):
        return None
    try:
        if REGISTRY_ENV in os.environ:
            return _registry_signer(endpoint)
        # Preserve the existing single-file mode and exact audience spelling.
        key_path = os.environ.get("METRICS_GRANT_SECRET_FILE", "")
        if not key_path:
            return None
        path = Path(key_path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_KEY_BYTES:
            return None
        secret = path.read_bytes()
        if len(secret) < 32:
            return None
        return MetricsSigner(endpoint.rstrip("/"), secret)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        # This value crosses public context/view/test paths: expose no private paths.
        return None
