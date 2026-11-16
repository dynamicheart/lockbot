"""Launch the shipped CLI with a clean interpreter on the runner's actual OS."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill


@pytest.mark.parametrize("scenario", ["legacy", "recover", "create", "reset"])
def test_portable_package_and_private_key_boundary(tmp_path, scenario):
    launcher = ["py", "-3"] if os.name == "nt" else [sys.executable]
    version = subprocess.check_output([*launcher, "--version"], text=True, timeout=10).strip()
    _, _, metadata = skill.skill_artifacts()
    archive = skill.installation_bundle()
    assert hashlib.sha256(archive).hexdigest() == metadata["installation"]["sha256"]
    manifest = {
        **metadata,
        "service_id": "lockbot.resource-api",
        "api_version": "1",
        "instance_id": "runtime-fixture",
        "context_url": "/api/agent/context",
        "enrollment_url": "/api/resource-auth/bindings",
    }
    challenge_path = "/api/resource-auth/credential-challenges"
    if scenario != "legacy":
        manifest["capabilities"] = ["chat_credential_challenges"]
    purpose = "reset" if scenario == "reset" else "recover"
    fixture_key = "lbr_" + "runtimefixture" * 3
    proof = "lbp_" + "runtimeproof" * 3
    enrollment_id = "x" * 22
    binding_confirmed = False
    holder = None
    commands = []
    challenge_requests = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            nonlocal holder
            authorization = self.headers.get("Authorization")
            requests.append((self.path, bool(authorization)))
            if self.path == challenge_path and scenario != "legacy" and not authorization:
                challenge_requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.respond(
                    201,
                    {
                        "challenge_id": enrollment_id,
                        "proof": proof,
                        "bind_code": "RST-ACDEFGHJ" if purpose == "reset" else "REC-ACDEFGHJ",
                        "bind_expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            elif self.path == manifest["enrollment_url"] and scenario == "legacy" and not authorization:
                self.respond(
                    201,
                    {
                        "binding_id": enrollment_id,
                        "credential": fixture_key,
                        "bind_code": "ACDEFGHJ",
                        "bind_expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            elif self.path == "/api/agent/commands" and authorization == "Bearer " + fixture_key:
                command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["command"]
                if command != "lock runtime-node 2m" or holder:
                    self.send_error(409)
                    return
                commands.append(command)
                holder = {
                    "external_user_id": "fixture-user",
                    "remaining_seconds": 120,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
                }
                self.respond(200, {"reply": "Fixture lock recorded"})
            else:
                self.send_error(405)

        def do_GET(self):
            authorization = self.headers.get("Authorization")
            requests.append((self.path, bool(authorization)))
            if self.path == "/api/agent-skill" and not authorization:
                status, body = 200, manifest
            elif self.path == metadata["installation"]["download_url"] and not authorization:
                status, body = 200, archive
            elif self.path == f"{challenge_path}/{enrollment_id}" and authorization == "Bearer " + proof:
                status, body = 200, {"status": "pending"}
                if binding_confirmed:
                    body = {"status": "confirmed", "credential": fixture_key}
                    if scenario == "create":
                        body["outcome"] = "created"
            elif self.path == manifest["enrollment_url"] + "/" + enrollment_id and not authorization:
                status, body = (
                    200,
                    {
                        "binding_id": enrollment_id,
                        "status": "confirmed" if binding_confirmed else "pending",
                    },
                )
            elif self.path == "/api/agent/context" and authorization == "Bearer " + fixture_key:
                status, body = (
                    200,
                    {
                        "server": manifest,
                        "skill": manifest,
                        "bot": {"id": 1, "uuid": "runtime-bot", "name": "运行环境测例", "type": "NODE"},
                        "actor": {"platform": "infoflow", "external_user_id": "fixture-user"},
                        "agent_access_enabled": True,
                        "links": {"resources": "/api/agent/resources", "commands": "/api/agent/commands"},
                    },
                )
            elif self.path == "/api/agent/resources" and authorization == "Bearer " + fixture_key:
                status, body = (
                    200,
                    {
                        "bot_type": "NODE",
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "age_seconds": 0,
                        "supported_operations": ["lock"],
                        "resources": [
                            {
                                "id": "runtime-node",
                                "lock_status": "exclusive" if holder else "idle",
                                "holders": [holder] if holder else [],
                                "booking_list": [],
                            }
                        ],
                    },
                )
            else:
                status, body = 403, {}
            self.respond(status, body)

        def respond(self, status, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
            bootstrap = tmp_path / "bootstrap.py"
            with ZipFile(BytesIO(archive)) as package:
                bootstrap.write_bytes(package.read("lockbot-resource-agent/scripts/lockbot_resource.py"))

            def cli(script, *args, code=0):
                result = subprocess.run(
                    [*launcher, "-I", "-S", str(script), *args],
                    cwd=tmp_path,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                assert fixture_key not in result.stdout + result.stderr
                assert proof not in result.stdout + result.stderr
                assert result.returncode == code, result.stderr
                return json.loads(result.stderr) if code else json.loads(result.stdout)["result"]

            destination = tmp_path / "installed"
            installed = cli(bootstrap, "install", "--origin", origin, "--destination", str(destination))
            assert installed["client_reload_required"] and not installed["connection_and_enrollment_created"]
            assert requests == [("/api/agent-skill", False), (metadata["installation"]["download_url"], False)]

            script = destination / "scripts/lockbot_resource.py"
            start = "enroll" if scenario == "legacy" else "key-challenge"
            args = ("--purpose", "reset") if scenario == "reset" else ()
            pending = cli(script, start, "--origin", origin, "--scope", "project", "--authorized", *args)
            assert pending["status"] == "pending"
            assert pending["target_group"] is None
            directory = pending["enrollment_dir"]
            initial = cli(script, "finish-enrollment", "--enrollment-dir", directory)
            assert initial["status"] == "pending" and not initial["connection_created"]
            if scenario != "legacy":
                assert pending["purpose"] == purpose
                assert pending["bind_command"] == ("bind RST-ACDEFGHJ" if purpose == "reset" else "bind REC-ACDEFGHJ")
                assert not (tmp_path / directory / "key").exists()
            binding_confirmed = True
            finished = cli(script, "finish-enrollment", "--enrollment-dir", directory)
            assert finished["connection_created"]
            assert finished["bot"]["name"] == "运行环境测例"
            assert (finished.get("outcome") == "created") == (scenario == "create")
            assert (finished.get("chat_action") == "reset_applied") == (scenario == "reset")
            assert challenge_requests == ([] if scenario == "legacy" else [{"purpose": purpose}])
            listed = cli(script, "connections", "--directory", ".lockbot/connections")
            assert listed["connections"][0]["connection"] == finished["connection"]
            checked = cli(script, "--connection", finished["connection"], "resources")
            assert checked["resources"][0]["id"] == "runtime-node"
            operation = cli(
                script,
                "--connection",
                finished["connection"],
                "command",
                "--operation",
                "lock",
                "--node",
                "runtime-node",
                "--duration",
                "2m",
            )
            assert operation["verified"] and commands == ["lock runtime-node 2m"]
            assert operation["verified_snapshot"]["resources"][0]["holders"][0]["external_user_id"] == "fixture-user"

            key = tmp_path / "fixture.key"
            key.write_text(fixture_key)
            key.chmod(0o600)
            before = len(requests)
            initialize = ("init", "--origin", origin, "--scope", "project", "--key-file", str(key))
            if os.name == "nt":
                # Introduce an actual grant to Everyone on a synthetic file only.
                subprocess.run(
                    ["icacls", str(key), "/grant", "*S-1-1-0:(R)"], capture_output=True, timeout=10, check=True
                )
                before = len(requests)
                assert cli(script, *initialize, code=2)["error"] == "windows_private_acl_required"
                assert requests[before:] == [("/api/agent-skill", False)]
                print(
                    f"{version} Windows ({scenario}): ZIP installed, binding resumed, lock verified, broad ACL refused"
                )
            else:
                initialized = cli(script, *initialize)
                checked = cli(script, "--connection", initialized["connection"], "resources")
                assert checked["resources"][0]["id"] == "runtime-node"
                assert requests[before:] == [
                    ("/api/agent-skill", False),
                    ("/api/agent/context", True),
                    ("/api/agent-skill", False),
                    ("/api/agent/context", True),
                    ("/api/agent/resources", True),
                ]
                print(f"{version} POSIX ({scenario}): ZIP installed, binding resumed, and lock verified")
        finally:
            server.shutdown()
            thread.join(timeout=2)
