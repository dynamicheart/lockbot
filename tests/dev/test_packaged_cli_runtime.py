"""Launch the shipped CLI with a clean interpreter on the runner's actual OS."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from zipfile import ZipFile

from lockbot.backend.app.resource_api import skill


def test_portable_package_and_private_key_boundary(tmp_path):
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
    }
    fixture_key = "lbr_" + "runtimefixture" * 3
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            authorization = self.headers.get("Authorization")
            requests.append((self.path, bool(authorization)))
            if self.path == "/api/agent-skill" and not authorization:
                status, body = 200, manifest
            elif self.path == metadata["installation"]["download_url"] and not authorization:
                status, body = 200, archive
            elif self.path == "/api/agent/context" and authorization == "Bearer " + fixture_key:
                status, body = (
                    200,
                    {
                        "server": manifest,
                        "skill": manifest,
                        "bot": {"id": 1, "uuid": "runtime-bot", "name": "Runtime fixture", "type": "NODE"},
                        "actor": {"platform": "infoflow", "external_user_id": "fixture-user"},
                        "agent_access_enabled": True,
                        "links": {"resources": "/api/agent/resources"},
                    },
                )
            elif self.path == "/api/agent/resources" and authorization == "Bearer " + fixture_key:
                status, body = (
                    200,
                    {
                        "bot_type": "NODE",
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "age_seconds": 0,
                        "resources": [{"id": "runtime-node", "holders": []}],
                    },
                )
            else:
                status, body = 403, {}
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
                assert result.returncode == code, result.stderr
                return json.loads(result.stderr) if code else json.loads(result.stdout)["result"]

            destination = tmp_path / "installed"
            installed = cli(bootstrap, "install", "--origin", origin, "--destination", str(destination))
            assert installed["client_reload_required"] and not installed["connection_and_enrollment_created"]
            assert requests == [("/api/agent-skill", False), (metadata["installation"]["download_url"], False)]

            script = destination / "scripts/lockbot_resource.py"
            key = tmp_path / "fixture.key"
            key.write_text(fixture_key)
            key.chmod(0o600)
            before = len(requests)
            initialize = ("init", "--origin", origin, "--scope", "project", "--key-file", str(key))
            if os.name == "nt":
                # chmod cannot establish a private Windows DACL. Refuse before transmitting the Key.
                assert cli(script, *initialize, code=2)["error"] == "private_file_permissions_or_size"
                assert requests[before:] == [("/api/agent-skill", False)]
                assert not (tmp_path / ".lockbot/connections").exists()
                print(f"{version} Windows: ZIP installed, Key refused before transmission")
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
                print(f"{version} POSIX: ZIP installed and a new process queried with the saved Key")
        finally:
            server.shutdown()
            thread.join(timeout=2)
