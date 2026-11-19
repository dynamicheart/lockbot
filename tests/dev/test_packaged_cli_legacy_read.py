"""Exercise the explicit legacy profile using the shipped package and localhost only.

The anonymous manifest shape is derived from a captured native API-1 descriptor.
All identities, credentials, revisions and resource records below are synthetic.
"""

import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "package"
    with ZipFile(BytesIO(skill.installation_bundle())) as bundle:
        bundle.extractall(root)
    root /= "lockbot-agent"
    script = root / "scripts/lockbot_cli.py"
    spec = importlib.util.spec_from_file_location("legacy_cli_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = {
        "service_id": "lockbot.resource-api",
        "instance_id": "synthetic-native-instance",
        "api_version": "1",
        "skill_id": "lockbot-resource-agent",
        "credential_scheme": "lbr_bot_scoped",
        "version": "1234567890abcdef",
        "capabilities": [
            "credential_context",
            "own_binding_directory",
            "own_binding_recovery",
            "namespaced_skill",
            "skill_version_headers",
            "resource_view_link",
        ],
        "context_url": "/api/agent/context",
    }
    fixture = SimpleNamespace(
        manifest=manifest,
        events=[],
        faults={},
        scopes={},
        root=root,
        script=script,
        module=module,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            fixture.events.append({"path": self.path, "method": "GET", "headers": dict(self.headers)})
            if self.path == "/api/agent-skill" and not token:
                body = deepcopy(manifest)
            elif token in fixture.scopes and self.path == manifest.get("context_url"):
                body = deepcopy(fixture.scopes[token]["context"])
            elif token in fixture.scopes and self.path == fixture.scopes[token]["context"]["links"]["resources"]:
                body = deepcopy(fixture.scopes[token]["resources"])
            else:
                self.send_error(403)
                return
            status, headers = 200, {}
            fault = fixture.faults.get(self.path)
            if fault:
                status, headers, body = fault(body)
            data = json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            fixture.events.append({"path": self.path, "method": "POST"})
            self.send_error(405)

    def connection(number=1, *, platform="infoflow", subject="same-subject"):
        directory = tmp_path / "connections" / str(number)
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        key = "lbr_" + str(number) * 24
        (directory / "key").write_text(key)
        (directory / "key").chmod(0o600)
        actor = {"platform": platform, "external_user_id": subject}
        bot = {"id": number, "uuid": f"synthetic-bot-{number}", "name": "same-bot-name", "type": "DEVICE"}
        context = {
            "server": {k: manifest[k] for k in ("service_id", "instance_id", "api_version", "credential_scheme")},
            "skill": {"version": manifest["version"]},
            "bot": bot,
            "actor": actor,
            "agent_access_enabled": True,
            "links": {"resources": f"/api/bots/{number}/resources", "commands": f"/api/bots/{number}/commands"},
        }
        snapshot = {
            "health": "running",
            "bot_type": "DEVICE",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "age_seconds": 0,
            "resources": [
                {"id": "same-node", "devices": [{"id": 0, "lock_status": "idle", "holders": [], "booking_list": []}]},
            ],
            "supported_operations": ["lock", "alias", "usage"],
        }
        fixture.scopes[key] = {"context": context, "resources": snapshot}
        config = {
            "approved_origin": fixture.origin,
            "instance_id": manifest["instance_id"],
            "bot_uuid": bot["uuid"],
            "actor": actor.copy(),
            "key_file": "key",
            "bot_type": "DEVICE",
        }
        path = directory / "connection.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        return SimpleNamespace(path=path, key=key, context=context, resources=snapshot, config=config)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        fixture.origin = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fixture.connection = connection
        fixture.scope = connection()

        def run(*args, code=0, opt_in=True, scoped=True):
            argv = ["--legacy-read"] if opt_in else []
            if scoped:
                argv += ["--connection", str(fixture.scope.path)]
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(script), *argv, *args],
                cwd=tmp_path,
                env={**os.environ, "LOCKBOT_HOME": str(tmp_path / "home"), "NO_PROXY": "127.0.0.1"},
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == code, result.stderr
            for key in fixture.scopes:
                assert key not in result.stdout + result.stderr
            assert not [event for event in fixture.events if event["method"] != "GET"]
            return json.loads(result.stderr if code else result.stdout)

        fixture.run = run
        yield fixture
        server.shutdown()
        thread.join(timeout=5)


