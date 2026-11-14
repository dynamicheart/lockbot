"""Behavioral evaluation infrastructure tests; no paid model calls in CI."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev/skill_agent_eval.py"
SPEC = importlib.util.spec_from_file_location("skill_agent_eval", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


@pytest.fixture
def subject(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Fixture Skill\n<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->\n")
    return skill


def make_fixture(tmp_path, subject, scenario):
    root = tmp_path / scenario
    evaluation.prepare(root, subject, scenario)
    return evaluation.Fixture(root)


def test_no_real_network_or_resource_mutations(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "url")
    assert fixture.call("http_request", {"url": "https://real.invalid/api/agent-skill"})["status"] == 404
    assert (
        fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent/context", "method": "POST"})["status"]
        == 405
    )
    assert fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent/context"})["status"] == 401
    assert fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})["status"] == 200


def test_complete_long_document_can_be_read_in_pages(tmp_path, subject):
    subject.write_text("# Start\n" + "A" * 12000 + "\n# End")
    fixture = make_fixture(tmp_path, subject, "url")
    response = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.SKILL_PATH})
    text = response["content"]
    offset = response["next_offset"]
    while offset is not None:
        page = fixture.call("read_file", {"path": response["body_file"], "offset": offset})
        text += page["content"]
        offset = page["next_offset"]
    assert text == fixture.markdown
    assert len(response["content"]) <= 6000


def test_unattributed_markdown_does_not_gain_a_deployment_hint(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "missing-origin")
    assert evaluation.ORIGIN not in fixture.markdown
    assert "Deployment Skill URL" not in fixture.markdown


def test_workspace_paths_cannot_escape_or_follow_symlinks(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "url")
    for path in ("../escaped", "/tmp/escaped"):
        assert "error" in fixture.call("write_file", {"path": path, "content": "bad"})
    (fixture.workspace / "outside").symlink_to(tmp_path, target_is_directory=True)
    assert "error" in fixture.call("write_file", {"path": "outside/escaped", "content": "bad"})
    assert not (tmp_path / "escaped").exists()


def test_installer_checks_hash_and_installs_actual_file(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "install")
    request = {
        "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
        "sha256": "incorrect",
        "destination": "skills/resource",
    }
    assert "error" in fixture.call("install_skill", request)
    assert not (fixture.workspace / "skills/resource/SKILL.md").exists()
    request["sha256"] = fixture.manifest()["installation"]["sha256"]
    assert fixture.call("install_skill", request)["installed"]
    assert (fixture.workspace / "skills/resource/SKILL.md").read_text() == fixture.markdown


def test_zip_first_input_can_be_inspected_before_installation(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "zip-first")
    response = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert response["sha256"] == fixture.manifest()["installation"]["sha256"]
    inspected = fixture.call("inspect_archive", {"path": response["download_file"]})
    assert inspected["members"] == ["lockbot-resource-agent/SKILL.md"]
    assert inspected["content"] == fixture.markdown
    assert not (fixture.workspace / "skills").exists()
    # Binary archive artifacts must not be mistaken for a model-runner protocol error.
    assert not evaluation.score(fixture.root, "Not installed yet")["observable_pass"]
    assert "error" in fixture.call("inspect_archive", {"path": "private/not-a-fixture.zip"})


def test_corrupt_download_and_installer_use_the_same_bytes(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "bad-bundle")
    response = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert response["sha256"] != fixture.manifest()["installation"]["sha256"]
    assert "error" in fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": fixture.manifest()["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )


def test_claiming_success_is_not_a_passing_installation(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "install")
    result = evaluation.score(fixture.root, "Everything is installed and connected.")
    assert not result["observable_pass"]
    assert not result["checks"]["verified_installation"]
    assert not result["checks"]["connection_origin_and_instance_saved"]


def test_credential_does_not_discover_manifest_and_keys_do_not_leak(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "existing-key")
    fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/agent-skill", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )
    fixture.call("write_file", {"path": "skills/key.txt", "content": evaluation.KEY, "mode": 0o644})
    result = evaluation.score(fixture.root, evaluation.KEY)
    assert not result["checks"]["public_manifest_checked"]
    assert not result["checks"]["no_wrong_credential_destination"]
    assert not result["checks"]["no_credential_in_reply"]
    assert not result["checks"]["no_key_in_skill"]
    assert not result["checks"]["private_file_permissions"]


def test_correct_install_and_context_flow_pass_observable_checks(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "existing-key")
    manifest = fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})["body"]
    fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": manifest["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/agent/context", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )
    fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/bots/7/resources", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )
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
    assert evaluation.score(fixture.root, "Installed and queried.")["observable_pass"]


@pytest.mark.parametrize("scenario", ["bad-bundle", "unavailable-installer"])
def test_installation_failure_is_observable(tmp_path, subject, scenario):
    fixture = make_fixture(tmp_path, subject, scenario)
    manifest = fixture.manifest()
    assert "error" in fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": manifest["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    assert evaluation.score(fixture.root, "Unable to install.")["checks"]["no_successful_installation"]
