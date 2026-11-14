"""Exercise the distributed client through actual bounded fake HTTP transports."""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import threading
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from lockbot.backend.app.resource_api import skill

SCRIPT = Path(__file__).parents[2] / "python/lockbot/backend/app/agent_skill/scripts/lockbot_resource.py"
spec = importlib.util.spec_from_file_location("distributed_resource_helper", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
KEY = "lbr_" + "x" * 43


def write_private(path, content):
    path.write_text(json.dumps(content) if isinstance(content, dict) else content)
    path.chmod(0o600)
    return path


@pytest.fixture
def case(tmp_path):
    markdown, archive, meta = skill.skill_artifacts()
    root = tmp_path / meta["name"]
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(tmp_path)
    state = SimpleNamespace(
        requests=[],
        manifest={
            **meta,
            "service_id": helper.SERVICE,
            "api_version": "1",
            "instance_id": "instance-a",
            "context_url": "/api/agent/context",
        },
        bot_uuid="bot-a",
        actor={"platform": "infoflow", "external_user_id": "alice"},
        holding=None,
        booking=[],
        context_count=0,
        grant_fail=False,
        command_status=200,
        command_applies=True,
        resources_fail=False,
        retry=False,
        forbidden=False,
        version_header=None,
        profile="lockbot-v1",
        redirect=False,
    )
    captured = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append((self.path, self.command, dict(self.headers), raw))
            body, status, extra = {}, 200, {}
            if self.path == "/api/agent-skill":
                body = state.manifest
            elif self.path == state.manifest["installation"]["download_url"]:
                content = skill.installation_bundle("agent")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(content)
                return
            elif self.path == "/api/agent/context":
                state.context_count += 1
                body = {
                    "server": {"service_id": helper.SERVICE, "instance_id": "instance-a", "api_version": "1"},
                    "bot": {"id": 7, "uuid": state.bot_uuid, "type": "NODE"},
                    "actor": state.actor,
                    "agent_access_enabled": True,
                    "bot_rules": {"forbid_relock": state.forbidden},
                    "usage_privacy": {"public_task_categories": {"unit_testing": "Unit testing"}},
                    "links": {"resources": "/api/bots/7/resources", "commands": "/api/bots/7/commands"},
                    "skill": {"version": meta["version"]},
                    "utilization": {
                        "configuration": {
                            "type": "prometheus",
                            "profile": state.profile,
                            "accessMode": "short_lived",
                            "endpoint": base + "/metrics",
                            "nodeMapping": {"node-a": "host-a"},
                        },
                        "access": {"scope": "metrics:read", "token": "metrics-grant-" + str(state.context_count)},
                    },
                }
            elif self.path == "/api/bots/7/resources":
                if state.resources_fail and any(path.endswith("commands") for path, *_ in state.requests):
                    status = 503
                body = {
                    "captured_at": captured.isoformat(),
                    "age_seconds": 0,
                    "supported_operations": ["lock", "slock", "book", "unlock"],
                    "resources": [
                        {
                            "id": "node-a",
                            "lock_status": "exclusive" if state.holding else "idle",
                            "holders": [state.holding] if state.holding else [],
                            "booking_list": state.booking,
                        }
                    ],
                }
                if state.retry:
                    state.retry = False
                    status, extra = 429, {"Retry-After": "2"}
                if state.redirect:
                    status, extra = 302, {"Location": base + "/credential-sink"}
            elif self.path == "/api/bots/7/commands":
                command = json.loads(raw)["command"]
                if state.command_applies:
                    if command.startswith("unlock"):
                        state.holding, state.booking = None, []
                    elif command.startswith("book"):
                        state.booking = [{"external_user_id": "alice"}]
                    else:
                        expiry = captured + timedelta(seconds=180 if state.holding else 120)
                        state.holding = {
                            "external_user_id": "alice",
                            "remaining_seconds": 120,
                            "expires_at": expiry.isoformat(),
                        }
                status, body = state.command_status, {"reply": "ok"}
            elif self.path == "/metrics/api/v1/query":
                from lockbot.metrics.gateway import validate_query

                query = urllib.parse.parse_qs(raw.decode())["query"][0]
                validate_query(query, {"host-a"})
                if state.grant_fail and self.headers.get("Authorization") == "Bearer metrics-grant-1":
                    status = 401
                body = {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {
                                    "__name__": "lockbot_accelerator_utilization_percent",
                                    "node": "host-a",
                                    "device": "0",
                                },
                                "value": [1900000000, "42"],
                            }
                        ],
                    },
                }
            else:
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if state.version_header:
                self.send_header("X-Lockbot-Skill-Version", state.version_header)
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = {
        "approved_origin": base,
        "instance_id": "instance-a",
        "bot_uuid": "bot-a",
        "actor": {"platform": "infoflow", "external_user_id": "alice"},
        "key_file": "resource.key",
        "approved_metrics_origin": base,
    }
    path = write_private(tmp_path / "connection.json", connection)
    write_private(tmp_path / "resource.key", KEY)
    delays = []
    client = helper.Client(path, root=root, sleep=delays.append)
    yield SimpleNamespace(
        client=client, state=state, root=root, path=path, base=base, connection=connection, delays=delays, tmp=tmp_path
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def commands(case):
    return [row for row in case.state.requests if row[0].endswith("commands")]


def test_service_discovery_precedes_key_and_redacts_context(case):
    result = case.client.context()
    assert case.state.requests[0][0] == "/api/agent-skill"
    assert "Authorization" not in case.state.requests[0][2]
    assert case.state.requests[1][2]["Authorization"] == "Bearer " + KEY
    assert case.state.requests[1][2]["X-Lockbot-Client-Skill-Version"] == skill.skill_artifacts()[2]["version"]
    output = json.dumps(case.client.redacted({"context": result, "unexpected": KEY}))
    assert KEY not in output and "metrics-grant-1" not in output
    assert helper.installed_version(case.root) == skill.skill_artifacts()[2]["version"]


@pytest.mark.parametrize(
    "field,value", [("instance_id", "wrong-instance"), ("service_id", "other-service"), ("api_version", "2")]
)
def test_manifest_identity_mismatch_sends_no_key(case, field, value):
    case.state.manifest[field] = value
    with pytest.raises(helper.ClientError):
        case.client.context()
    assert len(case.state.requests) == 1 and "Authorization" not in case.state.requests[0][2]


def test_context_bot_mismatch_refuses_mutation(case):
    case.state.bot_uuid = "different-bot"
    with pytest.raises(helper.ClientError, match="context_identity_mismatch"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)


def test_version_update_blocks_writes_but_keeps_reads(case):
    case.state.manifest["version"] = "a" * 16
    case.client.resources(case.client.context())
    assert case.client.needs_update
    with pytest.raises(helper.ClientError, match="update_bundle_before_mutation"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)


def test_modified_or_markdown_only_installation_is_not_accepted(case):
    (case.root / helper.SCRIPT_PATH).write_text("# modified\n")
    with pytest.raises(helper.ClientError, match="installed_bundle_incomplete_or_modified"):
        case.client.context()
    assert all("Authorization" not in row[2] for row in case.state.requests)


def test_acquire_renew_release_are_explicit_and_verified(case):
    metadata = {"task_category": "unit_testing", "agent_label": "test-agent", "purpose": "Unit tests"}
    assert case.client.command("lock", ["node-a"], [], "2m", metadata)["verified"]
    with pytest.raises(helper.ClientError, match="already_owned"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert helper.Client(case.path, root=case.root).command("renew", ["node-a"], [], "3m")["verified"]
    assert helper.Client(case.path, root=case.root).command("release", ["node-a"], [], None)["verified"]
    assert len(commands(case)) == 3
    assert all(
        case.state.requests[index + 1][0].endswith("resources")
        for index, row in enumerate(case.state.requests)
        if row[0].endswith("commands")
    )


@pytest.mark.parametrize(
    "metadata",
    [{"task_category": "invented"}, {"task_category": ["unit_testing"]}, {"external_user_id": "bob"}, {"purpose": KEY}],
)
def test_metadata_categories_and_identity_are_validated_before_write(case, metadata):
    with pytest.raises(helper.ClientError):
        case.client.command("lock", ["node-a"], [], "2m", metadata)
    assert not commands(case)


@pytest.mark.parametrize("duration", [None, "0m", "forever", "2"])
def test_acquisition_requires_explicit_positive_duration(case, duration):
    with pytest.raises(helper.ClientError, match="duration"):
        case.client.command("lock", ["node-a"], [], duration)
    assert not commands(case)


def test_uncertain_write_and_unconfirmed_readback_are_never_replayed(case):
    case.state.command_status = 503
    with pytest.raises(helper.ClientError, match="mutation_outcome_unknown"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert len(commands(case)) == 1
    case.state.command_status = 200
    case.state.resources_fail = True
    case.state.requests.clear()
    with pytest.raises(helper.ClientError, match="mutation_not_confirmed"):
        helper.Client(case.path, root=case.root).command("release", ["node-a"], [], None)
    assert len(commands(case)) == 1


def test_http_200_reply_without_state_effect_is_not_success(case):
    case.state.command_applies = False
    with pytest.raises(helper.ClientError, match="mutation_not_confirmed"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert len(commands(case)) == 1


def test_bounded_read_retry_and_redirect_protection(case):
    context = case.client.context()
    case.state.retry = True
    assert case.client.resources(context)["resources"]
    assert case.delays == [2]
    case.state.redirect = True
    with pytest.raises(helper.ClientError, match="redirect_refused"):
        case.client.resources(context)
    assert not any(row[0] == "/credential-sink" for row in case.state.requests)


def test_metrics_refreshes_grant_once_and_never_sends_resource_key(case):
    case.state.grant_fail = True
    result = case.client.metrics(["node-a"])
    assert result["best_effort"] and result["verification"] == "query_response_only"
    metrics = [row for row in case.state.requests if row[0].startswith("/metrics/")]
    assert [row[2]["Authorization"] for row in metrics] == ["Bearer metrics-grant-1", "Bearer metrics-grant-2"]
    assert all(KEY not in str(row) for row in metrics)
    assert case.state.context_count == 2
    query = urllib.parse.parse_qs(metrics[0][3].decode())["query"][0]
    assert "host" in query and "node=~" in query and "process_info" not in query


@pytest.mark.parametrize("change", ["unapproved", "unsupported", "unmapped"])
def test_metrics_scope_and_profile_fail_without_source_query(case, change):
    nodes = ["node-a"]
    if change == "unapproved":
        case.client.config["approved_metrics_origin"] = "https://different.example.test"
    elif change == "unsupported":
        case.state.profile = "custom"
    else:
        nodes = ["unknown"]
    with pytest.raises(helper.ClientError):
        case.client.metrics(nodes)
    assert not any(row[0].startswith("/metrics/") for row in case.state.requests)


def test_complete_bundle_bootstrap_and_existing_directory_protection(case):
    # This command uses no connection/key and pins no identity on the user's behalf.
    client = helper.Client(approved_origin=case.base, root=case.root)
    target = case.tmp / "new-installation"
    result = client.install(target)
    assert result["client_reload_required"] and not result["connection_and_enrollment_created"]
    assert helper.installed_version(target) == case.state.manifest["version"]
    assert all("Authorization" not in row[2] for row in case.state.requests)
    with pytest.raises(helper.ClientError, match="destination_must_be_new"):
        client.install(target)


def test_request_and_private_file_budgets(case):
    case.client.calls = helper.MAX_CALLS
    with pytest.raises(helper.ClientError, match="budget"):
        case.client.discover()
    assert not case.state.requests
    case.path.chmod(0o644)
    with pytest.raises(helper.ClientError, match="permissions"):
        helper.Client(case.path)


def test_cli_argument_error_does_not_echo_pasted_key(capsys):
    with pytest.raises(SystemExit):
        helper.main(["--raw-key", KEY, "resources"])
    captured = capsys.readouterr()
    assert KEY not in captured.err + captured.out
    assert "invalid_cli_arguments" in captured.err


def test_bundle_hashes_and_helper_only_change_rotate_revision(client, monkeypatch):
    skill._skill_artifacts.cache_clear()
    before = client.get("/api/agent-skill").json()
    original = skill.files

    class Resource:
        def joinpath(self, relative):
            path = original("lockbot.backend.app").joinpath(relative)
            if relative.endswith("scripts/lockbot_resource.py"):
                return SimpleNamespace(read_bytes=lambda: path.read_bytes() + b"\n# revision test\n")
            return path

    monkeypatch.setattr(skill, "files", lambda _: Resource())
    skill._skill_artifacts.cache_clear()
    after = client.get("/api/agent-skill").json()
    assert before["skill_sha256"] == after["skill_sha256"]
    assert before["version"] != after["version"] and before["sha256"] != after["sha256"]
    text = client.get(after["skill_url"])
    assert text.headers["x-lockbot-skill-version"] == after["version"]
    for descriptor in (after, after["installation"]):
        archive = client.get(descriptor["download_url"]).content
        assert hashlib.sha256(archive).hexdigest() == descriptor["sha256"]
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            root = descriptor["name"]
            stamp = json.loads(zipped.read(root + "/installation.json"))
            assert stamp["version"] == after["version"]
            for path, digest in stamp["files"].items():
                assert hashlib.sha256(zipped.read(root + "/" + path)).hexdigest() == digest
    skill._skill_artifacts.cache_clear()


def test_initialize_discovers_bot_scope_without_bot_id_or_uuid(case):
    before = (case.tmp / "resource.key").read_bytes()
    destination = case.tmp / "new-connection.json"
    bootstrap = helper.Client(approved_origin=case.base, root=case.root)
    result = bootstrap.initialize(case.tmp / "resource.key", destination)
    saved = json.loads(destination.read_text())
    assert saved["bot_uuid"] == "bot-a" and saved["instance_id"] == "instance-a"
    assert "credential" not in saved and KEY not in destination.read_text()
    assert result["key_unchanged"] and before == (case.tmp / "resource.key").read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert case.state.requests[0][0] == "/api/agent-skill" and "Authorization" not in case.state.requests[0][2]
    assert helper.Client(destination, root=case.root).context()["bot"]["uuid"] == "bot-a"


def test_write_transport_failure_is_not_retried(case):
    actual = case.client.opener
    writes = []

    class FailedWrite:
        def open(self, request, timeout):
            if request.full_url.endswith("/commands"):
                writes.append(request)
                raise urllib.error.URLError("connection lost")
            return actual.open(request, timeout=timeout)

    case.client.opener = FailedWrite()
    with pytest.raises(helper.ClientError, match="mutation_outcome_unknown"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert len(writes) == 1


def test_bootstrap_refuses_digest_mismatch_without_creating_directory(case):
    case.state.manifest["installation"]["sha256"] = "0" * 64
    target = case.tmp / "bad-install"
    with pytest.raises(helper.ClientError, match="bundle_digest_mismatch"):
        helper.Client(approved_origin=case.base).install(target)
    assert not target.exists()


def test_response_body_limit_is_enforced(case):
    case.state.manifest["padding"] = "x" * (helper.MAX_BYTES + 1)
    with pytest.raises(helper.ClientError, match="response_too_large"):
        case.client.discover()


def test_missing_retry_after_does_not_guess_backoff():
    assert helper.Client._retry_after("") > 30


def test_book_refuses_preexisting_owned_booking_without_post(case):
    case.state.booking = [{"external_user_id": "alice"}]
    with pytest.raises(helper.ClientError, match="already_owned_or_booked"):
        case.client.command("book", ["node-a"], [], "2m")
    assert not commands(case)


def test_category_error_lists_only_public_choices(case):
    with pytest.raises(helper.ClientError) as error:
        case.client.command(
            "lock",
            ["node-a"],
            [],
            "2m",
            {"task_category": "invalid", "purpose": "private-detail", "agent_label": "private-agent"},
        )
    assert error.value.allowed_categories == {"unit_testing": "Unit testing"}
    assert not commands(case)


def test_cli_category_error_exposes_actionable_choices_not_notes(case, monkeypatch, capsys):
    original = helper.Client
    monkeypatch.setattr(helper, "Client", lambda *args, **kwargs: original(*args, root=case.root, **kwargs))
    metadata = write_private(
        case.tmp / "task.json",
        {"task_category": "bad", "purpose": "sensitive-detail", "agent_label": "sensitive-agent"},
    )
    assert (
        helper.main(
            [
                "--connection",
                str(case.path),
                "command",
                "--operation",
                "lock",
                "--node",
                "node-a",
                "--duration",
                "2m",
                "--metadata-file",
                str(metadata),
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    body = json.loads(output.err)
    assert body["allowed_categories"] == {"unit_testing": "Unit testing"}
    assert not any(value in output.err + output.out for value in (KEY, "sensitive-detail", "sensitive-agent"))


def test_command_verification_explicitly_does_not_attribute_business_success(case):
    result = case.client.command("book", ["node-a"], [], "2m")
    assert result["verification"] == "observed_state_only"
    assert result["command_outcome"] == "not_structured_by_server"


def test_incompatible_python_refuses_before_client_or_network(monkeypatch, capsys):
    monkeypatch.setattr(helper, "runtime_compatible", lambda: False)
    monkeypatch.setattr(helper, "Client", lambda *_args, **_kwargs: pytest.fail("must not construct a client"))
    assert helper.main(["resources"]) == 2
    assert "helper_requires_python_3_10_or_newer" in capsys.readouterr().err


def test_packaged_cli_runs_without_site_packages_and_redacts_credentials(case):
    result = subprocess.run(
        [sys.executable, "-S", str(case.root / helper.SCRIPT_PATH), "--connection", str(case.path), "context"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["bot"]["uuid"] == "bot-a"
    assert KEY not in result.stdout + result.stderr and "metrics-grant-1" not in result.stdout


@pytest.mark.parametrize("field,value", [("external_user_id", "bob"), ("platform", "feishu")])
def test_same_bot_different_actor_cannot_reuse_connection(case, field, value):
    case.state.actor[field] = value
    with pytest.raises(helper.ClientError, match="context_actor_mismatch"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)
    assert case.path.read_text() == json.dumps(case.connection)


def test_missing_actor_pin_requires_explicit_connection_init_not_key_rotation(case):
    case.client.config.pop("actor")
    with pytest.raises(helper.ClientError, match="context_actor_mismatch"):
        case.client.context()
    assert not commands(case)
    assert (case.tmp / "resource.key").read_text() == KEY


def test_response_completion_after_deadline_is_not_accepted():
    now = [0.0]

    class SlowEOF(io.BytesIO):
        headers = {}

        def read1(self, size=-1):
            now[0] = 61.0
            return super().read1(size)

    class Transport:
        def open(self, request, timeout):
            return SlowEOF(b"{}")

    client = helper.Client(approved_origin="https://service.example.invalid", opener=Transport(), clock=lambda: now[0])
    with pytest.raises(helper.ClientError, match="request_budget_exhausted"):
        client.discover()


def test_additive_actor_metadata_does_not_invalidate_scoped_connection(case):
    case.state.actor["display_name"] = "Alice updated"
    assert case.client.context()["actor"]["external_user_id"] == "alice"
    assert case.client.command("lock", ["node-a"], [], "2m")["verified"]
