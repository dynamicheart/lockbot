"""Aggregate real packaged CLI reads over isolated HTTP servers; never allocate."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill


@pytest.fixture
def aggregate(tmp_path):
    root = tmp_path / "installed"
    with ZipFile(io.BytesIO(skill.installation_bundle())) as zipped:
        zipped.extractall(root)
    script = root / "lockbot-agent/scripts/lockbot_cli.py"
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    catalog = home / "connections"
    catalog.mkdir(mode=0o700)
    states, servers = [], []

    def server(number):
        state = SimpleNamespace(
            key="lbr_" + str(number) * 32,
            instance=f"instance-{number}",
            actor={"platform": "infoflow", "external_user_id": f"actor-{number}"},
            events=[],
            status=200,
            changed_instance=False,
            extra={},
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                state.events.append((self.path, self.command, self.headers.get("Authorization")))
                self.send_response(405)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                state.events.append((self.path, self.command, self.headers.get("Authorization")))
                manifest = {
                    **skill.skill_artifacts()[2],
                    "service_id": "lockbot.resource-api",
                    "api_version": "1",
                    "instance_id": "changed" if state.changed_instance else state.instance,
                    "context_url": "/api/agent/context",
                    "capabilities": ["coherent_resource_snapshot"],
                }
                status = 200
                if self.path == "/api/agent-skill":
                    assert self.headers.get("Authorization") is None
                    body = manifest
                elif self.path == "/api/agent/resources":
                    assert self.headers.get("Authorization") == "Bearer " + state.key
                    now = datetime.now(timezone.utc)
                    context = {
                        "server": manifest,
                        "bot": {"id": number, "uuid": f"bot-{number}", "name": "Same group name", "type": "QUEUE"},
                        "actor": state.actor,
                        "agent_access_enabled": True,
                        "bot_rules": {},
                        "links": {"resources": "/api/agent/resources", "commands": "/api/agent/commands"},
                        "skill": manifest,
                    }
                    body = {
                        "context": context,
                        "bot_id": number,
                        "bot_type": "QUEUE",
                        "actor": state.actor,
                        "captured_at": now.isoformat(),
                        "age_seconds": 0,
                        "health": "running",
                        "next_event_kind": "own_early_warning",
                        "resources": [
                            {
                                "id": "same-node",
                                "alias": "19",
                                "lock_status": "exclusive",
                                "holders": [
                                    {
                                        "external_user_id": state.actor["external_user_id"],
                                        "remaining_seconds": 7200,
                                        "expires_at": (now + timedelta(hours=2)).isoformat(),
                                    }
                                ],
                                "booking_list": [],
                            }
                        ],
                        **state.extra,
                    }
                    status = state.status
                else:
                    status, body = 404, {}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        state.origin = f"http://127.0.0.1:{listener.server_port}"
        states.append(state)
        servers.append((listener, thread))
        return state

    def record(state, name):
        directory = catalog / name
        directory.mkdir(mode=0o700)
        key = directory / "key"
        key.write_text(state.key)
        key.chmod(0o600)
        path = directory / "connection.json"
        path.write_text(
            json.dumps(
                {
                    "approved_origin": state.origin,
                    "instance_id": state.instance,
                    "bot_uuid": f"bot-{states.index(state) + 1}",
                    "actor": state.actor,
                    "key_file": "key",
                }
            )
        )
        path.chmod(0o600)
        return path

    def run(*args):
        before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
        process = subprocess.run(
            [sys.executable, "-S", str(script), *args],
            env={**os.environ, "LOCKBOT_HOME": str(home)},
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )
        assert before == {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
        assert all(state.key not in process.stdout + process.stderr for state in states)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    yield SimpleNamespace(
        server=server, record=record, run=run, home=home, catalog=catalog, states=states, script=script
    )
    for listener, thread in servers:
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=2)


def test_packaged_aggregate_keeps_service_identity_and_uses_four_gets(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    paths = [aggregate.record(first, "first"), aggregate.record(second, "second")]
    first.extra["notice"] = second.key
    output = aggregate.run("resources-all", "--mine")
    result = output["result"]
    assert result["complete"] is True and not result["unread"]
    assert result["http_requests"] == 4
    assert [group["connection"]["connection"] for group in result["groups"]] == list(map(str, paths))
    assert [group["identity"]["origin"] for group in result["groups"]] == [first.origin, second.origin]
    assert [group["identity"]["actor"] for group in result["groups"]] == [first.actor, second.actor]
    for state, group in zip((first, second), result["groups"], strict=True):
        assert group["snapshot"]["holdings"][0]["remaining_time"] == "2h"
        assert group["snapshot"]["next_event_kind"] == "own_early_warning"
        assert [row[0] for row in state.events] == ["/api/agent-skill", "/api/agent/resources"]
        assert all(row[1] == "GET" for row in state.events)
    assert result["groups"][0]["snapshot"]["notice"] == "[redacted]"


@pytest.mark.parametrize("failure", ["refused", "instance_changed", "stale"])
def test_packaged_aggregate_marks_partial_without_hiding_good_groups(aggregate, failure):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    bad = aggregate.record(second, "second")
    if failure == "refused":
        second.status = 403
    elif failure == "instance_changed":
        second.changed_instance = True
        bad.with_name("key").unlink()  # The anonymous mismatch must precede any key read.
    else:
        second.extra["age_seconds"] = 60
    result = aggregate.run("resources-all")["result"]
    assert result["complete"] is False
    assert result["groups"][0]["identity"]["origin"] == first.origin
    assert result["unread"][0]["connection"]["connection"] == str(bad)
    assert (
        result["unread"][0]["error"]
        == {
            "refused": "http_403",
            "instance_changed": "instance_mismatch",
            "stale": "stale_or_unverifiable_snapshot",
        }[failure]
    )
    if failure == "instance_changed":
        assert second.events == [("/api/agent-skill", "GET", None)]


def test_packaged_aggregate_shares_request_budget_without_a_resource_count_cap(aggregate):
    server = aggregate.server(1)
    for number in range(8):
        aggregate.record(server, str(number))
    result = aggregate.run("resources-all")["result"]
    assert result["complete"] is False and result["catalog_connections"] == 8
    assert result["http_requests"] == len(server.events) == 12
    assert len(result["groups"]) == 6 and len(result["unread"]) == 2
    assert all(row["error"] == "request_budget_exhausted" for row in result["unread"])
    assert sum(path == "/api/agent-skill" for path, *_ in server.events) == 6


def test_packaged_aggregate_empty_catalog_is_unknown_not_empty_resources(aggregate):
    result = aggregate.run("resources-all", "--mine")["result"]
    assert result["scope"] == "saved_connections_only"
    assert result["complete"] is False and result["groups"] == []
    assert result["unread"] == [{"error": "no_saved_connections"}]


def test_packaged_aggregate_explicit_directory_limits_the_private_catalog(aggregate):
    first, second = aggregate.server(1), aggregate.server(2)
    selected = aggregate.record(first, "first")
    aggregate.record(second, "second")
    directory = aggregate.home / "selected"
    directory.mkdir(mode=0o700)
    shutil.copytree(selected.parent, directory / "first")
    result = aggregate.run("resources-all", "--directory", str(directory))["result"]
    assert result["complete"] and result["catalog_connections"] == 1 and result["http_requests"] == 2
    assert result["groups"][0]["identity"]["origin"] == first.origin
    assert second.events == []


def test_packaged_aggregate_unreadable_record_is_reported_without_its_content(aggregate):
    server = aggregate.server(1)
    aggregate.record(server, "good")
    broken = aggregate.catalog / "broken"
    broken.mkdir(mode=0o700)
    record = broken / "connection.json"
    record.write_text(server.key)
    record.chmod(0o600)
    result = aggregate.run("resources-all")["result"]
    assert result["complete"] is False and len(result["groups"]) == 1
    assert result["unread"] == [{"error": "catalog_records_unavailable", "count": 1}]


def test_packaged_aggregate_deadline_is_shared_and_stops_before_credentials(aggregate):
    server = aggregate.server(1)
    aggregate.record(server, "first")
    aggregate.record(server, "second")
    spec = importlib.util.spec_from_file_location("aggregate_deadline_helper", aggregate.script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # One real anonymous response crosses the total deadline; no credential may
    # be sent afterward, including by a new per-connection Client.
    def clock():
        return 61 if server.events else 0
    result = module.resources_all(aggregate.catalog, root=aggregate.script.parents[1], clock=clock)["result"]
    assert result["complete"] is False and result["http_requests"] == 1
    assert server.events == [("/api/agent-skill", "GET", None)]
    assert all(row["error"] == "request_budget_exhausted" for row in result["unread"])
