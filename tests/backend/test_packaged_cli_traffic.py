"""Exercise the served CLI against actual resource routes without a network server."""

# ruff: noqa: F811 -- the imported api fixture is injected by pytest parameter name

import contextlib
import hashlib
import importlib.util
import io
import json
from datetime import datetime
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api.traffic import credential_budget

from backend.test_resource_api_resources import ACTOR, api  # noqa: F401

ORIGIN = "http://testserver"


class BackendTransport:
    def __init__(self, client, package_version):
        self.client = client
        self.package_version = package_version
        self.events = []
        self.tools = []

    def open(self, request, timeout=None):
        assert urlsplit(request.full_url).netloc == "testserver"
        response = self.client.request(
            request.get_method(),
            request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
        )
        # Retain route and status only. Credentials and request/response bodies stay private.
        self.events.append(
            {"method": request.get_method(), "path": urlsplit(request.full_url).path, "status": response.status_code}
        )
        headers = Message()
        for key, value in response.headers.items():
            headers[key] = value
        body = io.BytesIO(response.content)
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "backend response", headers, body)
        body.headers = headers
        return body


@pytest.fixture
def packaged_cli(client, api, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_URL", ORIGIN)
    monkeypatch.setattr(credential_budget, "read_limit", 120)
    monkeypatch.setattr(credential_budget, "write_limit", 30)
    credential_budget.reset()
    manifest = client.get("/api/agent-skill").json()
    archive = client.get(manifest["installation"]["download_url"])
    assert archive.status_code == 200
    assert hashlib.sha256(archive.content).hexdigest() == manifest["installation"]["sha256"]
    with ZipFile(io.BytesIO(archive.content)) as package:
        package.extractall(tmp_path / "installed")
    script = tmp_path / "installed" / manifest["installation"]["name"] / "scripts/lockbot_cli.py"
    spec = importlib.util.spec_from_file_location("packaged_cli_traffic", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transport = BackendTransport(client, manifest["package_version"])
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: transport)
    key = tmp_path / "key"
    key.write_text(api["headers"]["Authorization"].removeprefix("Bearer "))
    key.chmod(0o600)
    connection = tmp_path / "connection.json"

    def run(*arguments, expected_code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        start = len(transport.events)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(list(arguments))
        assert code == expected_code, stderr.getvalue()
        output = json.loads(stdout.getvalue() if code == 0 else stderr.getvalue())
        assert key.read_text() not in stdout.getvalue() + stderr.getvalue()
        action = arguments[2] if arguments[0] == "--connection" else arguments[0]
        transport.tools.append(
            {
                "tool": "packaged_cli.main",
                "action": action,
                "argv": list(arguments),
                "exit_code": code,
                "output": output,
                "http": transport.events[start:],
            }
        )
        return output, transport.events[start:]

    run("init", "--origin", ORIGIN, "--key-file", str(key), "--destination", str(connection))
    yield run, connection, transport
    credential_budget.reset()


def test_query_then_immediate_lock_and_verification(packaged_cli, api, tmp_path):
    run, connection, transport = packaged_cli
    arguments = ("--connection", str(connection))
    for _ in range(2):
        output, events = run(*arguments, "resources")
        assert all(event["status"] == 200 for event in events)
        assert next(item for item in output["result"]["resources"] if item["id"] == "n1")["holders"] == []
    output, events = run(
        *arguments, "command", "--operation", "lock", "--node", "n1", "--whole-node", "--duration", "1h"
    )
    assert output["result"]["verified"] is True
    assert all(event["status"] == 200 for event in events)
    assert [event["method"] for event in events].count("POST") == 1
    output, events = run(*arguments, "resources", "--exact-lease-times")
    assert all(event["status"] == 200 for event in events)
    snapshot = output["result"]
    held = next(item for item in snapshot["resources"] if item["id"] == "n1")
    assert held["lock_status"] == "exclusive"
    assert len(held["holders"]) == 1
    holder = held["holders"][0]
    assert holder["external_user_id"] == ACTOR
    assert 3590 <= holder["remaining_seconds"] <= 3600
    assert datetime.fromisoformat(holder["expires_at"]) > datetime.fromisoformat(snapshot["captured_at"])
    actual_holders = api["bot"].state.bot_state["n1"]["current_users"]
    assert len(actual_holders) == 1
    assert actual_holders[0]["user_id"] == ACTOR
    assert actual_holders[0]["duration"] == 3600
    assert sum(event["method"] == "POST" for event in transport.events) == 1
    evidence = {
        "case": "packaged-cli-query-immediate-lock-verification",
        "simulation_only": True,
        "transport": "FastAPI TestClient",
        "package_version": transport.package_version,
        "script": "scripts/lockbot_cli.py",
        "result": "passed",
        "read_budget_per_minute": 120,
        "write_budget_per_minute": 30,
        "verified_single_holder": True,
        "verified_duration_seconds": 3600,
        "tools": transport.tools,
        "http": transport.events,
    }
    evidence_path = tmp_path / "packaged-cli-traffic-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    evidence_path.chmod(0o600)


def test_resource_read_budget_still_returns_429(client, api, packaged_cli, monkeypatch):
    from lockbot.backend.app.rate_limit import limiter

    # Isolate the per-credential budget from the separate per-IP route limiter.
    monkeypatch.setattr(limiter, "enabled", False)
    credential_budget.reset()
    path = f"/api/bots/{api['bot_id']}/resources"
    for _ in range(120):
        response = client.get(path, headers=api["headers"])
        assert response.status_code == 200, response.text
    response = client.get(path, headers=api["headers"])
    assert response.status_code == 429
    assert 0 < int(response.headers["Retry-After"]) <= 61
    # A different credential retains its own allowance.
    assert client.get(path, headers=api["other_headers"]).status_code == 200
