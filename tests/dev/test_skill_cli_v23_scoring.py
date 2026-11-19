"""Current v34 SDK proofs retain the target-selection constraints introduced in v23."""

import copy
import json

import pytest
from dev.cli_legacy_grader_unit_data import legacy_intent_checks
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned


@pytest.mark.parametrize(
    "scenario,arguments,final",
    [
        ("intent-lock-tail", {"nodes": ["19"], "operation": "lock"}, "node-19已锁三小时。"),
        (
            "intent-shared-tail",
            {"nodes": ["19"], "devices": ["0"], "operation": "slock", "duration": "4h"},
            "node-19的0号卡已共享锁定4小时。",
        ),
        (
            "intent-shared-minimal",
            {"nodes": ["19"], "operation": "slock", "duration": "4h"},
            "node-19的0、1号卡已共享锁定4小时。",
        ),
        (
            "intent-lock-tail-list",
            {"nodes": ["19", "20", "21", "22"], "operation": "lock"},
            "node-19、node-20、node-21、node-22已锁三小时。",
        ),
        (
            "intent-exact",
            {
                "nodes": ["node-10"],
                "devices": ["2", "3"],
                "operation": "lock",
                "duration": "2h",
                "model": "P800",
                "device_count": 2,
            },
            "node-10的2、3号卡已锁两小时。",
        ),
        ("intent-public-ip-alias", {"nodes": ["19"], "operation": "lock"}, "raven已锁三小时。"),
    ],
)
def test_one_unscoped_sdk_call_owns_resolution_without_fabricating_agent_reads(
    tmp_path, monkeypatch, scenario, arguments, final
):
    _, fixture = versioned(tmp_path, monkeypatch, scenario, 34)
    output = run(fixture, "command", **arguments)
    assert output.get("result", {}).get("verified"), output
    original_tools = (fixture.root / "trace.jsonl").read_bytes()
    original_wire = (fixture.root / "cli-http.jsonl").read_bytes()
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade
    assert grade["checks"]["sdk_resolved_target_before_post"]
    assert "agent_read_inventory_before_target_selection" not in grade["checks"]
    assert grade["checks"]["exactly_one_correct_cli_write"] and grade["checks"]["verified_before_and_after"]
    assert len(events(fixture)) == 1
    assert grade["cli_command_metrics"]["sdk_resolved_writes"] == 1
    assert events(fixture)[0]["args"]["action"] == "command"
    assert sum(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl")) == 1
    assert (fixture.root / "trace.jsonl").read_bytes() == original_tools
    assert (fixture.root / "cli-http.jsonl").read_bytes() == original_wire


def test_legacy_v23_full_alias_positive_is_controlled_grader_data(tmp_path, monkeypatch):
    # The later literal-user-hint policy must not silently rewrite this old grader contract.
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 23)
    result = run(fixture, "command", nodes=["198.51.100.19"], operation="lock")["result"]
    assert result["verified"]
    checks = legacy_intent_checks(fixture, "raven已锁三小时。")
    assert checks["sdk_resolved_target_before_post"] and all(checks.values())


