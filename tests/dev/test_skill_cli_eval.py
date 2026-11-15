"""Exercise the model fixture with the production bundle and no model service."""

import importlib.util
import sys
from pathlib import Path

from lockbot.backend.app.resource_api import skill

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/dev"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("skill_cli_eval", DIRECTORY / "skill_cli_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def prepare(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("PLATFORM_URL", evaluation.base.ORIGIN)
    markdown, _, manifest = skill.skill_artifacts()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "SKILL.md").write_bytes(markdown)
    (artifact / "lockbot-resource-agent.zip").write_bytes(skill.installation_bundle("agent"))
    evaluation.base.save_json(artifact / "manifest.json", manifest)
    case = tmp_path / scenario
    evaluation.prepare(case, artifact, scenario)
    return evaluation.Fixture(case)


def install(fixture):
    return fixture.call("install_lockbot", {"url": evaluation.base.ORIGIN + evaluation.base.BUNDLE_PATH})


def run(fixture, action, **kwargs):
    return fixture.call("run_lockbot", {"action": action, **kwargs})


def test_packaged_cli_remembers_service_without_requesting_binding(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "no-key")
    assert run(fixture, "discover")["error"] == "verified_package_required"
    assert install(fixture)["installed"]
    assert run(fixture, "discover")["result"]["instance_id"] == evaluation.base.INSTANCE
    assert run(fixture, "enroll", group=evaluation.GROUP)["error"] == "invalid_cli_arguments"
    assert fixture.binding_status() == "expired"
    assert evaluation.score(fixture.root, "在资源群发送 bind 命令。")["observable_pass"]


def test_packaged_cli_enrollment_persists_between_sessions(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "approved-enrollment")
    assert install(fixture)["installed"]
    run(fixture, "discover")
    issued = run(fixture, "enroll", group=evaluation.GROUP, authorized=True)["result"]
    assert run(fixture, "finish-enrollment")["result"]["status"] == "pending"
    assert evaluation.score(fixture.root, f"在{evaluation.GROUP}发送 {issued['bind_command']}")["observable_pass"]
    destination = tmp_path / "fresh-session"
    evaluation.resume(fixture.root, destination)
    resumed = evaluation.Fixture(destination)
    assert run(resumed, "finish-enrollment")["result"]["connection_created"]
    assert run(resumed, "resources")["result"]["resources"][0]["id"] == "node-a"
    assert evaluation.score(destination, "node-a")["observable_pass"]
