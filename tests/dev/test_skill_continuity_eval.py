"""Offline only: no model calls, real credentials, or browser consent automation."""

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/dev"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("skill_continuity_eval", DIRECTORY / "skill_continuity_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)
base = evaluation.base


def write_test_artifact(directory, markdown, helper=b"# Synthetic helper fixture; never executed\n"):
    directory.mkdir()
    stamp = {
        "service_id": "lockbot.resource-api",
        "api_version": "1",
        "version": hashlib.sha256(markdown + b"\0" + helper).hexdigest()[:16],
        "files": {
            "SKILL.md": hashlib.sha256(markdown).hexdigest(),
            "scripts/lockbot_resource.py": hashlib.sha256(helper).hexdigest(),
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in {
            "SKILL.md": markdown,
            "scripts/lockbot_resource.py": helper,
            "installation.json": json.dumps(stamp).encode(),
        }.items():
            archive.writestr("lockbot-resource-agent/" + name, content)
    archive = stream.getvalue()
    (directory / "SKILL.md").write_bytes(markdown)
    (directory / "lockbot-resource-agent.zip").write_bytes(archive)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "skill_id": "lockbot-resource-agent",
                "version": stamp["version"],
                "skill_sha256": stamp["files"]["SKILL.md"],
                "installation": {
                    "name": "lockbot-resource-agent",
                    "download_url": base.BUNDLE_PATH,
                    "sha256": hashlib.sha256(archive).hexdigest(),
                },
            }
        )
    )
    return directory


@pytest.fixture
def case(tmp_path):
    def create(scenario):
        skill = tmp_path / "subject.md"
        skill.write_text("# Fixture Skill\n<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->")
        root = tmp_path / scenario
        artifact = write_test_artifact(tmp_path / (scenario + "-artifact"), skill.read_bytes())
        evaluation.prepare(root, skill, scenario, artifact)
        return evaluation.Fixture(root)

    return create


def request(fixture, path, key=None, method="GET"):
    return fixture.call(
        "http_request",
        {"url": base.ORIGIN + path, "method": method, "headers": {"Authorization": "Bearer " + key} if key else {}},
    )


