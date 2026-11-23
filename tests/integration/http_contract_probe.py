"""Real TCP contract probe, using a temporary loopback-only fake IM test server.

This never registers a fake adapter in the deployed service or contacts a real IM.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


def serve(port, private_file):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
    import lockbot.backend.app.config as config
    from fake_adapter import FakeAdapter
    from lockbot.core.platforms.registry import register_adapter

    settings = json.loads(Path(private_file).read_text())
    config.JWT_SECRET = settings["jwt_secret"]
    config.ENCRYPTION_KEY = settings["encryption_key"]
    register_adapter(FakeAdapter)
    import uvicorn
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password
    from lockbot.backend.app.database import SessionLocal
    from lockbot.backend.app.main import app

    original = app.router.lifespan_context

    @asynccontextmanager
    async def bootstrap(application):
        async with original(application):
            with SessionLocal() as db:
                if db.query(User).filter_by(username="probe-admin").first() is None:
                    db.add(
                        User(
                            username="probe-admin",
                            email="probe@test.invalid",
                            role="super_admin",
                            password_hash=_hash_password(settings["password"]),
                        )
                    )
                db.commit()
            yield

    app.router.lifespan_context = bootstrap
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, access_log=False, log_level="warning")


def probe(output):
    import hashlib

    import httpx
    from cryptography.fernet import Fernet

    with tempfile.TemporaryDirectory(prefix="lockbot-http-contract-") as temporary:
        directory = Path(temporary)
        private = directory / "test-credentials.json"
        settings = {
            "password": secrets.token_urlsafe(24),
            "jwt_secret": secrets.token_urlsafe(48),
            "encryption_key": Fernet.generate_key().decode(),
        }
        private.write_text(json.dumps(settings))
        private.chmod(0o600)
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        env = dict(
            os.environ,
            DATA_DIR=str(directory / "data"),
            DEV_MODE="false",
            ALLOW_REGISTER="false",
            PLATFORM_URL=f"http://127.0.0.1:{port}",
            RESOURCE_AUTH_V1_ENROLLMENT_ENABLED="true",
            RESOURCE_API_TIER_DISCOVERY_SECONDS="3",
        )
        env.pop("DATABASE_URL", None)
        env.pop("LOCKBOT_SKIP_STATE_OWNERSHIP", None)
        with (directory / "server.log").open("w") as log:
            child = subprocess.Popen(
                [sys.executable, __file__, "--serve", str(port), str(private)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        evidence = []
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=15, headers={"Connection": "close"}, trust_env=False
            ) as client:
                for _ in range(60):
                    if child.poll() is not None:
                        raise RuntimeError("Probe server exited before readiness")
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Probe server did not become ready")

                def request(method, path, expected=200, **kwargs):
                    response = client.request(method, path, **kwargs)
                    assert response.status_code == expected, (
                        f"{method} {path}: {response.status_code}, expected {expected}"
                    )
                    evidence.append({"method": method, "path": path, "status": response.status_code})
                    return response

                skill = request("GET", "/api/agent/v2/manifest").json()["skill"]
                skill_zip = request("GET", skill["download_url"])
                assert skill_zip.headers["content-type"] == "application/zip"
                assert hashlib.sha256(skill_zip.content).hexdigest() == skill["sha256"]
                with ZipFile(BytesIO(skill_zip.content)) as bundle:
                    assert set(bundle.namelist()) == {"lockbot/SKILL.md", "lockbot/agents/openai.yaml"}
                    skill_text = bundle.read("lockbot/SKILL.md")
                assert f"lb login http://127.0.0.1:{port}".encode() in skill_text
                for obsolete in ("/api/agent-skill", "/api/agent-skill/lockbot-resource-agent.zip"):
                    response = request("GET", obsolete, expected=404)
                    assert "location" not in response.headers

                login = request(
                    "POST", "/api/auth/login", json={"username": "probe-admin", "password": settings["password"]}
                )
                admin = {"Authorization": "Bearer " + login.json()["access_token"]}
                created = request(
                    "POST",
                    "/api/bots",
                    expected=201,
                    headers=admin,
                    json={
                        "name": "tcp-contract-only",
                        "bot_type": "NODE",
                        "platform": "fakeim",
                        "webhook_url": "https://example.invalid/unused",
                        "cluster_configs": ["n1", "n2"],
                    },
                )
                bot_id = created.json()["id"]
                assert created.json()["status"] == "running"
                request("PUT", f"/api/bots/{bot_id}/resource-auth", headers=admin, json={"enabled": True})

                def enroll(actor):
                    issued = request(
                        "POST", "/api/resource-auth/bindings", expected=201, json={"label": "synthetic " + actor}
                    ).json()
                    headers = {"Authorization": "Bearer " + issued["credential"]}
                    request(
                        "POST", f"/api/bots/{bot_id}/commands", expected=401, headers=headers, json={"command": "query"}
                    )
                    request(
                        "POST",
                        f"/api/bots/webhook/{bot_id}?signature=fake-valid-signature",
                        json={
                            "envelope": {
                                "actor": actor,
                                "conversation": "test-only",
                                "text": "bind " + issued["bind_code"],
                            }
                        },
                    )
                    status = request("GET", "/api/resource-auth/bindings/" + issued["binding_id"]).json()
                    assert status["status"] == "confirmed"
                    return headers

                alice = enroll("synthetic-alice")
                bob = enroll("synthetic-bob")
                commands = f"/api/bots/{bot_id}/commands"
                resources = f"/api/bots/{bot_id}/resources"
                usage = f"/api/bots/{bot_id}/usage-declarations"
                # A second Agent cannot create another effective key for the same owner/bot.
                duplicate = request("POST", "/api/resource-auth/bindings", expected=201, json={}).json()
                request(
                    "POST",
                    f"/api/bots/webhook/{bot_id}?signature=fake-valid-signature",
                    json={
                        "envelope": {
                            "actor": "synthetic-alice",
                            "conversation": "test-only",
                            "text": "bind " + duplicate["bind_code"],
                        }
                    },
                )
                assert (
                    request("GET", "/api/resource-auth/bindings/" + duplicate["binding_id"]).json()["status"]
                    == "expired"
                )
                request(
                    "POST",
                    commands,
                    expected=401,
                    headers={"Authorization": "Bearer " + duplicate["credential"]},
                    json={"command": "query"},
                )
                # Independent HTTP clients can share the original credential.
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as second_agent:
                    shared = second_agent.get(usage, headers=alice)
                    assert shared.status_code == 200
                    evidence.append({"method": "GET", "path": usage, "status": 200, "client": "second-agent"})
                request("GET", resources, expected=401)
                request("POST", commands, headers=alice, json={"command": "lock n1 10m"})
                snapshot = request("GET", resources, headers=alice).json()
                assert any(
                    h["external_user_id"] == "synthetic-alice"
                    for r in snapshot["resources"]
                    if r["id"] == "n1"
                    for h in r["holders"]
                )
                # The owner switch suspends personal Agent requests, while OP access stays separate.
                op_key = request("POST", f"/api/bots/{bot_id}/api-key", headers=admin).json()["api_key"]
                request("PUT", f"/api/bots/{bot_id}/resource-auth", headers=admin, json={"enabled": False})
                disabled = request("POST", commands, expected=403, headers=alice, json={"command": "query"})
                assert disabled.json()["detail"]["code"] == "agent_access_disabled"
                request("GET", f"/api/bots/{bot_id}/locked-users", headers={"Authorization": "Bearer " + op_key})
                request("PUT", f"/api/bots/{bot_id}/resource-auth", headers=admin, json={"enabled": True})
                request("POST", commands, headers=alice, json={"command": "query"})
                purpose = "temporary HTTP contract experiment"
                category = "functional_testing"
                request("PUT", usage + "/n1", headers=alice, json={"purpose": purpose, "task_category": category})
                declaration = request("GET", usage, headers=bob).json()["items"][0]
                assert (
                    declaration["actor"]["external_user_id"] == "synthetic-alice"
                    and declaration["task_category"] == category
                    and "purpose" not in declaration
                )
                shared_resources = request("GET", resources, headers=bob).json()
                embedded = next(r for r in shared_resources["resources"] if r["id"] == "n1")["holders"][0]["usage"]
                assert embedded["task_category"] == category and "purpose" not in embedded
                assert embedded["actor"]["external_user_id"] == "synthetic-alice"
                # Restart the temporary server against the same private SQLite/state files.
                # Both credentials and usage/statistics must survive an actual process exit.
                child.terminate()
                child.wait(20)
                with (directory / "server.log").open("a") as log:
                    child = subprocess.Popen(
                        [sys.executable, __file__, "--serve", str(port), str(private)],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                for _ in range(60):
                    if child.poll() is not None:
                        raise RuntimeError("Probe server failed to recover")
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Recovered probe server did not become ready")
                restored = request("GET", resources, headers=alice).json()
                assert any(
                    h["external_user_id"] == "synthetic-alice"
                    for r in restored["resources"]
                    if r["id"] == "n1"
                    for h in r["holders"]
                )
                assert (
                    next(r for r in restored["resources"] if r["id"] == "n1")["holders"][0]["usage"]["purpose"]
                    == purpose
                )
                public_declaration = request("GET", usage, headers=bob).json()["items"][0]
                assert public_declaration["task_category"] == category and "purpose" not in public_declaration
                request("PUT", usage + "/n1", expected=409, headers=bob, json={"purpose": "unauthorized holder"})
                request("POST", commands, expected=403, headers=alice, json={"command": "kickout n1"})
                request("POST", commands, headers=alice, json={"command": "unlock n1"})
                # A resource change permits an immediate refreshed snapshot.
                released = request("GET", resources, headers=alice).json()
                assert next(r for r in released["resources"] if r["id"] == "n1")["holders"] == []
                limited = request("GET", resources, expected=429, headers=alice)
                retry = int(limited.headers["Retry-After"])
                assert 1 <= retry <= 3
                time.sleep(retry + 0.2)
                snapshot = request("GET", resources, headers=alice).json()
                assert next(r for r in snapshot["resources"] if r["id"] == "n1")["holders"] == []
                assert request("GET", usage, headers=bob).json()["items"] == []
                request(
                    "POST",
                    f"/api/bots/webhook/{bot_id}?signature=fake-valid-signature",
                    json={"envelope": {"actor": "synthetic-alice", "conversation": "test-only", "text": "unbind"}},
                )
                request("POST", commands, expected=401, headers=alice, json={"command": "query"})
                stats = request(
                    "GET", f"/api/bots/{bot_id}/agent-activity", headers=admin, params={"status": "history"}
                ).json()["items"]
                alice_stats = next(item for item in stats if item["external_user_id"] == "synthetic-alice")
                assert alice_stats["limited_requests"] >= 1 and alice_stats["refused_requests"] >= 2
                assert alice_stats["read_requests"] >= 4, "statistics must accumulate across restart"
                request("PUT", f"/api/bots/{bot_id}/owner", expected=404, headers=admin, json={"username": "anyone"})
                result = {
                    "passed": True,
                    "transport": "real TCP loopback",
                    "identity_source": "synthetic fakeim test adapter, not real IM acceptance",
                    "requests": evidence,
                    "agent_statistics": stats,
                    "production_adapter_registered": False,
                    "shared_key_and_plain_unbind_verified": True,
                    "public_skill_verified": True,
                    "embedded_purpose_verified": True,
                    "skill_sha256": skill["sha256"],
                    "agent_access_switch_verified": True,
                    "op_key_independent_verified": True,
                    "restart_persistence_verified": ["credential", "holding", "purpose", "statistics"],
                }
                Path(output).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
                print(json.dumps({"passed": True, "requests": len(evidence), "output": output}))
        finally:
            child.terminate()
            try:
                child.wait(20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), sys.argv[3])
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--output", required=True)
        probe(parser.parse_args().output)