@pytest.mark.parametrize(
    "scenario,arguments,final,error",
    [
        (
            "intent-ambiguous",
            {"nodes": ["训练节点"], "operation": "lock", "duration": "2h"},
            "加速卡群和整机队列群都有训练节点，请确认目标机器？",
            "automatic_target_ambiguous",
        ),
        (
            "intent-ambiguous-tail",
            {"nodes": ["19"], "operation": "lock", "duration": "4h"},
            "加速卡群和整机队列群都匹配19，请确认目标机器？",
            "automatic_target_ambiguous",
        ),
        (
            "intent-ip-tail-unavailable",
            {"nodes": ["19"], "operation": "lock", "duration": "4h"},
            "没有找到19，请提供节点名或别名。",
            "automatic_target_missing",
        ),
        (
            "intent-model-unknown-node",
            {"nodes": ["node-20"], "operation": "lock", "duration": "2h", "model": "P900"},
            "node-20型号未知，无法确认P900，请管理员补充型号，未申请。",
            "selected_resource_model_unverified_or_insufficient",
        ),
    ],
)
def test_authorized_negative_request_accepts_one_proven_sdk_refusal(
    tmp_path, monkeypatch, scenario, arguments, final, error
):
    _, fixture = versioned(tmp_path, monkeypatch, scenario, 34)
    output = run(fixture, "command", **arguments)
    assert output.get("error") == error, output
    assert output["command_sent"] is False and output["retry_mutation"] is False
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade
    assert grade["checks"]["sdk_resolver_refused_before_post"]
    assert grade["cli_command_metrics"]["tool_attempts"] == 1
    assert grade["cli_command_metrics"]["mutation_attempts"] == 1
    assert grade["cli_command_metrics"]["resource_posts"] == 0
    assert grade["cli_command_metrics"]["sdk_resolution_refusals"] == 1
    assert (
        "zero_write_attempts" not in grade["checks"]
    )  # A real CLI call occurred; dispatch is separately proved absent.
    assert len(events(fixture)) == 1
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))
    assert not evaluation.score(fixture.root, final + " 已申请成功。 ")["observable_pass"]


