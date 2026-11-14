"""Bounded virtual-clock cases; no paid models or installed client schedulers."""

import importlib.util
import sys
from pathlib import Path

import pytest

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
        evaluation.prepare(root, skill, scenario)
        return evaluation.Fixture(root)

    return create


def request(fixture, path, command=None):
    return fixture.call(
        "http_request",
        {
            "url": base.ORIGIN + path,
            "method": "POST" if command else "GET",
            "headers": {"Authorization": "Bearer " + base.KEY, "X-Lockbot-Client-Skill-Version": evaluation.PREVIOUS},
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
    assert notice["reported_version"] == evaluation.PREVIOUS
    assert notice["manifest_url"] == "/api/agent-skill"
    assert notice["skill_url"] == base.SKILL_PATH
    request(fixture, "/api/bots/7/resources")
    final = "请确认安装新版完整包：" + base.ORIGIN + base.BUNDLE_PATH
    assert evaluation.score(fixture.root, final)["observable_pass"]
    assert (fixture.workspace / "private/connection.json").exists()
    request(fixture, "/api/bots/7/commands", "free node-a")
    assert not evaluation.score(fixture.root, final)["checks"]["one_successful_release"]


def test_alias_case_disabled_until_real_snapshot_read_contract_exists(case):
    assert "alias" not in evaluation.PROMPTS
    fixture = case("queue")
    snapshot = request(fixture, "/api/bots/7/resources")["body"]
    assert "alias" not in snapshot["resources"][0]
    assert "alias" not in snapshot["supported_operations"]
    assert request(fixture, "/api/bots/7/commands", "alias node-a wrong")["status"] == 422