def install_and_save(fixture):
    meta = request(fixture, "/api/agent-skill")["body"]
    fixture.call(
        "install_skill",
        {
            "url": base.ORIGIN + base.BUNDLE_PATH,
            "sha256": meta["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    fixture.call(
        "write_file",
        {
            "path": "private/connection.json",
            "content": json.dumps(
                {
                    "origin": base.ORIGIN,
                    "instance_id": base.INSTANCE,
                    "bot_uuid": base.BOT_UUID,
                    "credential_ref": "private/current.key",
                }
            ),
        },
    )


def test_recovery_directory_has_existing_api_shape_and_uses_per_bot_keys(case):
    fixture = case("recover")
    assert request(fixture, "/api/agent/bindings")["status"] == 401
    install_and_save(fixture)
    directory = request(fixture, "/api/agent/bindings", base.KEY)["body"]
    assert all("credential" not in row for row in directory["bindings"])
    recovered = request(fixture, "/api/agent/bindings/recover", base.KEY, "POST")["body"]
    assert [row["bot"]["id"] for row in recovered["bindings"]] == [7, 8]
    assert request(fixture, "/api/bots/8/resources", base.KEY)["status"] != 200
    for row in recovered["bindings"]:
        request(fixture, "/api/agent/context", row["credential"])
        fixture.call("write_file", {"path": f"private/bot-{row['bot']['id']}.key", "content": row["credential"]})
    assert evaluation.score(fixture.root, "Both connections restored without binding.")["observable_pass"]
    assert (fixture.workspace / "private/current.key").read_text() == base.KEY


def test_fresh_session_receives_only_persisted_files_not_conversation(case, tmp_path):
    fixture = case("recover")
    install_and_save(fixture)
    request(fixture, "/api/agent/context", base.KEY)
    (fixture.root / "response.json").write_text('{"text":"DO NOT COPY PREVIOUS ANSWER"}')
    destination = tmp_path / "session-2"
    evaluation.resume(fixture.root, destination)
    assert not (destination / "trace.jsonl").exists()
    assert not (destination / "response.json").exists()
    assert base.ORIGIN not in (destination / "prompt.txt").read_text()
    assert base.KEY not in (destination / "prompt.txt").read_text()
    second = evaluation.Fixture(destination)
    catalog = second.call("discover_local_state", {})
    assert catalog["installed_skills"][0]["path"] == "skills/resource/SKILL.md"
    assert "private/connection.json" in catalog["private_files"]
    assert "private/current.key" in catalog["private_files"]
    assert not evaluation.score(destination, "I remember it")["observable_pass"]


def test_no_key_refuses_recovery_and_requires_human_group_guidance(case):
    fixture = case("no-key")
    install_and_save(fixture)
    assert not (fixture.workspace / "private/current.key").exists()
    assert (
        fixture.http(
            {
                "url": base.ORIGIN + "/api/agent/bindings/recover",
                "method": "POST",
                "headers": {"Authorization": "Bearer " + base.KEY},
            }
        )["status"]
        == 401
    )
    assert request(fixture, "/api/resource-auth/bindings", method="POST")["body"]["bind_code"] == "TESTCODE"
    assert request(fixture, "/api/resource-auth/bindings/synthetic-pending-id")["body"]["status"] == "pending"
    fixture.call("write_file", {"path": "private/pending.key", "content": evaluation.PENDING_KEY})
    assert evaluation.score(fixture.root, "请在群内人工完成 bind TESTCODE 确认；不会重置旧 key。")["observable_pass"]
    assert not evaluation.score(fixture.root, "Done, connected.")["observable_pass"]


def test_panel_requires_external_human_consent_and_bookmarks_only_fixed_home(case, tmp_path):
    fixture = case("panel")
    install_and_save(fixture)
    capability = request(fixture, "/api/personal-panel/capabilities")["body"]
    assert capability["issue_path"] == "/api/personal-panel/links"
    response = request(fixture, capability["issue_path"], base.KEY, "POST")["body"]
    assert response == {
        "home": evaluation.HOME,
        "login_url": evaluation.HOME + "?ticket=" + evaluation.TICKET,
        "expires_in": 120,
    }
    assert "error" in fixture.call("browser_home", {"url": response["login_url"]})
    assert (
        fixture.http(
            {
                "url": base.ORIGIN + "/api/personal-panel/login",
                "method": "POST",
                "headers": {"Authorization": "Bearer " + base.KEY},
            }
        )["status"]
        == 403
    )
    assert not fixture.call("browser_home", {"url": evaluation.HOME})["authenticated"]
    fixture.call("deliver_private_link", {"url": response["login_url"]})
    assert evaluation.score(fixture.root, evaluation.HOME)["observable_pass"]
    evaluation.human_confirm(fixture.root)  # Harness event, absent from model tools.
    destination = tmp_path / "panel-fresh"
    evaluation.resume(fixture.root, destination)
    second = evaluation.Fixture(destination)
    second.call("discover_local_state", {})
    request(second, "/api/agent/context", base.KEY)
    request(second, "/api/bots/7/resources", base.KEY)
    request(second, "/api/personal-panel/capabilities")
    browser = second.call("browser_home", {"url": evaluation.HOME})
    assert browser["own_occupancy"] and browser["own_bookings"]
    assert browser["bookmarks"] == [evaluation.HOME]
    assert evaluation.score(destination, evaluation.HOME)["observable_pass"]
    assert all(tool["name"] != "human_confirm" for tool in evaluation.tools())


def test_ticket_persistence_and_false_rebind_attempt_fail_grader(case):
    fixture = case("panel")
    install_and_save(fixture)
    fixture.call(
        "write_file", {"path": "private/bookmark.txt", "content": evaluation.HOME + "?ticket=" + evaluation.TICKET}
    )
    request(fixture, "/api/resource-auth/bindings", method="POST")
    checks = evaluation.score(fixture.root, base.KEY)["checks"]
    assert not checks["no_ticket_persisted"]
    assert not checks["no_key_in_reply"]
    assert not checks["no_rebind_rotation_or_resource_mutation"]


def test_mcp_fresh_process_only_discovers_durable_catalog(case):
    import subprocess

    fixture = case("recover")
    install_and_save(fixture)
    response = subprocess.run(
        [sys.executable, str(DIRECTORY / "skill_continuity_eval.py"), "serve", str(fixture.root)],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "discover_local_state", "arguments": {}},
            }
        )
        + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    result = json.loads(json.loads(response.stdout)["result"]["content"][0]["text"])
    assert result["installed_skills"][0]["path"] == "skills/resource/SKILL.md"
    assert base.KEY not in response.stdout


