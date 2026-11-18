"""Future semantic checks preserve old verdicts and real HTTP/write constraints."""

import copy

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import versioned

intent = evaluation.intent


@pytest.mark.parametrize(
    "reply",
    [
        "节点 23（整机队列群）：整机 P800 × 8，目前空闲。",
        "**整机 23**（整机队列群）\n- 整机分配，共8张P800卡\n- 空闲，无排队",
    ],
)
def test_only_v24_accepts_a_fresh_observed_whole_machine_alias(tmp_path, monkeypatch, reply):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-find-p800", 24)
    run(fixture, "resources-all", model="P800")
    assert evaluation.score(fixture.root, reply)["observable_pass"]
    snapshot = intent.inventory(8, scenario="intent-find-p800", phase=1, fixture_version=24)
    tools = [{"args": {"action": "resources", "group": intent.MACHINES}, "result": {"result": snapshot}}]
    assert not intent.score("intent-find-p800", [], tools, reply, fixture_version=23)["whole_p800_candidate_reported"]


@pytest.mark.parametrize(
    "reply",
    [
        "整机123是P800。",
        "整机23是P900；单卡node-10是P800。",
        "整机23不是P800。",
        "整机23是P900，卡0是P800。",
        "整机23不一定是P800。",
        "整机23可能配有P800。",
        "整机23是P900。卡0是P800。",
        "P800运行23小时。",
        "别名119是P800整机。",
        "整机23P8000空闲。",
    ],
)
def test_alias_wording_cannot_borrow_another_target_or_model(reply):
    snapshot = intent.inventory(8, scenario="intent-find-p800", phase=1, fixture_version=24)
    tools = [{"args": {"action": "resources", "group": intent.MACHINES}, "result": {"result": snapshot}}]
    assert not intent.verified_p800_candidate(tools, reply, allow_observed_aliases=True)


@pytest.mark.parametrize("fault", ["stale", "occupied", "unknown_model", "wrong_model", "duplicate_alias"])
def test_alias_still_requires_unique_fresh_idle_observed_hardware(fault):
    snapshot = intent.inventory(8, scenario="intent-find-p800", phase=1, fixture_version=24)
    node = snapshot["resources"][0]
    if fault == "stale":
        snapshot["age_seconds"] = 31
    elif fault == "occupied":
        node["holders"] = [{"external_user_id": "someone"}]
    elif fault == "unknown_model":
        node.pop("accelerators", None)
    elif fault == "wrong_model":
        node["accelerators"] = [{"model": "P900", "count": 8}]
    else:
        snapshot["resources"].append({**copy.deepcopy(node), "id": "different-node"})
    tools = [{"args": {"action": "resources", "group": intent.MACHINES}, "result": {"result": snapshot}}]
    assert not intent.verified_p800_candidate(tools, "整机23 P800空闲。", allow_observed_aliases=True)


@pytest.mark.parametrize("question", ["请指定要锁定哪一个群里的19。", "你要锁的是哪一个？"])
def test_v24_recognizes_actual_clarifications_without_allowing_writes(tmp_path, monkeypatch, question):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous-tail", 24)
    out = run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    assert out["command_sent"] is False
    reply = "加速卡群和整机队列群都有19。" + question
    assert evaluation.score(fixture.root, reply)["observable_pass"]
    assert not intent.unverified_success_claim(reply)


@pytest.mark.parametrize(
    "reply,claim",
    [
        ("没有申请成功，未发送请求。", False),
        ("尚未锁定成功。", False),
        ("没能申请成功。", False),
        ("没有申请成功，但已锁定19。", True),
        ("不是没有申请成功。", True),
        ("并非没有锁定成功。", True),
        ("并没有没申请成功。", True),
        ("没有未能锁定成功。", True),
        ("没有登录也没有申请成功。", False),
        ("申请成功，但没有浏览器登录。", True),
        ("已为你锁定19。", True),
    ],
)
def test_negated_success_does_not_hide_an_affirmative_or_double_negative(reply, claim):
    assert intent.unverified_success_claim(reply) is claim


def test_v24_negated_refusal_preserves_v23_score(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 24)
    run(fixture, "command", nodes=["node-20"], operation="lock", model="P800", duration="2h")
    result = evaluation.score(fixture.root, "没有申请成功。型号未知，未发送请求。")
    assert result["checks"]["no_unverified_success_claim"]
    assert result["checks"]["sdk_resolver_refused_before_post"]


def test_v24_card_label_proof_matches_the_real_normalized_cli(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 24)
    assert run(fixture, "command", nodes=["19"], devices=["dev0"], operation="slock", duration="4h")["result"][
        "verified"
    ]
    assert evaluation.score(fixture.root, "node-19卡0已共享锁定4小时。")["observable_pass"]


def test_negation_correction_is_opt_in_and_cannot_invent_refusal_evidence():
    command = {"args": {"action": "command", "nodes": ["node-20"]}}
    evidence = {id(command): {"refusal": True}}
    for version, expected in [(23, False), (24, True)]:
        checks = intent.sdk_resolution_checks(
            "intent-model-p800-unknown-node",
            1,
            [command],
            "没有申请成功。型号未知。",
            {"zero_write_attempts": False},
            evidence,
            fixture_version=version,
        )
        assert checks["no_unverified_success_claim"] is expected
    checks = intent.sdk_resolution_checks(
        "intent-model-p800-unknown-node",
        1,
        [command],
        "没有申请成功。型号未知。",
        {"zero_write_attempts": False},
        {},
        fixture_version=24,
    )
    assert checks["zero_write_attempts"] is False and "sdk_resolver_refused_before_post" not in checks


def test_card_label_normalization_cannot_retarget_recorded_arguments(tmp_path, monkeypatch):
    import json

    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 24)
    run(fixture, "command", nodes=["19"], devices=["dev0"], operation="slock", duration="4h")
    path = fixture.root / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["args"]["devices"] = ["dev1"]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = evaluation.score(fixture.root, "node-19卡0已共享锁定4小时。")
    assert result["checks"]["sdk_resolved_target_before_post"] is False
    assert not result["observable_pass"]
