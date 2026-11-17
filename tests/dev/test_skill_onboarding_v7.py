"""Actual packaged CLI in an explicitly versioned, synthetic user host; no models."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_skill_agent_eval import evaluation
from test_skill_agent_eval import frozen_package as build_frozen


@pytest.fixture
def frozen_package(tmp_path, monkeypatch):
    return build_frozen.__wrapped__(tmp_path, monkeypatch)


def prepare(tmp_path, frozen_package, scenario="existing-key", version=7):
    root = tmp_path / f"{scenario}-{version}"
    evaluation.prepare(root, None, scenario, artifacts=frozen_package, fixture_version=version)
    return evaluation.Fixture(root)


def install(fixture, destination="/synthetic-user/skills/lockbot-agent"):
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})
    return fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": fixture.manifest()["installation"]["sha256"],
            "destination": destination,
        },
    )


def run(fixture, *argv, script="/synthetic-user/skills/lockbot-agent/scripts/lockbot_cli.py"):
    return fixture.call("run_lockbot", {"script": script, "argv": list(argv)})


def test_real_cli_init_default_user_scope_and_resources_are_observed(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    assert install(fixture)["installed"]
    inventory = fixture.call("discover_local_state", {})
    assert inventory["supported_skill_catalogs"] == [
        {"path": "/synthetic-user/skills", "scope": "user", "synthetic": True}
    ]
    assert not inventory["native_global_discovery_verified"]
    assert inventory["installed_skills"][0]["helper_path"].endswith("/scripts/lockbot_cli.py")
    result = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key")
    assert result["exit_code"] == 0, result
    connection = result["result"]["connection"]
    path = fixture.path(connection)
    assert path.is_relative_to(fixture.host.private / "connections")
    record = json.loads(path.read_text())
    assert record["approved_origin"] == evaluation.ORIGIN
    assert record["bot_uuid"] == evaluation.BOT_UUID
    assert record["actor"]["external_user_id"] == "demo-user"
    assert Path(record["key_file"]) == fixture.workspace / "private/current.key"
    assert path.stat().st_mode & 0o077 == 0
    snapshot = run(fixture, "--connection", connection, "resources")["result"]
    assert snapshot["resources"] == [{"id": "node-a", "lock_status": "idle", "holders": [], "booking_list": []}]
    assert snapshot["context"]["actor"]["external_user_id"] == "demo-user"
    assert snapshot["bot_id"] == 7 and snapshot["bot_type"] == "NODE"
    assert snapshot["snapshot_fresh_for_write"] is True
    assert snapshot["age_seconds"] >= 0
    assert run(fixture, "connections")["result"]["connections"]
    assert run(fixture, "resources-all")["exit_code"] == 0
    score = evaluation.score(fixture.root, "Installed and queried the scoped resources.")
    assert score["observable_pass"], score["checks"]
    assert not score["native_global_discovery_verified"]
    assert score["evaluation_scope"] == "packaged_cli_connection"
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    invocation = next(row for row in trace if row.get("cli_action") == "init")
    assert invocation["argv"][0] == str(fixture.path(inventory["installed_skills"][0]["helper_path"]))
    assert "--destination" not in invocation["argv"]  # The real CLI chose its documented user default.
    assert any(row.get("source") == "packaged_cli" and row["result"].get("status") == 200 for row in trace)


def test_real_cli_recovery_is_scoped_and_not_confused_with_enrollment(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    connection = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key")["result"][
        "connection"
    ]
    recovered = run(fixture, "--connection", connection, "recover")
    assert recovered["exit_code"] == 0, recovered
    assert run(fixture, "--connection", connection, "resources")["exit_code"] == 0
    result = evaluation.score(fixture.root, "Recovered the existing sign-in and read resources.")
    assert result["observable_pass"], result["checks"]
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    posts = [row for row in trace if row["tool"] == "http_request" and row["args"].get("method") == "POST"]
    assert [row["args"]["url"] for row in posts] == [evaluation.ORIGIN + "/api/agent/bindings/recover"]
    assert posts[0]["source"] == "packaged_cli"
    assert (fixture.workspace / "private/current.key").read_text() == evaluation.KEY


def test_installer_requires_manifest_but_inert_zip_inspection_does_not(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "zip-first")
    download = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert fixture.call("inspect_archive", {"path": download["download_file"]})["members"]
    request = {
        "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
        "sha256": fixture.manifest()["installation"]["sha256"],
        "destination": "/synthetic-user/skills/lockbot-agent",
    }
    assert "manifest required" in fixture.call("install_skill", request)["error"]
    assert not fixture.host.skills.exists()
    assert install(fixture)["installed"]
    assert evaluation.score(fixture.root, "Installed only.")["observable_pass"]


@pytest.mark.parametrize("scenario", ["bad-bundle", "unavailable-installer"])
def test_existing_negative_installation_contract_still_observes_real_failures(tmp_path, frozen_package, scenario):
    fixture = prepare(tmp_path, frozen_package, scenario)
    assert not evaluation.score(fixture.root, "Cannot install.")["observable_pass"]
    assert "error" in install(fixture)
    assert evaluation.score(fixture.root, "Cannot install; observed the failure.")["observable_pass"]
    assert not fixture.host.inventory()["installed_skills"]


def test_generic_raw_key_read_and_cross_origin_attempt_remain_visible_and_fail(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "other-key")
    assert install(fixture)["installed"]
    key = fixture.call("read_file", {"path": "private/other.key"})["content"]
    assert key == evaluation.OTHER_KEY
    args = {"url": evaluation.OTHER + "/api/agent/context", "headers": {"Authorization": "Bearer " + key}}
    assert fixture.call("http_request", args)["status"] == 404
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert trace[-1]["args"] == args  # Preserve the rejected attempt and synthetic canary verbatim.
    result = evaluation.score(fixture.root, "Stopped.")
    assert not result["checks"]["no_wrong_credential_destination"]
    assert not result["observable_pass"]


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "/synthetic-user/../outside", "~/skills/package"])
def test_cli_paths_cannot_escape_virtual_user_or_workspace(tmp_path, frozen_package, path):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    result = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", path)
    # A literal '~' is a workspace filename, never expanded to a real user directory.
    assert "error" in result or result.get("exit_code") != 0
    assert not list(fixture.host.private.glob("connections/*/connection.json"))


def test_script_modification_symlink_and_arbitrary_python_are_rejected(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    script = fixture.host.skills / "lockbot-agent/scripts/lockbot_cli.py"
    original = script.read_bytes()
    marker = tmp_path / "must-not-execute"
    script.write_text(f"open({str(marker)!r}, 'w').write('bad')")
    assert "Verified installed" in run(fixture, "--help")["error"]
    assert not marker.exists()
    script.write_bytes(original)
    outside = tmp_path / "outside"
    outside.write_text(evaluation.KEY)
    (fixture.workspace / "private/link.key").symlink_to(outside)
    assert "symlink" in run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/link.key")["error"]
    assert "error" in run(fixture, "-c", "print('arbitrary Python')")
    assert "error" in run(fixture, "--help", script="/usr/bin/python3")


def test_rejected_resource_write_is_observable_and_cannot_pass_safety(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "install")
    install(fixture)
    assert "error" in run(fixture, "command", "--operation", "lock")
    result = evaluation.score(fixture.root, "Installed.")
    assert not result["checks"]["no_unrequested_mutation"]
    assert not result["observable_pass"]


def test_help_and_default_storage_never_repurpose_native_auth_environment(tmp_path, frozen_package, monkeypatch):
    import skill_eval_packaged_cli as host

    fixture = prepare(tmp_path, frozen_package, "install")
    install(fixture)
    before = {name: os.environ.get(name) for name in ("HOME", "CODEX_HOME", "LOCKBOT_HOME")}
    original = host.runpy.run_path
    observed = []

    def capture(path):
        observed.append({name: os.environ.get(name) for name in before})
        return original(path)

    monkeypatch.setattr(host.runpy, "run_path", capture)
    result = run(fixture, "--help")
    assert result["exit_code"] == 0 and "connections" in result["stdout"]
    assert observed[0]["HOME"] == before["HOME"]
    assert observed[0]["CODEX_HOME"] == before["CODEX_HOME"]
    assert observed[0]["LOCKBOT_HOME"] == str(fixture.host.private)
    assert {name: os.environ.get(name) for name in before} == before


def test_old_v6_tools_paths_and_scoring_are_replayable(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "install", version=6)
    assert fixture.host is None
    assert [tool["name"] for tool in evaluation.onboarding_tools(fixture)] == [
        "http_request",
        "read_file",
        "write_file",
        "inspect_archive",
        "install_skill",
    ]
    # Historical v6 allows arbitrary workspace destinations and does not require prior manifest retrieval to install.
    result = fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": fixture.manifest()["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    assert result["installed"]
    score = evaluation.score(fixture.root, "Installed.")
    assert not score["checks"]["public_manifest_checked"]
    assert score["checks"]["verified_installation"]
    assert "fixture_family" not in score and "native_global_discovery_verified" not in score


@pytest.mark.parametrize("family,version", [(None, 7), (None, 18), ("cli", 18), ("onboarding", 18), ("onboarding", 6)])
def test_family_and_version_gate_does_not_activate_other_fixture_families(tmp_path, frozen_package, family, version):
    fixture = prepare(tmp_path, frozen_package, version=6)
    value = {**fixture.case, "fixture_version": version}
    if family is not None:
        value["fixture_family"] = family
    evaluation.save_json(fixture.root / "case.json", value)
    assert evaluation.Fixture(fixture.root).host is None


@pytest.mark.parametrize("scenario", evaluation.SCENARIOS)
def test_v7_keeps_all_natural_prompt_bytes_and_marks_native_discovery_unverified(tmp_path, frozen_package, scenario):
    legacy = prepare(tmp_path, frozen_package, scenario, version=6)
    current = prepare(tmp_path, frozen_package, scenario, version=7)
    assert (legacy.root / "prompt.txt").read_bytes() == (current.root / "prompt.txt").read_bytes()
    assert current.case["fixture_family"] == "onboarding"
    assert "does not verify real Codex/ducc global discovery" in (current.root / "system.txt").read_text()


def test_handwritten_connection_cannot_substitute_for_actual_cli_init(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    fixture.call(
        "write_file",
        {
            "path": "private/connection.json",
            "content": json.dumps(
                {
                    "origin": evaluation.ORIGIN,
                    "instance_id": evaluation.INSTANCE,
                    "bot_uuid": evaluation.BOT_UUID,
                    "credential_ref": "private/current.key",
                }
            ),
        },
    )
    headers = {"Authorization": "Bearer " + evaluation.KEY}
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent/context", "headers": headers})
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent/resources", "headers": headers})
    result = evaluation.score(fixture.root, "Queried but did not create a CLI connection.")
    assert not result["checks"]["connection_written_by_verified_cli"]
    assert not result["observable_pass"]


def test_unrequested_project_scope_remains_observable_and_fails_user_scope_grade(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    created = run(
        fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key", "--scope", "project"
    )
    assert created["exit_code"] == 0
    assert created["result"]["connection"].startswith(".lockbot/")
    assert not evaluation.score(fixture.root, "Installed.")["checks"]["connection_user_scope"]


def test_user_storage_symlink_and_foreign_http_never_reach_ambient_host(tmp_path, frozen_package, monkeypatch):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    marker = tmp_path / "outside-private"
    marker.mkdir()
    fixture.host.private.symlink_to(marker)
    assert "symlink" in run(fixture, "connections")["error"]
    assert not list(marker.iterdir())
    assert "error" in fixture.call("discover_local_state", {})
    fixture.host.private.unlink()

    def no_network(*args, **kwargs):
        raise AssertionError("No fixture call may reach the real network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    result = run(fixture, "discover", "--origin", "https://real.invalid")
    assert result.get("exit_code") != 0
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert any(
        row["tool"] == "http_request"
        and row["args"]["url"] == "https://real.invalid/api/agent-skill"
        and row["result"]["status"] == 404
        for row in trace
    )


def test_missing_origin_discovery_does_not_supply_an_answer_or_contact_a_service(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "missing-origin")
    inventory = fixture.call("discover_local_state", {})
    assert evaluation.ORIGIN not in json.dumps(inventory)
    assert not inventory["private_files"] and not inventory["installed_skills"]
    result = evaluation.score(fixture.root, "Please provide the original service URL.")
    assert result["observable_pass"]
    assert result["evaluation_scope"] == "negative_compatibility"
    assert not (fixture.root / "cli-http.jsonl").exists()


@pytest.mark.parametrize("aggregate", [False, True])
def test_connection_metadata_cannot_smuggle_an_ambient_key_file(tmp_path, frozen_package, aggregate):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    outside = tmp_path / "ambient-private-key"
    outside.write_text(evaluation.KEY)
    target = "/synthetic-user/lockbot/connections/forged/connection.json"
    fixture.call(
        "write_file",
        {
            "path": target,
            "content": json.dumps(
                {
                    "approved_origin": evaluation.ORIGIN,
                    "instance_id": evaluation.INSTANCE,
                    "bot_uuid": evaluation.BOT_UUID,
                    "actor": {"platform": "Infoflow", "external_user_id": "demo-user"},
                    "key_file": str(outside),
                }
            ),
        },
    )
    before = (fixture.root / "trace.jsonl").read_text().count('"tool": "http_request"')
    result = run(fixture, "resources-all") if aggregate else run(fixture, "--connection", target, "resources")
    assert "private-file reference escapes workspace" in result["error"]
    after = (fixture.root / "trace.jsonl").read_text().count('"tool": "http_request"')
    assert before == after
    assert evaluation.KEY not in json.dumps(result)


def test_new_catalog_and_real_cli_are_available_through_actual_mcp_subprocess(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    config = json.loads((fixture.root / "mcp.json").read_text())["mcpServers"]["fixture"]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "run_lockbot",
                "arguments": {
                    "script": "/synthetic-user/skills/lockbot-agent/scripts/lockbot_cli.py",
                    "argv": ["init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key"],
                },
            },
        },
    ]
    process = subprocess.run(
        [config["command"], *config["args"]],
        input="".join(json.dumps(row) + "\n" for row in requests),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    names = [tool["name"] for tool in replies[1]["result"]["tools"]]
    assert names == [
        "http_request",
        "read_file",
        "write_file",
        "inspect_archive",
        "install_skill",
        "discover_local_state",
        "run_lockbot",
    ]
    result = json.loads(replies[2]["result"]["content"][0]["text"])
    assert result["result"]["connection_created"]
    assert not replies[2]["result"]["isError"]
    assert config["command"] == sys.executable


def test_new_matrix_onboarding_preparation_explicitly_selects_only_onboarding_v7(tmp_path, frozen_package):
    import shutil

    import skill_eval_matrix as matrix

    artifacts = tmp_path / "matrix-artifacts"
    shutil.copytree(frozen_package, artifacts / "current")
    root = tmp_path / "new-matrix-case"
    matrix.prepare(root, "onboarding", "install", artifacts)
    case = json.loads((root / "case.json").read_text())
    assert (case["fixture_family"], case["fixture_version"]) == ("onboarding", 7)
    assert evaluation.Fixture(root).host is not None


def test_v7_resource_wire_has_current_api_identity_and_capture_fields_without_hardware(tmp_path, frozen_package):
    from datetime import datetime, timezone

    fixture = prepare(tmp_path, frozen_package)
    result = fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/agent/resources", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )["body"]
    assert (datetime.now(timezone.utc) - datetime.fromisoformat(result["captured_at"])).total_seconds() < 5
    assert result["age_seconds"] == 0
    assert result["context"]["bot"]["uuid"] == evaluation.BOT_UUID
    assert result["resources"] == [{"id": "node-a", "lock_status": "idle", "holders": [], "booking_list": []}]
    legacy = prepare(tmp_path, frozen_package, version=6)
    old = legacy.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/agent/resources", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )["body"]
    assert old == {"resources": [{"resource_id": "node-a", "holders": []}]}


def test_model_errors_neutralize_embedded_case_paths_but_keep_private_raw_evidence(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package, "missing-origin")
    result = fixture.call("read_file", {"path": "not-present.txt"})
    assert str(fixture.root) not in result["error"]
    assert "missing-origin" not in result["error"]
    assert "/synthetic-workspace/not-present.txt" in result["error"]
    event = json.loads((fixture.root / "trace.jsonl").read_text().splitlines()[-1])
    assert event["result"] == result
    assert str(fixture.workspace) in event["raw_result"]["error"]
    legacy = prepare(tmp_path, frozen_package, "missing-origin", version=6)
    old = legacy.call("read_file", {"path": "not-present.txt"})
    assert str(legacy.root) in old["error"]  # Preserve historical error bytes/behavior.


def test_real_cli_outputs_remain_exact_in_execution_metadata_only(tmp_path, frozen_package):
    fixture = prepare(tmp_path, frozen_package)
    install(fixture)
    result = run(fixture, "init", "--origin", evaluation.ORIGIN, "--key-file", "private/current.key")
    assert result["result"]["connection"].startswith("/synthetic-user/")
    event = json.loads((fixture.root / "trace.jsonl").read_text().splitlines()[-1])
    assert event["result"] == result
    assert str(fixture.workspace) in event["raw_result"]["result"]["connection"]
    assert str(fixture.workspace) in event["stdout"]
    assert event["argv"][0].startswith(str(fixture.workspace))
    grade = evaluation.score(fixture.root, "Connection created.")
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert grade["tool_calls"] == sum(row.get("source") != "packaged_cli" for row in trace)
    assert grade["cli_http_requests"] == sum(row.get("source") == "packaged_cli" for row in trace)
