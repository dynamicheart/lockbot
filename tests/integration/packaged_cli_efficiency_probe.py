"""Count actual TCP requests from the served CLI against an isolated fake-IM service.

No model provider, real IM application, real resource, or deployed instance is used.
Pass --source to compare another checkout using the exact same probe.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile


def serve(port, settings, evidence):
    from http_contract_probe import serve as contract_server

    contract_server(port, settings, request_evidence=evidence)


def probe(source, output):
    import httpx
    from cryptography.fernet import Fernet

    with tempfile.TemporaryDirectory(prefix="lockbot-cli-efficiency-") as temporary:
        root = Path(temporary)
        private = root / "credentials.json"
        credentials = {
            "password": secrets.token_urlsafe(24),
            "jwt_secret": secrets.token_urlsafe(48),
            "encryption_key": Fernet.generate_key().decode(),
        }
        private.write_text(json.dumps(credentials))
        private.chmod(0o600)
        events = root / "requests.jsonl"
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = dict(
            os.environ,
            PYTHONPATH=str(source / "python"),
            DATA_DIR=str(root / "data"),
            DEV_MODE="false",
            ALLOW_REGISTER="false",
            PLATFORM_URL=origin,
        )
        environment.pop("DATABASE_URL", None)
        environment.pop("LOCKBOT_SKIP_STATE_OWNERSHIP", None)
        # Use this probe's instrumentation even when testing a baseline checkout.
        with (root / "server.log").open("w") as log:
            child = subprocess.Popen(
                [sys.executable, __file__, "--serve", str(port), str(private), str(events)],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError("Isolated probe server exited")
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated probe readiness timed out")

                def request(method, path, **kwargs):
                    response = client.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response

                login = request(
                    "POST", "/api/auth/login", json={"username": "probe-admin", "password": credentials["password"]}
                ).json()
                admin = {"Authorization": "Bearer " + login["access_token"]}
                bot = request(
                    "POST",
                    "/api/bots",
                    headers=admin,
                    json={
                        "name": "cli-efficiency-synthetic",
                        "bot_type": "NODE",
                        "platform": "fakeim",
                        "webhook_url": "https://example.invalid/unused",
                        "cluster_configs": ["n1", "n2"],
                    },
                ).json()
                request("PUT", f"/api/bots/{bot['id']}/resource-auth", headers=admin, json={"enabled": True})
                binding = request("POST", "/api/resource-auth/bindings", json={}).json()
                request(
                    "POST",
                    f"/api/bots/webhook/{bot['id']}?signature=fake-valid-signature",
                    json={
                        "envelope": {
                            "actor": "synthetic-cli-user",
                            "conversation": "test-only",
                            "text": "bind " + binding["bind_code"],
                        }
                    },
                )
                assert (
                    request("GET", "/api/resource-auth/bindings/" + binding["binding_id"]).json()["status"]
                    == "confirmed"
                )
                manifest = request("GET", "/api/agent-skill").json()
                bundle = request("GET", manifest["installation"]["download_url"]).content
                assert hashlib.sha256(bundle).hexdigest() == manifest["installation"]["sha256"]
                with ZipFile(io.BytesIO(bundle)) as archive:
                    archive.extractall(root / "installed")
                script = root / "installed" / manifest["installation"]["name"] / "scripts/lockbot_cli.py"
                key = root / "key"
                key.write_text(binding["credential"])
                key.chmod(0o600)
                connection = root / "connection.json"

                def cli(*arguments):
                    before = len(events.read_text().splitlines())
                    command = subprocess.run(
                        [sys.executable, "-I", "-S", str(script), *arguments],
                        env=environment,
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    assert binding["credential"] not in command.stdout + command.stderr
                    assert command.returncode == 0, command.stderr
                    observed = [json.loads(line) for line in events.read_text().splitlines()[before:]]
                    return json.loads(command.stdout)["result"], observed

                cli("init", "--origin", origin, "--key-file", str(key), "--destination", str(connection))
                prefix = ("--connection", str(connection))
                inventory, read_events = cli(*prefix, "resources")
                assert all(not row["holders"] for row in inventory["resources"])
                result, write_events = cli(
                    *prefix, "command", "--operation", "lock", "--node", "n1", "--whole-node", "--duration", "1h"
                )
                assert result["verified"] and result["verification"] == "observed_state_only"
                held, verify_events = cli(*prefix, "resources")
                holder = next(row for row in held["resources"] if row["id"] == "n1")["holders"]
                assert len(holder) == 1 and holder[0]["external_user_id"] == "synthetic-cli-user"
                assert sum(event["method"] == "POST" for event in write_events) == 1
                coherent = "coherent_resource_snapshot" in manifest.get("capabilities", ())
                assert [len(read_events), len(write_events), len(verify_events)] == (
                    [2, 3, 2] if coherent else [3, 5, 3]
                )
                proof = {
                    "synthetic_only": True,
                    "transport": "real loopback TCP",
                    "cli_process": "isolated interpreter -I -S",
                    "package_version": manifest["package_version"],
                    "coherent_snapshot": coherent,
                    "read": read_events,
                    "mutation": write_events,
                    "explicit_later_read": verify_events,
                    "single_mutation": True,
                    "live_holder_verified": True,
                    "counts": {
                        "read": len(read_events),
                        "mutation": len(write_events),
                        "explicit_later_read": len(verify_events),
                    },
                }
                output.write_text(json.dumps(proof, indent=2) + "\n")
                output.chmod(0o600)
                print(json.dumps({"coherent_snapshot": coherent, **proof["counts"], "single_mutation": True}))
        finally:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
        parser.add_argument("--output", type=Path, required=True)
        arguments = parser.parse_args()
        probe(arguments.source.resolve(), arguments.output)