def test_packaged_legacy_separate_read_is_opt_in_and_revision_independent(legacy):
    assert legacy.run("resources", opt_in=False, code=2)["error"] == "unsupported_service"
    legacy.events.clear()
    output = legacy.run("resources")
    snapshot = output["result"]
    assert [event["path"] for event in legacy.events] == [
        "/api/agent-skill",
        "/api/agent/context",
        "/api/bots/1/resources",
    ]
    assert "Authorization" not in legacy.events[0]["headers"]
    assert all("X-Lockbot-Client-Skill-Version" not in event["headers"] for event in legacy.events)
    assert all("X-Lockbot-Client-Package-Version" not in event["headers"] for event in legacy.events)
    assert all(event["headers"]["X-Lockbot-Client-Api-Version"] == "1" for event in legacy.events)
    assert output["update_required"] is True and snapshot["instruction_state"] == "unknown"
    assert snapshot["instruction_refresh_required"] is True and snapshot["snapshot_fresh_for_write"] is False
    assert snapshot["api_profile"] == "native-legacy-read-v1"
    assert snapshot["snapshot_fresh_for_read"] is True
    assert snapshot["inventory_overview"]["nodes"][0]["groups"][0]["idle"] == [0]
    assert snapshot["resources"][0]["devices"][0]["id"] == 0
    assert "skill" not in snapshot["context"] and "links" not in snapshot["context"]
    assert snapshot["context_source"] == "separate_verified_read"


def test_legacy_own_summary_reports_read_freshness_without_write_authority(legacy):
    card = legacy.scope.resources["resources"][0]["devices"][0]
    card.update(
        lock_status="exclusive",
        dev_model="P800",
        holders=[
            {
                "external_user_id": legacy.scope.context["actor"]["external_user_id"],
                "remaining_seconds": 7200,
            }
        ],
    )
    output = legacy.run("resources", "--mine", "--summary")
    result = output["result"]
    assert result["scope_complete"] and result["view"] == "own"
    assert result["rows"][0]["snapshot_fresh"] is True
    assert result["rows"][0]["holdings"][0]["device"] == 0
    assert result["api_profiles"] == ["native-legacy-read-v1"]
    assert result["instruction_refresh_required"] and result["update_required"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("api_version", "9", "api_version_changed"),
        ("skill_id", "unknown", "unsupported_service"),
        ("credential_scheme", None, "unsupported_legacy_read_profile"),
        ("context_url", "/unexpected", "unsupported_legacy_read_profile"),
        ("capabilities", ["credential_context"], "unsupported_legacy_read_profile"),
        ("instance_id", "replacement", "instance_mismatch"),
    ],
)
def test_unknown_profile_fails_before_key_is_opened(legacy, field, value, error):
    legacy.manifest[field] = value
    legacy.scope.path.with_name("key").unlink()
    assert legacy.run("resources", code=2)["error"] == error
    assert len(legacy.events) == 1 and "Authorization" not in legacy.events[0]["headers"]


@pytest.mark.parametrize("route", ["/api/agent-skill", "/api/agent/context"])
def test_redirect_is_not_followed_or_rebound(legacy, route):
    legacy.faults[route] = lambda body: (302, {"Location": legacy.origin + "/elsewhere"}, body)
    assert legacy.run("resources", code=2)["error"] == "redirect_refused"
    assert not any(event["path"] == "/elsewhere" for event in legacy.events)
    assert len(legacy.events) == (1 if route.endswith("skill") else 2)


