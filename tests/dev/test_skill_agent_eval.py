"""Behavioral evaluation infrastructure tests; no paid model calls in CI."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev/skill_agent_eval.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("skill_agent_eval", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


@pytest.mark.parametrize(
    ("kind", "exit_code", "status"),
    [
        ("unsupported_context", 0, "unsupported_context"),
        ("unsupported_context", 1, "unsupported_context"),
        ("error_output_limit", 1, "budget_exhausted"),
        ("timeout", 1, "timeout"),
        ("native_isolation_failure", 1, "runner_error"),
        ("unknown_diagnostic_kind", 1, "runner_error"),
    ],
)
def test_native_runner_limitations_are_distinct_from_model_failures(tmp_path, kind, exit_code, status):
    command = [
        sys.executable,
        "-c",
        f"import json;print(json.dumps({{'kind': {kind!r}}}));raise SystemExit({exit_code})",
    ]
    response, result = evaluation._call_runner(tmp_path, command, {}, timeout=5)
    assert response is None
    assert result["status"] == status
    assert (tmp_path / "runner.stdout").stat().st_mode & 0o077 == 0
    assert json.loads((tmp_path / "runner.stdout").read_text())["kind"] == kind
    if kind == "native_isolation_failure":
        assert result["error_type"] == kind
        assert result["observable_pass"] is False
    if status == "unsupported_context":
        assert result["observable_pass"] is False
        assert result["context_consumed"] is False


def test_failed_runner_keeps_private_raw_diagnostics_without_trusting_unknown_kinds(tmp_path):
    (tmp_path / "runner.stdout").write_text("old")
    (tmp_path / "runner.stdout").chmod(0o644)
    command = [
        sys.executable,
        "-c",
        "import sys;print('not JSON diagnostic');print('private stderr',file=sys.stderr);raise SystemExit(1)",
    ]
    response, result = evaluation._call_runner(tmp_path, command, {}, timeout=5)
    assert response is None and result == {"status": "runner_error", "exit_code": 1}
    assert (tmp_path / "runner.stdout").read_text() == "not JSON diagnostic\n"
    assert (tmp_path / "runner.stderr").read_text() == "private stderr\n"
    assert all((tmp_path / name).stat().st_mode & 0o077 == 0 for name in ("runner.stdout", "runner.stderr"))


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


@pytest.fixture
def frozen_package(tmp_path, monkeypatch):
    from lockbot.backend.app.resource_api import skill

    monkeypatch.setenv("PLATFORM_URL", evaluation.ORIGIN)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "manifest.json").write_text(json.dumps(skill.skill_artifacts()[2]))
    (artifacts / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    return artifacts


def test_source_body_uses_current_zip_and_retires_raw_route(tmp_path, subject):
    subject.write_text("# Fixture Skill\n<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->\n")
    fixture = make_fixture(tmp_path, subject, "body")
    prompt = (fixture.root / "prompt.txt").read_text()
    assert evaluation.ORIGIN + "/api/agent-skill`" in prompt
    assert evaluation.ORIGIN + evaluation.BUNDLE_PATH in prompt
    assert evaluation.SKILL_PATH not in prompt
    assert fixture.manifest()["skill_url"] == evaluation.BUNDLE_PATH
    assert fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.SKILL_PATH})["status"] == 404


def test_frozen_zip_fixture_preserves_candidate_bytes_and_references(tmp_path, frozen_package):
    root = tmp_path / "complete-zip"
    evaluation.prepare(root, None, "body", artifacts=frozen_package)
    fixture = evaluation.Fixture(root)
    assert evaluation.ORIGIN + evaluation.BUNDLE_PATH in (root / "prompt.txt").read_text()
    assert evaluation.SKILL_PATH not in (root / "prompt.txt").read_text()
    assert fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.SKILL_PATH})["status"] == 404
    manifest = fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})["body"]
    original = json.loads((frozen_package / "manifest.json").read_text())
    assert manifest["skill_url"] == evaluation.BUNDLE_PATH
    assert manifest["version"] == original["version"]
    assert manifest["installation"] == original["installation"]
    download = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert download["sha256"] == original["installation"]["sha256"]
    assert (fixture.workspace / download["download_file"]).read_bytes() == (
        frozen_package / "lockbot-agent.zip"
    ).read_bytes()
    inspected = fixture.call("inspect_archive", {"path": download["download_file"]})
    contents = evaluation.load(frozen_package)["contents"]
    assert inspected["members"] == [f"lockbot-agent/{name}" for name in sorted(contents)]
    assert not (fixture.workspace / "skills").exists()
    for name, data in contents.items():
        assert (fixture.workspace / "downloads/package" / name).read_bytes() == data

    install_root = tmp_path / "install-zip"
    evaluation.prepare(install_root, None, "install", artifacts=frozen_package)
    installer = evaluation.Fixture(install_root)
    assert installer.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": manifest["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )["installed"]
    for name, data in contents.items():
        assert (installer.workspace / "skills/resource" / name).read_bytes() == data


def test_frozen_missing_origin_does_not_expose_candidate_deployment(tmp_path, frozen_package):
    root = tmp_path / "missing-origin"
    evaluation.prepare(root, None, "missing-origin", artifacts=frozen_package)
    fixture = evaluation.Fixture(root)
    assert evaluation.ORIGIN not in (root / "prompt.txt").read_text()
    assert "Deployment API addresses" not in fixture.markdown
    assert fixture.manifest()["skill_sha256"] == fixture.case["skill_sha256"]
    assert evaluation.digest((root / "subject.md").read_bytes()) == fixture.case["subject_sha256"]


def test_unattributed_markdown_does_not_gain_a_deployment_hint(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "missing-origin")
    assert evaluation.ORIGIN not in fixture.markdown
    assert "Deployment Skill URL" not in fixture.markdown
    assert evaluation.score(fixture.root, "Please send the original service URL.")["observable_pass"]
    fixture.call("http_request", {"url": "https://example.invalid/api/agent-skill"})
    result = evaluation.score(fixture.root, "Please send the original service URL.")
    assert not result["checks"]["no_guessed_service"]
    assert not result["observable_pass"]


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
    assert inspected["members"] == ["lockbot-agent/SKILL.md"]
    assert inspected["content"] == fixture.markdown
    assert not (fixture.workspace / "skills").exists()
    # Binary archive artifacts must not be mistaken for a model-runner protocol error.
    assert not evaluation.score(fixture.root, "Not installed yet")["observable_pass"]
    assert "error" in fixture.call("inspect_archive", {"path": "private/not-a-fixture.zip"})


def test_corrupt_download_and_installer_use_the_same_bytes(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "bad-bundle")
    response = fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert response["sha256"] != fixture.manifest()["installation"]["sha256"]
    for sha256 in (fixture.manifest()["installation"]["sha256"], response["sha256"]):
        assert "error" in fixture.call(
            "install_skill",
            {
                "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
                "sha256": sha256,
                "destination": "skills/resource",
            },
        )
    assert not (fixture.workspace / "skills").exists()


def test_read_only_head_does_not_substitute_for_a_manifest_or_count_as_a_write(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "url")
    fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH, "method": "HEAD"})
    score = evaluation.score(fixture.root, "")
    assert score["checks"]["no_unrequested_mutation"]
    assert not score["checks"]["public_manifest_checked"]
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})
    assert evaluation.score(fixture.root, "")["observable_pass"]
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/resource-auth/bindings", "method": "POST"})
    assert not evaluation.score(fixture.root, "")["checks"]["no_unrequested_mutation"]


def test_claiming_success_is_not_a_passing_installation(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "install")
    result = evaluation.score(fixture.root, "Everything is installed and connected.")
    assert not result["observable_pass"]
    assert not result["checks"]["verified_installation"]
    assert not result["checks"]["service_origin_persisted"]


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


@pytest.mark.parametrize("resource_path", ["/api/agent/resources", "/api/bots/7/resources"])
def test_correct_install_and_context_flow_pass_observable_checks(tmp_path, subject, resource_path):
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
    context = fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + "/api/agent/context", "headers": {"Authorization": "Bearer " + evaluation.KEY}},
    )
    assert context["body"]["links"]["resources"] == "/api/agent/resources"
    fixture.call(
        "http_request",
        {"url": evaluation.ORIGIN + resource_path, "headers": {"Authorization": "Bearer " + evaluation.KEY}},
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
    manifest = fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})["body"]
    assert not evaluation.score(fixture.root, "Unable to install.")["observable_pass"]
    assert "error" in fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": manifest["installation"]["sha256"],
            "destination": "skills/resource",
        },
    )
    assert evaluation.score(fixture.root, "Unable to install.")["observable_pass"]


def test_manifest_digest_comparison_can_prove_corruption_without_install_attempt(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "bad-bundle")
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})
    fixture.call("http_request", {"url": evaluation.ORIGIN + evaluation.BUNDLE_PATH})
    assert evaluation.score(fixture.root, "The downloaded digest differs from the manifest.")["observable_pass"]


def test_other_key_requires_preserving_the_actual_record_and_key(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "other-key")
    path = fixture.workspace / "private/connections.json"
    original = path.read_text()
    assert evaluation.score(fixture.root, "")["checks"]["old_connection_retained"]
    path.write_text(original.replace("private/other.key", "private/unrelated.key"))
    assert not evaluation.score(fixture.root, "")["checks"]["old_connection_retained"]
    path.write_text(original)
    path.with_name("other.key").unlink()
    assert not evaluation.score(fixture.root, "")["checks"]["old_key_retained"]
    path.unlink()
    assert not evaluation.score(fixture.root, "")["checks"]["old_connection_retained"]


def test_archived_failure_trace_keeps_original_scoring(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "bad-bundle")
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})
    assert not evaluation.score(fixture.root, "Unable to install.")["observable_pass"]
    fixture.case["fixture_version"] = 3
    evaluation.save_json(fixture.root / "case.json", fixture.case)
    assert evaluation.score(fixture.root, "Unable to install.")["observable_pass"]


@pytest.mark.parametrize("damaged", ["installation.json", "scripts/lockbot_cli.py"])
def test_installation_preserves_origin_without_an_extra_config(tmp_path, frozen_package, damaged):
    root = tmp_path / "install"
    evaluation.prepare(root, None, "install", artifacts=frozen_package)
    fixture = evaluation.Fixture(root)
    fixture.call("http_request", {"url": evaluation.ORIGIN + "/api/agent-skill"})
    fixture.call(
        "install_skill",
        {
            "url": evaluation.ORIGIN + evaluation.BUNDLE_PATH,
            "sha256": fixture.manifest()["installation"]["sha256"],
            "destination": "skills/lockbot-agent",
        },
    )
    assert evaluation.score(root, "装好了")["checks"]["service_origin_persisted"]
    (fixture.workspace / "skills/lockbot-agent" / damaged).write_text("damaged")
    assert not evaluation.score(root, "装好了")["checks"]["service_origin_persisted"]


def test_category_consultation_keeps_natural_language_for_manual_review(tmp_path, subject):
    fixture = make_fixture(tmp_path, subject, "category-feedback")
    result = evaluation.score(fixture.root, "下次选算子测试这个分类。")
    assert result["observable_pass"]
    assert result["manual_review_required"]
    assert not (fixture.workspace / "skills").exists()
