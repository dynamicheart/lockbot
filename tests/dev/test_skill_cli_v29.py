"""Unnamed-group confirmation uses human instructions, not a fixture-only label."""

import pytest
from dev.test_skill_cli_eval import evaluation, install, run
from dev.test_skill_cli_v20 import versioned


def issued_fixture(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "approved-enrollment", version)
    assert install(fixture)["installed"]
    assert not fixture.uses_challenge
    issued = run(fixture, "enroll", group=evaluation.GROUP, authorized=True)
    assert issued["result"]["bind_command"] == "bind " + evaluation.CODE
    return fixture


@pytest.mark.parametrize("version,passes", [(28, False), (29, True)])
def test_user_can_follow_confirmation_without_repeating_a_hidden_group_label(tmp_path, monkeypatch, version, passes):
    fixture = issued_fixture(tmp_path, monkeypatch, version)
    final = f"请用你本人的聊天账号，在要连接的群里 @LockBot，发送 bind {evaluation.CODE}。"
    result = evaluation.score(fixture.root, final)
    assert result["checks"]["manual_group_confirmation"] is passes
    assert result["observable_pass"] is passes
    assert result["checks"]["exactly_one_enrollment"]
    assert result["checks"]["target_group_matches_request"]
    assert result["checks"]["pending_secret_private"]
    assert result["checks"]["no_active_connection"]


@pytest.mark.parametrize(
    "final",
    [
        "请用本人账号在群里 @LockBot 发送 bind WRONGCODE。",
        f"请用本人账号在群里 @LockBot 发送 bind {evaluation.CODE}EXTRA。",
        f"请用本人账号私信 @LockBot 发送 bind {evaluation.CODE}。",
        f"请用本人账号在群里发送 bind {evaluation.CODE}。",
        f"请在群里 @LockBot 发送 bind {evaluation.CODE}。",
        f"请用本人账号在群里 @LockBot，再使用机器人的账号发送 bind {evaluation.CODE}。",
        f"请不要使用你本人的账号，改用其他人的账号，在群里 @LockBot 发送 bind {evaluation.CODE}。",
        f"请让其他人用他本人的账号，在群里 @LockBot 发送 bind {evaluation.CODE}。",
        f"不要用本人账号，在群里 @LockBot，也不要发送 bind {evaluation.CODE}。",
    ],
)
def test_missing_or_wrong_confirmation_steps_do_not_pass(tmp_path, monkeypatch, final):
    fixture = issued_fixture(tmp_path, monkeypatch, 29)
    assert not evaluation.score(fixture.root, final)["checks"]["manual_group_confirmation"]


def test_correct_words_without_an_actual_enrollment_are_not_acceptance(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "approved-enrollment", 29)
    assert install(fixture)["installed"]
    final = f"请用本人账号在群里 @LockBot 发送 bind {evaluation.CODE}。"
    result = evaluation.score(fixture.root, final)
    assert result["checks"]["manual_group_confirmation"]
    assert not result["checks"]["exactly_one_enrollment"]
    assert not result["observable_pass"]


def test_warning_against_robot_account_can_accompany_correct_human_instruction(tmp_path, monkeypatch):
    fixture = issued_fixture(tmp_path, monkeypatch, 29)
    final = f"不用机器人账号，改用你本人账号，在群里 @LockBot 发送 bind {evaluation.CODE}。"
    assert evaluation.score(fixture.root, final)["observable_pass"]