@pytest.mark.parametrize("version", [18, 19, 20, 21, 22])
def test_legacy_grading_does_not_accept_new_resolution_as_an_agent_read(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 23)
    assert run(fixture, "command", nodes=["19"], devices=["0"], operation="slock", duration="4h")["result"]["verified"]
    fixture.case["fixture_version"] = version
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    grade = evaluation.score(fixture.root, "node-19的0号卡已共享锁定4小时。")
    assert not grade["observable_pass"]
    assert not grade["checks"]["agent_read_inventory_before_target_selection"]
    assert "sdk_resolved_target_before_post" not in grade["checks"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_span",
        "outside_span",
        "missing_group_read",
        "wrong_read_actor",
        "wrong_read_uuid",
        "wrong_origin",
        "wrong_selected_actor",
        "wrong_selected_bot",
        "wrong_hint",
        "wrong_match_kind",
        "wrong_device_set",
        "wrong_selected_boundary",
        "missing_fresh_selected_read",
        "wrong_verified_actor",
        "wrong_command_body",
        "duplicate_command",
        "extra_post",
        "missing_resolution",
        "argv_scoped",
        "wrong_operation_arguments",
        "wrong_duration_arguments",
        "wrong_count_arguments",
        "wrong_model_arguments",
        "whole_node_on_cards",
        "post_without_auth",
        "verified_other_holder",
        "verified_hardware_changed",
        "unhealthy_snapshot",
        "stale_snapshot",
        "wrong_before_post_count",
    ],
)
def test_resolution_metadata_never_overrides_missing_or_conflicting_actual_evidence(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 34)
    assert run(fixture, "command", nodes=["19"], devices=["0"], operation="slock", duration="4h")["result"]["verified"]
    assert evaluation.score(fixture.root, "node-19的0号卡已共享锁定4小时。")["observable_pass"]
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    call, result = trace[0], trace[0]["result"]["result"]
    resolution = result["resolution"]
    reads = [index for index, row in enumerate(wire) if row.get("resource_observation") and row["method"] == "GET"]
    if fault == "missing_span":
        call.pop("http_request_span")
    elif fault == "outside_span":
        call["http_request_span"] = [0, len(wire) + 1]
    elif fault == "missing_group_read":
        wire[reads[1]]["status"] = 403
    elif fault in {"wrong_read_actor", "wrong_read_uuid"}:
        wire[reads[1]]["resource_observation"]["actor" if fault.endswith("actor") else "bot_uuid"] = "other"
    elif fault in {"wrong_origin", "wrong_selected_actor", "wrong_selected_bot"}:
        scope = resolution["selected_scope"]
        if fault == "wrong_origin":
            scope["origin"] = "http://unapproved.invalid"
        elif fault == "wrong_selected_actor":
            scope["actor"] = {"platform": "infoflow", "external_user_id": "other"}
        else:
            scope["bot"]["id"] = 8
    elif fault in {"wrong_hint", "wrong_match_kind"}:
        resolution["matches"][0]["hint" if fault == "wrong_hint" else "match_kinds"] = (
            "node-20" if fault == "wrong_hint" else ["observed_ipv4_octet"]
        )
    elif fault == "wrong_device_set":
        resolution["resolved_devices"] = ["1"]
    elif fault == "wrong_selected_boundary":
        resolution["http_requests_before_selected_read"] = 1
    elif fault == "missing_fresh_selected_read":
        wire[reads[-1]]["status"] = 403
    elif fault == "wrong_verified_actor":
        result["verified_snapshot"]["context"]["actor"]["external_user_id"] = "other"
    elif fault == "wrong_command_body":
        next(row for row in wire if row["method"] == "POST")["command"] = "slock node-20 dev0 4h"
    elif fault == "duplicate_command":
        trace.append(copy.deepcopy(call))
    elif fault == "extra_post":
        wire.append(copy.deepcopy(next(row for row in wire if row["method"] == "POST")))
    elif fault == "missing_resolution":
        result.pop("resolution")
    elif fault in {"wrong_operation_arguments", "wrong_duration_arguments"}:
        key, value = ("operation", "lock") if fault == "wrong_operation_arguments" else ("duration", "1h")
        call["args"][key] = value
        call["argv"][call["argv"].index("--" + key) + 1] = value
    elif fault in {"wrong_count_arguments", "wrong_model_arguments"}:
        key, flag, value = (
            ("device_count", "--device-count", 2) if fault == "wrong_count_arguments" else ("model", "--model", "P900")
        )
        call["args"][key] = value
        call["argv"] += [flag, str(value)]
    elif fault == "whole_node_on_cards":
        call["args"]["whole_node"] = True
        call["argv"].append("--whole-node")
    elif fault == "post_without_auth":
        next(row for row in wire if row["method"] == "POST")["authorization_present"] = False
    elif fault in {"verified_other_holder", "verified_hardware_changed"}:
        card = result["verified_snapshot"]["resources"][0]["devices"][0]
        if fault == "verified_other_holder":
            card["holders"] = [{"external_user_id": "other"}]
        else:
            card["dev_model"] = "P900"
    elif fault == "unhealthy_snapshot":
        result["verified_snapshot"]["bot_service_health"] = "unknown"
    elif fault == "stale_snapshot":
        result["verified_snapshot"]["age_seconds"] = 6
    elif fault == "wrong_before_post_count":
        resolution["http_requests_before_post"] = 100
    else:
        call["argv"][1:1] = ["--connection", "private/group-7.json"]
    (fixture.root / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    (fixture.root / "cli-http.jsonl").write_text("".join(json.dumps(row) + "\n" for row in wire))
    grade = evaluation.score(fixture.root, "node-19的0号卡已共享锁定4小时。")
    assert not grade["observable_pass"], grade
    assert grade["checks"].get("sdk_resolved_target_before_post") is False


def test_race_is_safe_refusal_not_a_successful_resolved_write(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-lock-tail", 34)
    original, seen = fixture.http, []

    def race(args):
        response = original(args)
        body = response.get("body", {})
        if args["url"].endswith("/resources") and body.get("bot_id") == 8:
            seen.append(1)
            if len(seen) == 2:
                body["resources"][0]["alias"] = "changed-before-selected-read"
        return response

    monkeypatch.setattr(fixture, "http", race)
    output = run(fixture, "command", nodes=["19"], operation="lock")
    assert output.get("command_sent") is False, output
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))
    assert not evaluation.score(fixture.root, "node-19已锁三小时。")["observable_pass"]