@pytest.mark.parametrize("contradiction", ["uuid", "platform", "subject", "type", "resources-url", "resource-actor"])
def test_scope_contradictions_never_authorize_another_scope(legacy, contradiction):
    scope = legacy.scope
    if contradiction == "uuid":
        scope.context["bot"]["uuid"] = "other-bot"
    elif contradiction in {"platform", "subject"}:
        scope.context["actor"]["platform" if contradiction == "platform" else "external_user_id"] = "other"
    elif contradiction == "type":
        scope.context["bot"]["type"] = "NODE"
    elif contradiction == "resources-url":
        scope.context["links"]["resources"] = "/api/bots/99/resources"
    else:
        scope.resources["actor"] = {"platform": "feishu", "external_user_id": "same-subject"}
    assert legacy.run("resources", code=2)["error"] in {
        "context_identity_mismatch",
        "context_actor_mismatch_reinitialize_connection_without_rotating_key",
        "invalid_legacy_context",
        "unsupported_legacy_resource_endpoint",
        "resource_snapshot_identity_mismatch",
    }


@pytest.mark.parametrize("device", [None, True, -1, {}, ""])
def test_malformed_card_identity_is_rejected(legacy, device):
    legacy.scope.resources["resources"][0]["devices"][0]["id"] = device
    assert legacy.run("resources", code=2)["error"] == "invalid_legacy_resource_snapshot"


def test_missing_optional_observations_stay_unknown(legacy):
    snapshot = legacy.scope.resources
    snapshot.pop("age_seconds")
    snapshot["captured_at"] = "bad timestamp"
    card = snapshot["resources"][0]["devices"][0]
    card.pop("booking_list")
    card.pop("holders")
    output = legacy.run("resources")["result"]
    assert output["age_seconds"] is None and output["captured_at"] is None
    assert "booking_list" not in output["resources"][0]["devices"][0]
    assert output["inventory_overview"]["nodes"][0]["groups"][0]["unknown"] == [0]
    assert output["inventory_overview"]["nodes"][0]["groups"][0]["model"] is None


@pytest.mark.parametrize("status", [200, 401, 429, 503])
@pytest.mark.parametrize("signal", ["header-api", "header-service", "body-api", "body-update"])
def test_response_metadata_is_checked_on_success_and_errors(legacy, status, signal):
    def fault(body):
        headers = {}
        if signal == "header-api":
            headers["X-Lockbot-API-Compatibility"] = "incompatible"
        elif signal == "header-service":
            headers["X-Lockbot-Service-ID"] = "external.report"
        elif signal == "body-api":
            body["api_version"] = "2"
        else:
            body["skill_update"] = {"status": "update_available", "current_version": "f" * 16}
        return status, headers, body

    legacy.faults["/api/bots/1/resources"] = fault
    success = status == 200 and signal == "body-update"
    output = legacy.run("resources", code=0 if success else 2)
    if signal == "body-update":
        state = output["result"] if success else output
        assert state["instruction_state"] == "changed"
        assert state["instruction_refresh_required"] is True
    else:
        assert output["error"] == ("unsupported_service" if signal == "header-service" else "api_version_changed")


@pytest.mark.parametrize(
    "args",
    [
        ("command", "--operation", "release", "--node", "same-node", "--device", "0"),
        ("recover",),
        ("alias", "--node", "same-node", "--set", "label"),
        ("panel-link",),
        ("personal-report", "--extension", "anything"),
        ("metrics", "--node", "same-node"),
    ],
)
def test_all_optional_and_mutating_cli_entry_points_remain_blocked(legacy, args):
    assert legacy.run(*args, code=2)["error"] == "legacy_read_only"
    assert legacy.events == []


