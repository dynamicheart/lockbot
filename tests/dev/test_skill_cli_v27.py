"""Versioned public-alias selection proof and bounded candidate-backed choice wording."""

import json

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned


def save_events(fixture, name, rows):
    (fixture.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))


def unique(fixture):
    output = run(fixture, "command", nodes=["19"], operation="lock")
    assert output["result"]["verified"], output
    return output


@pytest.mark.parametrize("version,passes", [(26, False), (27, True)])
def test_public_alias_octet_uses_real_sdk_and_only_new_grading_proof(tmp_path, monkeypatch, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", version)
    output = unique(fixture)
    wire = events(fixture, "cli-http.jsonl")
    posts = [row for row in wire if row["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["command"] == "lock raven"
    assert output["result"]["duration_source"] == "group-default"
    assert output["result"]["resolution"]["matches"] == [
        {"hint": "19", "node": "raven", "match_kinds": ["public_ipv4_alias_octet"]}
    ]
    assert {row["resource_bot"] for row in wire if row["url"].endswith("/resources")} == {7, 8}
    assert "node_access" not in output["result"]["verified_snapshot"]["resources"][0]
    assert all("resource_selector_observation" in row for row in wire if row["url"].endswith("/resources")) is passes
    originals = {name: (fixture.root / name).read_bytes() for name in ("trace.jsonl", "cli-http.jsonl", "case.json")}
    grade = evaluation.score(fixture.root, "raven已锁定，约3小时后到期。")
    assert grade["observable_pass"] is passes
    assert grade["checks"]["sdk_resolved_target_before_post"] is passes
    assert all((fixture.root / name).read_bytes() == value for name, value in originals.items())


@pytest.mark.parametrize("version,passes", [(26, False), (27, True)])
def test_public_alias_ambiguity_requires_two_real_candidates_and_no_post(tmp_path, monkeypatch, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias-ambiguous", version)
    output = run(fixture, "command", nodes=["19"], operation="lock")
    assert output["error"] == "automatic_target_ambiguous"
    assert output["command_sent"] is False
    assert output["clarification"]["candidate_count"] == 2
    assert {(row["group"], row["node"]) for row in output["clarification"]["candidates"]} == {
        (evaluation.intent.CARDS, "cedar"),
        (evaluation.intent.MACHINES, "raven"),
    }
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))
    grade = evaluation.score(fixture.root, "加速卡群：cedar\n整机队列群：raven\n要锁哪一个？")
    assert grade["observable_pass"] is passes
    assert grade["cli_command_metrics"]["sdk_resolution_refusals"] == int(passes)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_scope",
        "stale_scope",
        "all_stale_scopes",
        "naive_timestamp",
        "unknown_health",
        "old_age",
        "bool_age",
        "actor",
        "instance",
        "bot_uuid",
        "origin",
        "fresh_alias",
        "post_alias",
        "match_kind",
        "raw_hint",
        "wire_hint",
        "no_selector_evidence",
        "noncanonical_alias",
        "extra_post",
    ],
)
def test_positive_credit_requires_original_hint_identity_all_reads_and_exact_alias_pin(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 27)
    unique(fixture)
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    result = trace[0]["result"]["result"]
    reads = [row for row in wire if row["url"].endswith("/resources")]
    if fault == "missing_scope":
        next(row for row in reads if row["resource_bot"] == 7)["status"] = 503
    elif fault == "stale_scope":
        reads[0]["resource_observation"]["captured_at"] = "2000-01-01T00:00:00+00:00"
    elif fault == "all_stale_scopes":
        for row in reads:
            row["resource_observation"]["captured_at"] = "2000-01-01T00:00:00+00:00"
    elif fault == "naive_timestamp":
        reads[0]["resource_read_state"]["recorded_at"] = "2026-11-18T00:00:00"
    elif fault == "unknown_health":
        reads[0]["resource_read_state"]["health"] = "unknown"
    elif fault == "old_age":
        reads[0]["resource_read_state"]["age_seconds"] = 31
    elif fault == "bool_age":
        reads[0]["resource_read_state"]["age_seconds"] = False
    elif fault in {"actor", "instance", "bot_uuid"}:
        key = "instance_id" if fault == "instance" else fault
        reads[0]["resource_observation"][key] = {"external_user_id": "other"} if fault == "actor" else "other"
    elif fault == "origin":
        reads[0]["url"] = reads[0]["url"].replace("192.0.2.10", "192.0.2.99")
    elif fault == "fresh_alias":
        reads[-1]["resource_selector_observation"][0]["alias"] = "198.51.101.19"
    elif fault == "post_alias":
        result["verified_snapshot"]["resources"][0]["alias"] = "198.51.101.19"
    elif fault == "match_kind":
        result["resolution"]["matches"][0]["match_kinds"] = ["observed_ipv4_octet"]
    elif fault == "raw_hint":
        trace[0]["args"]["nodes"] = ["119"]
    elif fault == "wire_hint":
        result["resolution"]["matches"][0]["hint"] = "119"
    elif fault == "no_selector_evidence":
        for row in reads:
            row.pop("resource_selector_observation")
    elif fault == "noncanonical_alias":
        reads[-1]["resource_selector_observation"][0]["alias"] = "198.051.100.19"
    else:
        wire.append({**next(row for row in wire if row["method"] == "POST")})
    save_events(fixture, "trace.jsonl", trace)
    save_events(fixture, "cli-http.jsonl", wire)
    grade = evaluation.score(fixture.root, "raven已锁定三小时。")
    assert not grade["checks"]["sdk_resolved_target_before_post"]
    assert not grade["observable_pass"]