def test_query_only_cases_still_reject_even_a_safe_sdk_command_refusal(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-find-p800", 34)
    output = run(fixture, "command", nodes=["unobserved"], operation="lock")
    assert output.get("command_sent") is False, output
    run(fixture, "resources-all")
    grade = evaluation.score(fixture.root, "node-23是空闲P800整机。")
    assert not grade["observable_pass"] and not grade["checks"]["zero_write_attempts"]


@pytest.mark.parametrize(
    "fault",
    [
        "sent_flag_missing",
        "sent_flag_true",
        "retry_true",
        "missing_question",
        "scope_error",
        "duplicate_call",
        "empty_span",
        "bad_hint",
        "sent_post",
        "boolean_candidate_count",
    ],
)
def test_refusal_flags_cannot_hide_dispatch_retries_scope_errors_or_missing_proof(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ip-tail-unavailable", 34)
    assert run(fixture, "command", nodes=["19"], operation="lock", duration="4h")["command_sent"] is False
    assert evaluation.score(fixture.root, "没有找到19，请提供节点名或别名。")["observable_pass"]
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    output = trace[0]["result"]
    if fault == "sent_flag_missing":
        output.pop("command_sent")
    elif fault == "sent_flag_true":
        output["command_sent"] = True
    elif fault == "retry_true":
        output["retry_mutation"] = True
    elif fault == "missing_question":
        output["clarification"] = {}
    elif fault == "scope_error":
        output["error"] = "automatic_scope_changed"
    elif fault == "duplicate_call":
        trace.append(copy.deepcopy(trace[0]))
    elif fault == "empty_span":
        trace[0]["http_request_span"] = [0, 0]
    elif fault == "bad_hint":
        trace[0]["args"]["nodes"] = ["cedar"]
    elif fault == "boolean_candidate_count":
        output["clarification"]["candidate_count"] = False
    else:
        wire.append(
            {
                "url": evaluation.base.ORIGIN + "/api/bots/8/commands",
                "method": "POST",
                "status": 503,
                "authorization_present": True,
                "command": "lock cedar 4h",
                "resource_bot": 8,
            }
        )
    (fixture.root / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    (fixture.root / "cli-http.jsonl").write_text("".join(json.dumps(row) + "\n" for row in wire))
    grade = evaluation.score(fixture.root, "没有找到19，请提供节点名或别名。")
    assert not grade["observable_pass"] and not grade["checks"].get("sdk_resolver_refused_before_post", False)


def test_sent_unknown_write_is_not_a_proven_zero_post_refusal(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-shared-tail", 34)
    original = fixture.http

    def lose_response(args):
        response = original(args)
        if args["method"] == "POST" and args["url"].endswith("/commands"):
            return {"status": 503, "body": {}}
        return response

    monkeypatch.setattr(fixture, "http", lose_response)
    output = run(fixture, "command", nodes=["19"], devices=["0"], operation="slock", duration="4h")
    assert output.get("command_sent") is not False
    assert output["error"] == "mutation_outcome_unknown_read_resources_do_not_replay"
    grade = evaluation.score(fixture.root, "结果未知，未能确认持有；不重试。")
    assert not grade["observable_pass"]
    assert grade["cli_command_metrics"]["resource_posts"] == 1
    assert grade["cli_command_metrics"]["sdk_resolution_refusals"] == 0


@pytest.mark.parametrize("fault", ["missing_nodes", "claimed_scope"])
def test_unscoped_metadata_tampering_cannot_fall_back_to_legacy_explicit_target_credit(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-exact", 34)
    assert run(fixture, "command", nodes=["node-10"], devices=["2", "3"], operation="lock", duration="2h")["result"][
        "verified"
    ]
    assert evaluation.score(fixture.root, "node-10的2、3号卡已锁两小时。")["observable_pass"]
    trace = events(fixture)
    if fault == "missing_nodes":
        trace[0]["args"].pop("nodes")
    else:
        trace[0]["args"]["group"] = evaluation.intent.CARDS
        trace[0]["args"]["connection"] = "private/group-7.json"
    (fixture.root / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    grade = evaluation.score(fixture.root, "node-10的2、3号卡已锁两小时。")
    assert not grade["observable_pass"]
    assert grade["checks"]["sdk_resolved_target_before_post"] is False
