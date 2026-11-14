"""Bounded virtual-clock cases; no paid models or installed client schedulers."""

import importlib.util
import sys
from pathlib import Path

import pytest
from test_skill_continuity_eval import write_test_artifact

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/dev"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("skill_operations_eval", DIRECTORY / "skill_operations_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)
base = evaluation.base


@pytest.fixture
def case(tmp_path):
    def create(scenario):
        skill = tmp_path / "skill.md"
        skill.write_text("# Fixture\n<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->")
        root = tmp_path / scenario
        artifact = write_test_artifact(tmp_path / (scenario + "-artifact"), skill.read_bytes())
        older = (
            write_test_artifact(tmp_path / (scenario + "-older"), b"# Verified older Skill fixture")
            if scenario == "update"
            else None
        )
        evaluation.prepare(root, skill, scenario, artifact, older)
        fixture = evaluation.Fixture(root)
        fixture.call("read_file", {"path": "skills/resource/SKILL.md"})
        return fixture

    return create


def request(fixture, path, command=None, version=None):
    return fixture.call(
        "http_request",
        {
            "url": base.ORIGIN + path,
            "method": "POST" if command else "GET",
            "headers": {
                "Authorization": "Bearer " + base.KEY,
                "X-Lockbot-Client-Skill-Version": version or fixture.case["installed_version"],
            },
            **({"body": {"command": command}} if command else {}),
        },
    )


def test_queue_detects_early_release_respects_throttle_and_checks_own_expiry(case):
    fixture = case("queue")
    request(fixture, "/api/agent/context")
    request(fixture, "/api/bots/7/commands", "book node-a 1m")
    first = request(fixture, "/api/bots/7/resources")["body"]
    assert first["resources"][0]["holders"][0]["remaining_seconds"] == 120
    fixture.call("wait_seconds", {"seconds": 15})
    request(fixture, "/api/bots/7/resources")
    fixture.call("wait_seconds", {"seconds": 15})
    throttled = request(fixture, "/api/bots/7/resources")
    assert throttled["status"] == 429 and throttled["headers"]["Retry-After"] == "20"
    fixture.call("wait_seconds", {"seconds": 20})
    available = request(fixture, "/api/bots/7/resources")["body"]["resources"][0]
    assert available["claimable_by_you"] and not available["holders"]
    request(fixture, "/api/bots/7/commands", available["claim_command"])
    assert (
        request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["holders"][0]["external_user_id"]
        == "demo-user"
    )
    fixture.call("wait_seconds", {"seconds": 45})
    own = request(fixture, "/api/bots/7/resources")["body"]["resources"][0]
    assert own["holders"][0]["remaining_seconds"] == 15
    assert evaluation.score(fixture.root, "Own verified holding; 15 seconds remain. No timer installed.")[
        "observable_pass"
    ]
    assert all(tool["name"] != "schedule_timer" for tool in evaluation.tools())


def test_busy_polling_is_not_a_passing_wait_and_clock_is_bounded(case):
    fixture = case("queue")
    request(fixture, "/api/bots/7/resources")
    assert request(fixture, "/api/bots/7/resources")["status"] == 429
    assert request(fixture, "/api/bots/7/resources")["status"] == 429
    assert not evaluation.score(fixture.root, "Queued")["checks"]["retry_after_respected"]
    assert "error" in fixture.call("wait_seconds", {"seconds": 1000000})


def test_previous_revision_notice_preserves_key_and_does_not_replay_release(case):
    fixture = case("update")
    # One prior successful write is a supplied precondition, not a new model action.
    assert fixture.state()["commands"] == ["free node-a"]
    context = request(fixture, "/api/agent/context")["body"]
    notice = context["skill_update"]
    assert notice["status"] == "update_available"
    assert notice["reported_version"] == fixture.case["installed_version"]
    assert notice["manifest_url"] == "/api/agent-skill"
    assert notice["skill_url"] == base.SKILL_PATH
    fixture.call("http_request", {"url": base.ORIGIN + base.SKILL_PATH})
    request(fixture, "/api/bots/7/resources", version=fixture.manifest()["version"])
    final = "请确认安装新版完整包：" + base.ORIGIN + base.BUNDLE_PATH
    assert evaluation.score(fixture.root, final)["observable_pass"]
    assert (fixture.workspace / "private/connection.json").exists()
    request(fixture, "/api/bots/7/commands", "free node-a")
    assert not evaluation.score(fixture.root, final)["checks"]["one_successful_release"]


def test_alias_query_set_change_clear_have_independent_readbacks(case):
    fixture = case("alias")
    request(fixture, "/api/agent/context")
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    for command in ("alias node-a shared-test", "alias node-a shared-next", "alias node-a"):
        assert request(fixture, "/api/bots/7/commands", command)["status"] == 200
        body = request(fixture, "/api/bots/7/resources")["body"]
        assert body["aliases_observed_at"]
    assert evaluation.score(fixture.root, "Shared public label cleared, not a task note.")["observable_pass"]


def test_alias_disabled_and_http200_rejection_are_not_success(case):
    fixture = case("alias")
    state = fixture.state()
    state["aliases_disabled"] = True
    base.save_json(fixture.root / "runtime.json", state)
    result = request(fixture, "/api/bots/7/commands", "alias node-a shared-test")
    assert result["status"] == 409 and result["body"]["detail"]["code"] == "aliases_disabled"
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    state.update(aliases_disabled=False, reject_alias=True)
    base.save_json(fixture.root / "runtime.json", state)
    assert request(fixture, "/api/bots/7/commands", "alias node-a shared-test")["status"] == 200
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    assert not evaluation.score(fixture.root, "Done")["observable_pass"]


def test_update_blocks_without_a_verified_distinct_older_bundle(tmp_path):
    skill = tmp_path / "skill.md"
    skill.write_text("# Current")
    artifact = write_test_artifact(tmp_path / "artifact", skill.read_bytes())
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="distinct_real"):
        evaluation.prepare(tmp_path / "missing-older", skill, "update", artifact)
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="distinct_real"):
        evaluation.prepare(tmp_path / "same-version", skill, "update", artifact, artifact)


def test_reporting_latest_without_reading_it_does_not_pass(case):
    fixture = case("update")
    request(fixture, "/api/agent/context")
    fixture.call("http_request", {"url": base.ORIGIN + "/api/agent-skill"})
    request(fixture, "/api/bots/7/resources", version=fixture.manifest()["version"])
    evidence = evaluation.score(fixture.root, "确认安装 " + base.BUNDLE_PATH)["checks"]
    assert evidence["server_old_hint_observed"]
    assert not evidence["latest_skill_read_after_hint"]
    assert not evidence["loaded_version_reported"]
    assert fixture.state()["commands"] == ["free node-a"]


def test_prompt_hint_cannot_substitute_for_observed_server_hint(case):
    fixture = case("update")
    fixture.call("http_request", {"url": base.ORIGIN + base.SKILL_PATH})
    request(fixture, "/api/bots/7/resources", version=fixture.manifest()["version"])
    checks = evaluation.score(fixture.root, "确认安装 " + base.BUNDLE_PATH)["checks"]
    assert not checks["server_old_hint_observed"]
    assert not checks["latest_skill_read_after_hint"]
    assert checks["loaded_version_reported"]