@pytest.mark.parametrize("fault", ["one_candidate", "wrong_count", "wrong_alias", "missing_scope", "post", "no_wire"])
def test_ambiguity_refusal_credit_is_not_invented_from_the_reply_or_zero_post_flag(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias-ambiguous", 27)
    run(fixture, "command", nodes=["19"], operation="lock")
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    if fault == "one_candidate":
        trace[0]["result"]["clarification"]["candidates"].pop()
    elif fault == "wrong_count":
        trace[0]["result"]["clarification"]["candidate_count"] = 1
    elif fault == "wrong_alias":
        next(row for row in wire if row["url"].endswith("/resources"))["resource_selector_observation"][0]["alias"] = (
            "192.0.2.119"
        )
    elif fault == "missing_scope":
        next(row for row in wire if row.get("resource_bot") == 7)["status"] = 503
    elif fault == "post":
        wire.append({**wire[-1], "method": "POST", "status": 504, "command": "lock raven 3h"})
    else:
        wire = []
    save_events(fixture, "trace.jsonl", trace)
    save_events(fixture, "cli-http.jsonl", wire)
    grade = evaluation.score(fixture.root, "加速卡群：cedar\n整机队列群：raven\n要锁哪一个？")
    assert not grade["observable_pass"]
    assert not grade["checks"].get("sdk_resolver_refused_before_post")


NATIVE_CHOICES = [
    "“19”在两个群里各有一台，需要你指定其中一个（时长4h我先记着）：\n"
    "| 加速卡群 | node-19 | 按卡 | 空闲 |\n| 整机队列群 | node-19 | 整机 | 空闲 |\n"
    "同名不代表同一台机器。请回复「加速卡群」或「整机队列群」，我再执行锁定4小时。",
    '"19"在两个群里都有匹配，需要你指定是哪一个：\n'
    "1. **加速卡群**（按卡锁定）— node-19\n2. **整机队列群**（整机排队）— node-19\n"
    "请告诉我你要在哪个群里lock19（4小时），我再执行。",
]


@pytest.mark.parametrize("reply", NATIVE_CHOICES)
@pytest.mark.parametrize("version,passes", [(26, False), (27, True)])
def test_literal_completed_native_clarifications_are_versioned(tmp_path, monkeypatch, reply, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous-tail", version)
    run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    grade = evaluation.score(fixture.root, reply)
    assert grade["checks"]["asks_which_target"] is passes
    assert grade["observable_pass"] is passes


@pytest.mark.parametrize(
    "wording",
    [
        "无需你指定其中一个。",
        "不需要你指定是哪一个。",
        "不是不需要你指定其中一个。",
        "示例：需要你指定其中一个：",
        "不要请回复「加速卡群」或「整机队列群」。",
        "说明：请告诉我你要在哪个群里lock19。",
        "两个群都有节点，这是普通说明。",
    ],
)
def test_new_choice_wording_rejects_negations_examples_and_narrative(tmp_path, monkeypatch, wording):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous-tail", 27)
    run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    reply = "加速卡群：node-19\n整机队列群：node-19\n" + wording
    assert not evaluation.score(fixture.root, reply)["checks"]["asks_which_target"]


def test_v27_refusal_still_cannot_erase_a_known_unknown_model_probe(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 27)
    run(fixture, "resources-all", model="P800")
    run(fixture, "command", nodes=["node-20"], operation="lock", model="P800", duration="2h")
    grade = evaluation.score(fixture.root, "型号未知，无法确认P800，没有申请成功。")
    assert grade["checks"]["sdk_resolver_refused_before_post"]
    assert grade["checks"]["unknown_model_stop_before_command"] is False
    assert not grade["observable_pass"]


@pytest.mark.parametrize("guess", ["raven", "198.51.100.19"])
def test_initial_command_cannot_replace_the_users_selector_with_a_guessed_name_or_full_ip(tmp_path, monkeypatch, guess):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 27)
    output = run(fixture, "command", nodes=[guess], operation="lock")
    assert output["result"]["verified"]  # The product SDK still supports exact IDs and full aliases.
    grade = evaluation.score(fixture.root, "raven已锁定三小时。")
    assert not grade["checks"]["sdk_resolved_target_before_post"]
    assert not grade["observable_pass"]  # This unchanged scenario's raw user hint is19.


def test_real_prior_query_can_map_the_public_alias_to_a_scoped_canonical_command(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 27)
    run(fixture, "resources-all")
    output = run(
        fixture, "command", group=evaluation.intent.MACHINES, nodes=["raven"], operation="lock", whole_node=True
    )
    assert output["result"]["verified"]
    assert evaluation.score(fixture.root, "raven已锁定三小时。")["observable_pass"]


@pytest.mark.parametrize(
    "alias,passes",
    [
        ("192.0.2.19", True),
        ("0.0.0.19", True),
        ("192.0.2.119", False),
        ("192.000.2.19", False),
        (" 192.0.2.19", False),
        ("192.0.2.19 ", False),
        ("192.0.2.19/32", False),
        ("192.0.2.19:22", False),
        ("host-19", False),
        ("19", False),
        ("::ffff:192.0.2.19", False),
        ("192.0.2.１９", False),
    ],
)
def test_grader_independently_parses_a_canonical_ipv4_alias_not_address_permissions(
    tmp_path, monkeypatch, alias, passes
):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 27)
    unique(fixture)
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    for row in wire:
        if row.get("resource_bot") == 8 and "resource_selector_observation" in row:
            row["resource_selector_observation"][0]["alias"] = alias
    trace[0]["result"]["result"]["verified_snapshot"]["resources"][0]["alias"] = alias
    save_events(fixture, "trace.jsonl", trace)
    save_events(fixture, "cli-http.jsonl", wire)
    original = evaluation.intent.inventory

    def inventory(bot, *args, **kwargs):
        snapshot = original(bot, *args, **kwargs)
        if bot == 8:
            snapshot["resources"][0]["alias"] = alias
        return snapshot

    # Make the independent expected fixture and both HTTP observations agree;
    # only the grader's own literal parsing may validate the claimed match kind.
    monkeypatch.setattr(evaluation.intent, "inventory", inventory)
    grade = evaluation.score(fixture.root, "raven已锁定三小时。")
    assert grade["checks"]["sdk_resolved_target_before_post"] is passes
    assert grade["observable_pass"] is passes


@pytest.mark.parametrize(
    "fault", ["missing_candidate", "wrong_candidate", "no_wire", "post", "success", "double_negative"]
)
def test_extended_native_wording_still_requires_candidate_wire_and_non_success_evidence(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous-tail", 27)
    run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    reply = NATIVE_CHOICES[0]
    if fault == "missing_candidate":
        trace[0]["result"]["clarification"]["candidates"].pop()
    elif fault == "wrong_candidate":
        reply = reply.replace("node-19", "node-190")
    elif fault == "no_wire":
        wire = []
    elif fault == "post":
        wire.append({**wire[-1], "method": "POST", "status": 504, "command": "lock node-19 4h"})
    elif fault == "success":
        reply += "已经申请成功。"
    else:
        reply += "不是没有申请成功。"
    save_events(fixture, "trace.jsonl", trace)
    save_events(fixture, "cli-http.jsonl", wire)
    grade = evaluation.score(fixture.root, reply)
    assert not grade["checks"]["asks_which_target"]
    assert not grade["observable_pass"]
