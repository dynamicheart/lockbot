"""Exercise the shipped read-only CLI against disposable configuration and SQLite data."""

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/deployment_preflight.py"


def environment(tmp_path, **updates):
    values = {
        "LOCKBOT_IMAGE": "ghcr.io/dynamicheart/lockbot:v2.15.2",
        "JWT_SECRET": "private-test-canary-" + "a" * 32,
        "LOCKBOT_PORT": "8000",
    }
    # Use exactly 32 bytes without creating a usable service or credential.
    values["ENCRYPTION_KEY"] = base64.urlsafe_b64encode(bytes(range(32))).decode()
    values.update(updates)
    target = tmp_path / "private.env"
    target.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    target.chmod(0o600)
    return target


def run(env, *args):
    result = subprocess.run(
        [sys.executable, str(TOOL), "--env-file", str(env), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "private-test-canary" not in result.stdout + result.stderr
    assert base64.urlsafe_b64encode(bytes(range(32))).decode() not in result.stdout + result.stderr
    return result.returncode, json.loads(result.stdout)


def test_new_install_check_never_creates_data_or_changes_configuration(tmp_path):
    env = environment(tmp_path)
    before = env.read_bytes()
    data = tmp_path / "not-created"
    code, report = run(env, "--data-dir", data)
    assert code == 0 and report["ok"]
    assert env.read_bytes() == before and not data.exists()
    assert len(report["limits"]) == 3


def test_upgrade_reads_existing_database_without_modifying_it(tmp_path):
    env = environment(tmp_path)
    data = tmp_path / "existing"
    data.mkdir()
    database = data / "lockbot.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE bots(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO bots VALUES (4)")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    code, report = run(env, "--upgrade", "--data-dir", data)
    assert code == 0 and report["ok"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    assert list(data.iterdir()) == [database]


@pytest.mark.parametrize(
    "updates,failed",
    [
        ({"JWT_SECRET": "replace-with-secret"}, "jwt_secret"),
        ({"ENCRYPTION_KEY": "invalid"}, "encryption_key"),
        ({"LOCKBOT_IMAGE": "ghcr.io/dynamicheart/lockbot:latest"}, "image_reference"),
        ({"LOCKBOT_IMAGE": "image@sha256:bad"}, "image_reference"),
        ({"WEB_CONCURRENCY": "2"}, "single_writer"),
        ({"DEV_MODE": "true"}, "development_mode"),
        ({"LOCKBOT_PORT": "70000"}, "published_port"),
    ],
)
def test_actionable_failed_checks_do_not_echo_values(tmp_path, updates, failed):
    code, report = run(environment(tmp_path, **updates))
    assert code == 1 and not report["ok"]
    assert any(item["check"] == failed and not item["ok"] for item in report["checks"])


def test_shell_text_is_not_executed_and_missing_upgrade_data_is_not_created(tmp_path):
    env = environment(tmp_path)
    marker = tmp_path / "must-not-exist"
    env.write_text(env.read_text() + f"IGNORED=$(touch {marker})\n")
    assert run(env)[0] == 1
    assert not marker.exists()
    env = environment(tmp_path)
    code, report = run(env, "--upgrade")
    assert code == 1 and not report["ok"]
    assert any(item["check"] == "existing_data_selected" and not item["ok"] for item in report["checks"])


def test_shared_environment_file_is_rejected(tmp_path):
    env = environment(tmp_path)
    env.chmod(0o644)
    assert run(env)[0] == 1