def test_direct_transport_cannot_bypass_read_guard_or_load_invalid_package(legacy):
    client = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True)
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(legacy.module.ClientError, match="legacy_read_only"):
            client._request(legacy.origin + "/api/agent/context", token=legacy.scope.key, method=method)
    with pytest.raises(legacy.module.ClientError, match="legacy_read_only"):
        client.recover(None)
    legacy.script.write_text(legacy.script.read_text() + "\n# modified executable\n")
    assert legacy.run("resources", code=2)["error"] == "installed_bundle_incomplete_or_modified"
    assert len(legacy.events) == 1 and "Authorization" not in legacy.events[0]["headers"]


def test_distinct_saved_scopes_do_not_share_authority_or_names(legacy):
    second = legacy.connection(2, platform="feishu")
    second.resources["resources"][0]["devices"][0]["private_label"] = "only-second"
    output = legacy.run("resources-all", "--directory", str(legacy.scope.path.parent.parent), scoped=False)
    groups = output["result"]["groups"]
    assert len(groups) == 2
    assert output["result"]["complete"] is True and output["result"]["unread"] == []
    assert all(group["snapshot"]["snapshot_fresh_for_write"] is False for group in groups)
    assert all(group["snapshot"]["snapshot_fresh_for_read"] is True for group in groups)
    assert {group["identity"]["actor"]["platform"] for group in groups} == {"infoflow", "feishu"}
    assert {group["identity"]["bot"]["uuid"] for group in groups} == {"synthetic-bot-1", "synthetic-bot-2"}
    labels = {
        group["identity"]["bot"]["uuid"]: group["snapshot"]["resources"][0]["devices"][0].get("private_label")
        for group in groups
    }
    assert labels == {"synthetic-bot-1": None, "synthetic-bot-2": "only-second"}
    assert len(legacy.events) == 5
    assert len([event for event in legacy.events if event["path"] == "/api/agent/context"]) == 2


@pytest.mark.parametrize("status,expected", [(429, 2), (503, 1)])
def test_bounded_read_failures_do_not_replay_other_reads(legacy, status, expected):
    legacy.faults["/api/bots/1/resources"] = lambda body: (status, {"Retry-After": "0"}, {})
    assert legacy.run("resources", code=2)["error"] == f"http_{status}"
    assert len([event for event in legacy.events if event["path"] == "/api/bots/1/resources"]) == expected
    assert len([event for event in legacy.events if event["path"] == "/api/agent/context"]) == 1


def test_multi_scope_budget_exhaustion_exposes_unvisited_suffix(legacy):
    for number in range(2, 7):
        last = legacy.connection(number)
    deferred_key = last.path.with_name("key")
    material = deferred_key.read_bytes()
    deferred_key.unlink()
    output = legacy.run("resources-all", "--directory", str(legacy.scope.path.parent.parent), scoped=False)
    assert len(legacy.events) == 11
    assert len(output["result"]["groups"]) == 5
    assert len([event for event in legacy.events if event["path"] == "/api/agent/context"]) == 5
    assert output["result"]["scope_page"]["attempted_connections"] == 5
    assert output["result"]["unread"]
    cursor = output["result"]["scope_page"]["next_after_scope"]
    assert cursor
    assert {row["error"] for row in output["result"]["unread"]} == {"request_budget_exhausted"}
    deferred_key.write_bytes(material)
    deferred_key.chmod(0o600)
    legacy.events.clear()
    continued = legacy.run(
        "resources-all",
        "--directory",
        str(legacy.scope.path.parent.parent),
        "--after-scope",
        cursor,
        scoped=False,
    )["result"]
    assert len(continued["groups"]) == 1 and len(legacy.events) == 3
    assert continued["scope_page"]["prior_connections"] == 5
    assert continued["scope_page"]["exhausted"] is True
    assert continued["unread"] == []


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_access_is_a_pause_and_stopped_health_is_preserved(legacy, enabled):
    legacy.scope.context["agent_access_enabled"] = enabled
    legacy.scope.context["utilization"] = {"authorization": "synthetic-metrics-grant"}
    legacy.scope.resources["health"] = "stopped"
    context = legacy.run("context")
    assert context["result"]["agent_access_enabled"] is enabled
    assert "utilization" not in context["result"]
    legacy.events.clear()
    output = legacy.run("resources", code=0 if enabled else 2)
    if enabled:
        assert output["result"]["health"] == "stopped"
        assert output["result"]["snapshot_fresh_for_write"] is False
    else:
        assert output["error"] == "agent_access_disabled"
        assert len(legacy.events) == 2


