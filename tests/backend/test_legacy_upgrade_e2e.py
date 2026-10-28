"""Real TCP startup/restart against old SQLite + legacy state, using only fake IM."""

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from cryptography.fernet import Fernet


def _seed_legacy_deployment(root):
    """Pre-Agent bots schema, with no resource-auth or managed-identity columns."""
    root.mkdir()
    with sqlite3.connect(root / "lockbot.db") as db:
        db.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL, max_running_bots INTEGER DEFAULT 10,
                must_change_password BOOLEAN NOT NULL DEFAULT 0, token_version INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, bot_type TEXT NOT NULL, platform TEXT,
                group_id TEXT, last_user_id TEXT, webhook_url TEXT NOT NULL,
                aes_key TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '',
                cluster_configs TEXT NOT NULL, status TEXT, pid INTEGER,
                consecutive_failures INTEGER DEFAULT 0, last_request_at DATETIME,
                config_overrides TEXT DEFAULT '{}', api_key TEXT,
                is_deleted BOOLEAN NOT NULL DEFAULT 0, deleted_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users (id,username,email,password_hash,role)
            VALUES (1,'legacy-owner','owner@test.invalid','unused','admin');
        """)
        originals = {}
        for bot_id, kind in enumerate(["NODE", "DEVICE", "QUEUE"], 1):
            cluster = {"n1": ["GPU"]} if kind == "DEVICE" else ["n1"]
            db.execute(
                """INSERT INTO bots
                   (id,user_id,name,bot_type,platform,webhook_url,cluster_configs,status,config_overrides,api_key)
                   VALUES (?,1,?,?,'fakeim','',?,'running',?,?)""",
                (
                    bot_id,
                    f"legacy-{kind}",
                    kind,
                    json.dumps(cluster),
                    json.dumps({"MAX_LOCK_DURATION": -1, "EARLY_NOTIFY": False}),
                    f"lbk_test_upgrade_{bot_id}",
                ),
            )
            holder = {"user_id": "alice", "start_time": int(time.time()), "duration": 7200, "is_notified": False}
            node = {"status": "exclusive", "current_users": [holder], "booking_list": []}
            if kind == "QUEUE":
                node["booking_list"] = [{"user_id": "bob", "duration": 1800, "start_time": 0, "is_notified": False}]
            state = {"n1": [{"dev_id": 0, "dev_model": "GPU", **node}] if kind == "DEVICE" else node}
            legacy = root / "bots" / str(bot_id) / "cluster_status.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"cluster_status": state}))
            originals[bot_id] = legacy.read_bytes()
        return originals


@contextmanager
def _server(root, private, log_path):
    repository = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(
        os.environ, DATA_DIR=str(root), PYTHONPATH=str(repository / "python"), DEV_MODE="false", ALLOW_REGISTER="false"
    )
    env.pop("DATABASE_URL", None)
    env.pop("LOCKBOT_SKIP_STATE_OWNERSHIP", None)
    with log_path.open("w") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str(repository / "tests/integration/http_contract_probe.py"),
                "--serve",
                str(port),
                str(private),
            ],
            cwd=repository,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                for _ in range(100):
                    assert child.poll() is None, log_path.read_text()[-4000:]
                    try:
                        if client.get("/api/health").json() == {"status": "ok"}:
                            break
                    except (httpx.ConnectError, httpx.ReadError):
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("Isolated upgrade server did not become ready")
                yield client
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise AssertionError("Isolated upgrade server did not shut down cleanly") from None


def test_legacy_startup_migrates_and_recovers_live_state_across_restart(tmp_path):
    root = tmp_path / "legacy-data"
    originals = _seed_legacy_deployment(root)
    private = tmp_path / "private.json"
    private.write_text(
        json.dumps(
            {
                "password": secrets.token_urlsafe(24),
                "jwt_secret": secrets.token_urlsafe(48),
                "encryption_key": Fernet.generate_key().decode(),
            }
        )
    )
    private.chmod(0o600)
    first_uuids = None
    for boot in range(2):
        with _server(root, private, tmp_path / f"boot-{boot}.log") as client:
            for bot_id, kind in enumerate(["NODE", "DEVICE", "QUEUE"], 1):
                response = client.get(
                    f"/api/bots/{bot_id}/op/resources", headers={"Authorization": f"Bearer lbk_test_upgrade_{bot_id}"}
                )
                assert response.status_code == 200, response.text
                snapshot = response.json()
                # This route requires an admitted, recovered live instance, not file fallback.
                assert snapshot["health"] == "running" and snapshot["bot_type"] == kind
                node = snapshot["resources"][0]
                holders = node["devices"][0]["holders"] if kind == "DEVICE" else node["holders"]
                assert [h["external_user_id"] for h in holders] == ["alice"]
                assert holders[0]["duration_seconds"] == 7200
                if kind == "QUEUE":
                    assert [u["external_user_id"] for u in node["booking_list"]] == ["bob"]
                    assert node["booking_list"][0]["position"] == 0
                directory = root / "bots" / str(bot_id)
                assert not (directory / "cluster_status.json").exists()
                assert (directory / "bot_state.json").read_bytes() == originals[bot_id]
            with sqlite3.connect(root / "lockbot.db") as db:
                rows = db.execute(
                    "SELECT id,uuid,resource_auth_enabled,resource_api_notify_enabled FROM bots ORDER BY id"
                ).fetchall()
                assert len(rows) == 3 and all(uuid and enabled == notify == 1 for _, uuid, enabled, notify in rows)
                uuids = [uuid for _, uuid, _, _ in rows]
                if first_uuids is None:
                    first_uuids = uuids
                else:
                    assert uuids == first_uuids
                assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert (root / ".state-store-owner.lock").is_file()