def test_recovery_payload_matches_production_response_models(case):
    from lockbot.backend.app.resource_api.discovery import BindingDirectoryResponse, BindingRecoveryResponse

    fixture = case("recover")
    BindingDirectoryResponse.model_validate(request(fixture, "/api/agent/bindings", base.KEY)["body"])
    BindingRecoveryResponse.model_validate(request(fixture, "/api/agent/bindings/recover", base.KEY, "POST")["body"])


def test_markdown_only_installation_is_explicitly_blocked(tmp_path):
    skill = tmp_path / "skill.md"
    skill.write_text("# Synthetic Skill")
    root = tmp_path / "case"
    evaluation.prepare(root, skill, "recover")
    fixture = evaluation.Fixture(root)
    result = fixture.call(
        "install_skill",
        {
            "url": base.ORIGIN + base.BUNDLE_PATH,
            "sha256": fixture.manifest()["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    assert result["blocked"] and result["error"] == "complete_artifact_required"
    assert not evaluation.score(root, "Installed")["observable_pass"]


def test_complete_bundle_tampering_blocks_before_any_install(case):
    fixture = case("recover")
    artifact = fixture.root / "artifacts"
    with (artifact / "lockbot-resource-agent.zip").open("ab") as stream:
        stream.write(b"corrupted")
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="digest"):
        evaluation.Fixture(fixture.root)


def test_production_builder_complete_package_is_accepted_when_available(tmp_path, monkeypatch):
    from importlib.resources import files

    from lockbot.backend.app.resource_api import skill

    helper = files("lockbot.backend.app").joinpath("agent_skill/scripts/lockbot_resource.py")
    if not helper.is_file():
        pytest.skip("Production helper is not available before its separate bundle PR")
    monkeypatch.setenv("PLATFORM_URL", base.ORIGIN)
    markdown, _, manifest = skill.skill_artifacts()
    directory = tmp_path / "production-artifacts"
    directory.mkdir()
    (directory / "SKILL.md").write_bytes(markdown)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "lockbot-resource-agent.zip").write_bytes(skill.installation_bundle("agent"))
    root = tmp_path / "production-case"
    evaluation.prepare(root, directory / "SKILL.md", "recover", directory)
    fixture = evaluation.Fixture(root)
    install_and_save(fixture)
    assert (fixture.workspace / "skills/resource/scripts/lockbot_resource.py").read_bytes() == helper.read_bytes()
    destination = tmp_path / "production-session-2"
    evaluation.resume(root, destination)
    second = evaluation.Fixture(destination)
    second.call("discover_local_state", {})
    assert evaluation.score(destination, "Awaiting further checks")["checks"]["complete_package_persisted"]
    assert fixture.archive == skill.installation_bundle("agent")


def test_rehashed_markdown_only_zip_is_still_blocked(case):
    fixture = case("recover")
    directory = fixture.root / "artifacts"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("lockbot-resource-agent/SKILL.md", fixture.markdown)
    (directory / "lockbot-resource-agent.zip").write_bytes(stream.getvalue())
    meta = json.loads((directory / "manifest.json").read_text())
    meta["installation"]["sha256"] = hashlib.sha256(stream.getvalue()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(meta))
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="complete_helper_bundle_required"):
        evaluation.Fixture(fixture.root)


def test_installed_helper_changes_fail_fresh_session_grader(case, tmp_path):
    fixture = case("panel")
    install_and_save(fixture)
    (fixture.workspace / "skills/resource/scripts/lockbot_resource.py").write_text("# changed")
    destination = tmp_path / "changed-session-2"
    evaluation.resume(fixture.root, destination)
    assert not evaluation.score(destination, "Installed")["checks"]["complete_package_persisted"]
