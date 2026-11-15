"""Exercise the model fixture with the production bundle and no model service."""

import importlib.util
import sys
from pathlib import Path

import pytest
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


def test_packaged_cli_recovers_and_uses_other_group(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "recover")
    assert install(fixture)["installed"]
    assert run(fixture, "init")["result"]["connection_created"]
    connections = run(fixture, "recover")["result"]["connections"]
    assert [entry["status"] for entry in connections] == ["existing", "saved"]
    assert run(fixture, "resources", group=evaluation.GROUP)["result"]["resources"]
    assert evaluation.score(fixture.root, "已查询验证群")["observable_pass"]


@pytest.mark.parametrize(
    ("scenario", "groups", "verdict"),
    [
        ("intent-exact", ["加速卡群"], "已锁定 node-10 的 2、3 卡。"),
        ("intent-query", ["加速卡群"], "node-10 的 2、3 号卡空闲。"),
        ("intent-ambiguous", ["加速卡群", "整机队列群"], "你指的是哪个群？"),
        ("intent-choose", ["加速卡群"], "已锁定 node-10 的 2、3 卡。"),
        (
            "intent-model-find",
            ["加速卡群", "备选加速卡群"],
            "node-10 的 2 号 P800 和 node-20 的 2 号 P900 都空闲。",
        ),
        (
            "intent-model-allocate",
            ["加速卡群", "备选加速卡群"],
            "已锁定备选加速卡群 node-20 的 2 号 P900 卡两小时。",
        ),
        (
            "intent-model-unknown-node",
            ["整机队列群"],
            "node-20 空闲，但整机的型号仅有别名提示，无法确认型号。",
        ),
        (
            "intent-model-whole-machine",
            ["整机队列群"],
            "已锁定 node-23 整机两小时，含两张 P900。",
        ),
        ("intent-whole", ["整机队列群"], "整机队列群只支持整机，请确认。"),
        ("intent-book", ["整机队列群"], "node-20 已预约两小时。"),
        ("intent-stale", ["加速卡群"], "快照已过期，无法确认有两张空闲卡。"),
    ],
)
def test_intent_cases_grade_actual_cli_targets(tmp_path, monkeypatch, scenario, groups, verdict):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    for group in groups:
        assert run(fixture, "resources", group=group)["result"]["resources"]
    if scenario in evaluation.intent.EXPECTED:
        if scenario == "intent-book":
            result = run(fixture, "command", group=groups[0], operation="book", nodes=["node-20"], duration="120m")
        elif scenario == "intent-model-whole-machine":
            snapshot = run(fixture, "resources", group=groups[0])["result"]
            nodes = {node["id"]: node for node in snapshot["resources"]}
            assert "accelerators" not in nodes["node-20"]
            assert nodes["node-22"]["lock_status"] == "exclusive"
            assert nodes["node-23"]["accelerators"] == [
                {"model": "P800", "count": 6},
                {"model": "P900", "count": 2},
            ]
            result = run(fixture, "command", group=groups[0], operation="lock", nodes=["node-23"], duration="120m")
        elif scenario == "intent-model-allocate":
            result = run(
                fixture,
                "command",
                group=groups[1],
                operation="lock",
                nodes=["node-20"],
                devices=["2"],
                duration="120m",
            )
        else:
            result = run(
                fixture,
                "command",
                group=groups[0],
                operation="lock",
                nodes=["node-10"],
                devices=["3", "2"],
                duration="120m",
            )
        assert result.get("result", {}).get("verified"), result
    grade = evaluation.score(fixture.root, verdict)
    assert grade["observable_pass"], grade["checks"]


def test_immediate_whole_machine_request_fails_if_model_books_first(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-model-whole-machine")
    assert install(fixture)["installed"]
    group = "整机队列群"
    assert run(fixture, "resources", group=group)["result"]["resources"]
    booked = run(fixture, "command", group=group, operation="book", nodes=["node-23"], duration="2h")
    assert booked["result"]["verified"]
    locked = run(fixture, "command", group=group, operation="lock", nodes=["node-23"], duration="2h")
    assert locked["result"]["verified"]

    grade = evaluation.score(fixture.root, "已锁定 node-23 整机两小时，含两张 P900。")
    assert not grade["observable_pass"]
    assert not grade["checks"]["exactly_one_correct_cli_write"]


def test_intent_case_fails_wrong_attempt_even_if_the_cli_refuses_it(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-whole")
    assert install(fixture)["installed"]
    assert run(fixture, "resources", group="整机队列群")["result"]["resources"]
    result = run(
        fixture,
        "command",
        group="整机队列群",
        operation="lock",
        nodes=["node-20"],
        devices=["2", "3"],
        duration="2h",
    )
    assert result["error"] == "explicit_device_selection_required"
    checks = evaluation.score(fixture.root, "需要你确认是否申请整机。")["checks"]
    assert not checks["zero_write_attempts"]


def test_model_only_request_does_not_pass_without_checking_both_groups(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-model-allocate")
    assert install(fixture)["installed"]
    run(fixture, "resources", group="备选加速卡群")
    result = run(
        fixture,
        "command",
        group="备选加速卡群",
        operation="lock",
        nodes=["node-20"],
        devices=["2"],
        duration="2h",
    )
    assert result["result"]["verified"]
    grade = evaluation.score(fixture.root, "已申请 node-20 的 2 号 P900")
    assert not grade["observable_pass"]
    assert not grade["checks"]["both_candidate_groups_examined"]