@pytest.mark.parametrize("fault", ["calls", "deadline", "timeout"])
def test_per_invocation_call_time_and_transport_bounds(legacy, fault):
    from urllib.error import URLError

    now = [0]
    client = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, clock=lambda: now[0])
    context = client.context()
    if fault == "calls":
        client.calls = 12
    elif fault == "deadline":
        now[0] = 60
    else:

        def timed_out(*_args, **_kwargs):
            raise URLError("synthetic timeout")

        client.opener.open = timed_out
    with pytest.raises(
        legacy.module.ClientError, match="request_failed" if fault == "timeout" else "request_budget_exhausted"
    ):
        client.resources(context)
    assert len(legacy.events) == 2


@pytest.mark.parametrize("field,value", [("actor", None), ("bot_uuid", None), ("bot_uuid", ["invalid"])])
def test_unpinned_connection_fails_before_key_is_opened(legacy, field, value):
    config = legacy.scope.config.copy()
    if value is None:
        config.pop(field)
    else:
        config[field] = value
    legacy.scope.path.write_text(json.dumps(config))
    legacy.scope.path.with_name("key").unlink()
    assert legacy.run("resources", code=2)["error"] == "connection_identity_not_pinned"
    assert len(legacy.events) == 1


def test_server_current_notice_cannot_certify_loaded_legacy_instructions(legacy):
    def notice(body):
        body["skill_update"] = {"status": "current", "current_version": legacy.manifest["version"]}
        return 200, {"X-Lockbot-Skill-Update": "current"}, body

    legacy.faults["/api/bots/1/resources"] = notice
    output = legacy.run("resources")
    assert output["instruction_state"] == "unknown" and output["update_required"] is True
    assert all("X-Lockbot-Client-Skill-Version" not in event["headers"] for event in legacy.events)


@pytest.mark.parametrize("fault", ["age", "missing-capture", "elapsed"])
def test_legacy_read_freshness_ages_without_enabling_writes(legacy, fault):
    now = [0]
    client = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, clock=lambda: now[0])
    if fault == "age":
        legacy.scope.resources["age_seconds"] = 30
    elif fault == "missing-capture":
        legacy.scope.resources.pop("captured_at")
    snapshot = client.inventory()
    if fault == "elapsed":
        assert snapshot["snapshot_fresh_for_read"] is True
        now[0] = 30
    result = legacy.module.observation_at_completion(client, snapshot)
    assert result["snapshot_fresh_for_read"] is False
    assert result["snapshot_fresh_for_write"] is False
    assert result["inventory_overview"]["nodes"][0]["groups"][0]["unknown"] == [0]


def test_bounded_summary_retains_profile_instruction_state_and_read_freshness(legacy):
    output = legacy.run("resources", "--summary")["result"]
    assert output["api_profiles"] == ["native-legacy-read-v1"]
    assert output["instruction_state"] == "unknown" and output["instruction_refresh_required"] is True
    assert output["update_required"] is True
    assert output["rows"][0]["snapshot_fresh"] is True
    assert output["rows"][0]["idle_device_ids"] == [0]


