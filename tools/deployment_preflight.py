#!/usr/bin/env python3
"""Read-only deployment checks. Never source shell files, print secrets, or start services."""

import argparse
import base64
import json
import re
import sqlite3
import stat
from pathlib import Path


def inspect_environment(path):
    """Accept plain KEY=VALUE files; do not evaluate shell expansion or dotenv hooks."""
    if path.stat().st_size > 65536:
        raise ValueError("environment_file_too_large")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError("invalid_or_duplicate_environment_key")
        if (
            value[:1] in {"'", '"'}
            or any(char in value for char in ("$", "`", "\x00"))
            or any(char.isspace() for char in value)
        ):
            raise ValueError("use_plain_values_without_shell_expansion")
        values[key] = value
    return values


def check(env_file, data_dir=None, upgrade=False):
    checks = []

    def record(name, ok, reason):
        checks.append({"check": name, "ok": bool(ok), "reason": reason})

    try:
        values = inspect_environment(env_file)
        mode = stat.S_IMODE(env_file.stat().st_mode)
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "checks": [{"check": "environment", "ok": False, "reason": "unreadable_or_invalid"}]}
    record("environment_permissions", mode & 0o077 == 0, "private_file_required")
    secret = values.get("JWT_SECRET", "")
    record(
        "jwt_secret",
        len(secret) >= 32 and "replace" not in secret.lower() and "dev-secret" not in secret,
        "non_placeholder_secret_required",
    )
    try:
        key = values.get("ENCRYPTION_KEY", "")
        valid_key = len(key) == 44 and len(base64.b64decode(key, altchars=b"-_", validate=True)) == 32
    except (ValueError, TypeError):
        valid_key = False
    record("encryption_key", valid_key, "fernet_key_required_preserve_existing_on_upgrade")
    image = values.get("LOCKBOT_IMAGE", "")
    versioned = (
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]+", image))
        and "replace" not in image.lower()
        and ":" in image.rsplit("/", 1)[-1]
        and not image.endswith(":latest")
        and ("@" not in image or bool(re.search(r"@sha256:[a-f0-9]{64}\Z", image)))
    )
    record("image_reference", versioned, "pinned_tag_or_sha256_digest_required")
    port = values.get("LOCKBOT_PORT", "8000")
    record(
        "published_port",
        1 <= len(port) <= 5 and port.isdecimal() and 1 <= int(port) <= 65535,
        "valid_port_required_preserve_existing_on_upgrade",
    )
    record(
        "development_mode",
        values.get("DEV_MODE", "false").lower() not in {"1", "true", "yes"},
        "development_mode_must_be_disabled",
    )
    record("single_writer", values.get("WEB_CONCURRENCY", "1") == "1", "one_process_per_database_and_state_directory")
    if upgrade:
        record("existing_data_selected", data_dir is not None, "explicit_existing_data_directory_required")
    if data_dir is not None:
        database = data_dir / "lockbot.db"
        if not database.is_file():
            record("database", not upgrade, "existing_database_missing" if upgrade else "new_install_no_database")
        else:
            try:
                # URI mode=ro never creates a missing database or changes schema.
                with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                    valid = connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                record("database", valid and {"users", "bots"} <= tables, "read_only_integrity_and_platform_tables")
            except sqlite3.Error:
                record("database", False, "database_unreadable_or_busy")
    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "limits": [
            "No service was started or changed",
            "No network, image, port availability or IM delivery was tested",
            "This is not a consistent backup or an upgrade compatibility proof",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, help="Host-visible data directory, never a Docker volume name")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    result = check(args.env_file, args.data_dir, args.upgrade)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