def test_same_service_34_scopes_complete_with_finite_cursor_progress_and_no_disk_cache(legacy):
    for number in range(2, 35):
        legacy.connection(number)
    directory = legacy.scope.path.parent.parent
    before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    cursor, seen, requests, pages = None, [], 0, 0
    while True:
        legacy.events.clear()
        args = ("--after-scope", cursor) if cursor else ()
        result = legacy.run("resources-all", "--directory", str(directory), *args, scoped=False)["result"]
        pages += 1
        assert pages <= 7
        assert result["http_requests"] == len(legacy.events) <= 12
        assert len([event for event in legacy.events if event["path"] == "/api/agent-skill"]) == 1
        assert "Authorization" not in legacy.events[0]["headers"]
        requests += len(legacy.events)
        seen.extend(group["identity"]["bot"]["uuid"] for group in result["groups"])
        assert result["scope_page"]["attempted_connections"] == len(result["groups"])
        next_cursor = result["scope_page"]["next_after_scope"]
        if next_cursor is None:
            assert result["scope_page"]["exhausted"] and not result["unread"]
            break
        assert next_cursor != cursor and not result["scope_page"]["exhausted"]
        cursor = next_cursor
    assert len(seen) == len(set(seen)) == 34
    assert pages == 7 and requests == 75  # Previously 9 subprocesses and 102 GETs.
    assert before == {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("difference", ["origin", "instance"])
def test_descriptor_reuse_requires_approved_origin_and_pinned_instance(legacy, difference):
    second = legacy.connection(2)
    if difference == "origin":
        second.config["approved_origin"] = legacy.origin.replace("127.0.0.1", "localhost")
    else:
        second.config["instance_id"] = "different-instance"
    second.path.write_text(json.dumps(second.config))
    # A missing Key makes an accidental cached-profile protected start observable.
    if difference != "origin":
        second.path.with_name("key").unlink()
    result = legacy.run("resources-all", "--directory", str(second.path.parent.parent), scoped=False)["result"]
    assert len([event for event in legacy.events if event["path"] == "/api/agent-skill"]) == 2
    assert len(legacy.events) == (6 if difference == "origin" else 4)
    assert len(result["groups"]) == (2 if difference == "origin" else 1)
    if difference != "origin":
        assert result["unread"][0]["error"] == "instance_mismatch"


def test_replacement_context_invalidates_reuse_before_later_key(legacy):
    second, third = legacy.connection(2), legacy.connection(3)
    third.path.with_name("key").unlink()

    def replaced(body):
        if body["bot"]["id"] == 2:
            body["server"]["instance_id"] = "replacement-instance"
            legacy.manifest["instance_id"] = "replacement-instance"
        return 200, {}, body

    legacy.faults["/api/agent/context"] = replaced
    result = legacy.run("resources-all", "--directory", str(second.path.parent.parent), scoped=False)["result"]
    assert len(result["groups"]) == 1
    assert [event["path"] for event in legacy.events] == [
        "/api/agent-skill",
        "/api/agent/context",
        "/api/bots/1/resources",
        "/api/agent/context",
        "/api/agent-skill",
    ]
    assert [row["error"] for row in result["unread"]] == ["instance_mismatch", "instance_mismatch"]


@pytest.mark.parametrize("fault", ["status", "profile", "api-header"])
def test_failed_anonymous_discovery_is_not_reused(legacy, fault):
    second = legacy.connection(2)
    attempts = []

    def fail_once(body):
        attempts.append(True)
        if len(attempts) == 1:
            if fault == "status":
                return 503, {}, {}
            if fault == "api-header":
                return 200, {"X-Lockbot-API-Compatibility": "incompatible"}, body
            body["credential_scheme"] = "unknown"
        return 200, {}, body

    legacy.faults["/api/agent-skill"] = fail_once
    result = legacy.run("resources-all", "--directory", str(second.path.parent.parent), scoped=False)["result"]
    assert len(attempts) == 2 and len(legacy.events) == 4
    assert len(result["groups"]) == 1 and len(result["unread"]) == 1
    assert result["groups"][0]["identity"]["bot"]["uuid"] == second.context["bot"]["uuid"]


def test_cached_descriptor_is_copied_revalidated_and_replays_header_notice(legacy):
    second = legacy.connection(2)
    legacy.faults["/api/agent-skill"] = lambda body: (200, {"x-lockbot-skill-update": "update_available"}, body)
    cache = {}
    first = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    first.discover()
    first.manifest["capabilities"].clear()
    first.manifest["context_url"] = "/mutated"
    first._discovery_headers.clear()
    other = legacy.module.Client(second.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    context = other.context()
    assert other.manifest == legacy.manifest
    assert context["instruction_state"] == "changed" and other.needs_update
    assert len([event for event in legacy.events if event["path"] == "/api/agent-skill"]) == 1
    # Even tampered in-memory metadata cannot bypass validation for the next Key.
    descriptor, _ = next(iter(cache.values()))
    descriptor["instance_id"] = "tampered"
    blocked = legacy.module.Client(second.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    with pytest.raises(legacy.module.ClientError, match="instance_mismatch"):
        blocked.context()
    assert blocked.key is None


def test_cached_descriptor_does_not_share_actor_or_bot_authority(legacy):
    second, third = legacy.connection(2, platform="feishu"), legacy.connection(3)
    second.context["actor"]["external_user_id"] = "different-actor"
    third.context["bot"]["uuid"] = "different-bot"
    result = legacy.run("resources-all", "--directory", str(second.path.parent.parent), scoped=False)["result"]
    assert len(result["groups"]) == 1
    assert [row["error"] for row in result["unread"]] == [
        "context_actor_mismatch_reinitialize_connection_without_rotating_key",
        "context_identity_mismatch",
    ]
    assert len([event for event in legacy.events if event["path"] == "/api/agent/context"]) == 3
    assert not any(event["path"] in {"/api/bots/2/resources", "/api/bots/3/resources"} for event in legacy.events)


def test_unknown_instance_cannot_reuse_a_pinned_descriptor(legacy):
    cache = {}
    first = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    first.discover()
    legacy.scope.config.pop("instance_id")
    legacy.scope.path.write_text(json.dumps(legacy.scope.config))
    legacy.scope.path.with_name("key").unlink()
    client = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    with pytest.raises(legacy.module.ClientError, match="connection_identity_not_pinned"):
        client.context()
    assert len(legacy.events) == 2 and client.key is None
    assert len(cache) == 1


@pytest.mark.parametrize("signal", ["header", "json"])
def test_reused_modern_discovery_retains_revision_notices(legacy, signal):
    second = legacy.connection(2)
    legacy.manifest.update(skill_id="lockbot-agent", version=legacy.module.installed_version(legacy.root))
    for scope in (legacy.scope, second):
        scope.context["skill"]["version"] = legacy.manifest["version"]

    def notice(body):
        headers = {}
        if signal == "header":
            headers["x-lockbot-skill-version"] = "f" * 16
        else:
            body["skill_update"] = {"current_version": "f" * 16}
        return 200, headers, body

    legacy.faults["/api/agent-skill"] = notice
    cache = {}
    for scope in (legacy.scope, second):
        client = legacy.module.Client(scope.path, root=legacy.root, _discovery_cache=cache)
        client.context()
        assert client.needs_update
    assert len([event for event in legacy.events if event["path"] == "/api/agent-skill"]) == 1


def test_client_profile_never_reuses_an_opt_in_legacy_descriptor(legacy):
    cache = {}
    first = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    first.discover()
    modern = legacy.module.Client(legacy.scope.path, root=legacy.root, _discovery_cache=cache)
    with pytest.raises(legacy.module.ClientError, match="unsupported_service"):
        modern.context()
    assert len(legacy.events) == 2 and modern.key is None


def test_cached_api_header_is_rechecked_before_key(legacy):
    cache = {}
    first = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    first.discover()
    _, headers = next(iter(cache.values()))
    headers["X-Lockbot-API-Compatibility"] = "incompatible"
    other = legacy.module.Client(legacy.scope.path, root=legacy.root, legacy_read=True, _discovery_cache=cache)
    for _ in range(2):
        with pytest.raises(legacy.module.ClientError, match="api_version_changed"):
            other.context()
    assert len(legacy.events) == 1 and other.key is None and other.manifest is None
