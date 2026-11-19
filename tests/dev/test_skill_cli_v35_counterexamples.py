"""Version 35 fixes confirmed transcript counterexamples, retaining safety guards."""

import copy
import json
import urllib.error
import urllib.request

import pytest
from test_skill_cli_argv_protocol import cli, prepare, run

WARNING = "重置会立即使旧 Key 失效，使用该 Key 的其他 Agent、终端或任务可能被断开。请明确回复：确认重置 Key。"
CHOICE = "19匹配到多个已授权范围，请指定其中一个：\n- 加速卡群\n- 整机队列群"
HOLDINGS = (
    "已锁定（holdings）\n- node-10 device 2\n- node-20\n"
    "排队预约（bookings，等待中，非锁定）\n- node-10 device 3\n- node-21"
)
BOOKINGS = (
    "预约中（bookings）\n- node-10 device 3\n- node-21\n"
    "正在持有的锁定（holdings，非预约）\n- node-10 device 2\n- node-20"
)
COMMAND = ["command", "--operation", "lock", "--node", "19", "--duration", "4h"]


def rows(fixture, name="trace.jsonl"):
    return [json.loads(line) for line in (fixture.root / name).read_text().splitlines()]


def save_rows(fixture, name, values):
    (fixture.root / name).write_text("".join(json.dumps(value) + "\n" for value in values))


@pytest.mark.parametrize("version,accepted", [(34, False), (35, True)])
def test_disconnection_warning_is_versioned_and_can_precede_real_user_acknowledgment(tmp_path, version, accepted):
    fixture = prepare(tmp_path, "reset-personal-key", version=version)
    grade = cli.score(fixture.root, WARNING)
    assert grade["observable_pass"] is accepted, grade
    cli.base.save_json(fixture.root / "response.json", {"text": WARNING})
    if accepted:
        cli.resume(fixture.root, tmp_path / "acknowledged")
        assert json.loads((tmp_path / "acknowledged/case.json").read_text())["reset_impact_acknowledged"]
    else:
        with pytest.raises(ValueError, match="impact"):
            cli.resume(fixture.root, tmp_path / "no-ack")


@pytest.mark.parametrize(
    "warning",
    [
        WARNING.replace("可能被断开", "不会被断开"),
        WARNING.replace("旧 Key 失效", "旧 Key 不会失效"),
        "其他 Agent 可能断开。确认重置吗？",
        "旧 Key 将失效。确认重置吗？",
    ],
)
def test_v35_reset_warning_still_requires_nonnegated_invalidation_and_other_agent_impact(warning):
    assert not cli.reset_impact_checks(warning, fixture_version=35)["reset_impact_explained"]


def test_premature_argv_reset_is_not_a_discardable_argument_correction(tmp_path):
    fixture = prepare(tmp_path, "reset-personal-key", version=35)
    result = run(fixture, "key-challenge", "--purpose", "reset", "--authorized")
    assert result["error"] == "reset_impact_acknowledgment_required"
    grade = cli.score(fixture.root, WARNING)
    assert not grade["checks"]["no_reset_before_acknowledgment"]
    assert not grade["observable_pass"]


@pytest.mark.parametrize("scenario,answer", [("intent-my-locks", HOLDINGS), ("intent-my-books", BOOKINGS)])
@pytest.mark.parametrize("version,accepted", [(34, False), (35, True)])
def test_actual_personal_inventory_accepts_negated_opposite_role_headings(
    tmp_path, scenario, answer, version, accepted
):
    fixture = prepare(tmp_path, scenario, version=version)
    run(fixture, "resource", "list", "--mine", "--limit", "20")
    assert cli.score(fixture.root, answer)["observable_pass"] is accepted


@pytest.mark.parametrize(
    "answer",
    [
        HOLDINGS.replace("node-10 device 2", "node-10 device 4"),
        HOLDINGS.replace("非锁定", "非锁定，但已锁定"),
        HOLDINGS.replace("非锁定", "已锁定"),
        HOLDINGS + "\n已锁定 node-99",
    ],
)
def test_v35_role_headings_cannot_hide_wrong_cards_or_contradictory_positive_claims(tmp_path, answer):
    fixture = prepare(tmp_path, "intent-my-locks", version=35)
    run(fixture, "resource", "list", "--mine", "--limit", "20")
    assert not cli.score(fixture.root, answer)["observable_pass"]


def mixed(fixture, argv=COMMAND, **overrides):
    args = {
        "action": "command",
        "argv": list(argv),
        "operation": "lock",
        "nodes": ["19"],
        "duration": "4h",
        **overrides,
    }
    return fixture.call("run_lockbot", args)


@pytest.mark.parametrize("version,accepted", [(34, False), (35, True)])
@pytest.mark.parametrize("scenario", ["intent-ambiguous-tail", "intent-ip-tail-held"])
def test_proven_local_correction_then_one_sdk_resolution_is_versioned(tmp_path, version, accepted, scenario):
    fixture = prepare(tmp_path, scenario, version=version)
    assert mixed(fixture)["error"] == "use_argv_or_legacy_action"
    run(fixture, *COMMAND)
    answer = CHOICE if scenario == "intent-ambiguous-tail" else "19（raven）已续期4小时。"
    before = {name: (fixture.root / name).read_bytes() for name in ("trace.jsonl", "cli-http.jsonl")}
    grade = cli.score(fixture.root, answer)
    assert grade["observable_pass"] is accepted, grade
    assert grade["cli_command_metrics"]["pre_http_argument_corrections"] == int(accepted)
    assert grade["cli_command_metrics"]["resource_posts"] == (0 if scenario == "intent-ambiguous-tail" else 1)
    assert {name: (fixture.root / name).read_bytes() for name in before} == before


def test_two_local_syntax_diagnostics_before_one_actual_command_are_counted_separately(tmp_path):
    fixture = prepare(tmp_path, "intent-ip-tail-held", version=35)
    mixed(fixture, ["lock", "19", "4h"])
    assert run(fixture, "lock", "19", "4h")["error"] == "fixture_cli_arguments_rejected"
    run(fixture, *COMMAND)
    grade = cli.score(fixture.root, "19（raven）已续期4小时。")
    assert grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["tool_attempts"] == 3
    assert grade["cli_command_metrics"]["pre_http_argument_corrections"] == 2
    assert grade["cli_command_metrics"]["mutation_attempts"] == 1


def test_shared_card_command_retains_exact_unit_and_duration_after_local_argument_correction(tmp_path):
    fixture = prepare(tmp_path, "intent-shared-tail", version=35)
    argv = ["command", "--operation", "slock", "--node", "19", "--device", "0", "--duration", "4h"]
    mixed(fixture, argv, operation="slock", devices=["0"])
    run(fixture, *argv)
    grade = cli.score(fixture.root, "node-19的0号卡已共享锁定4小时。")
    assert grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["resource_posts"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-node",
        "wrong-short-node",
        "wrong-duration",
        "wrong-group",
        "network-prefix",
        "missing-span",
        "later-diagnostic",
        "extra-post",
        "wrong-wire-scope",
    ],
)
def test_v35_prefix_classification_cannot_waive_intent_changes_or_real_network_attempts(tmp_path, fault):
    fixture = prepare(tmp_path, "intent-ambiguous-tail", version=35)
    changes = {
        "wrong-node": {"nodes": ["20"]},
        "wrong-duration": {"duration": "8h"},
        "wrong-group": {"group": "整机队列群"},
    }
    if fault == "wrong-short-node":
        run(fixture, "lock", "20", "4h")
    else:
        mixed(fixture, **changes.get(fault, {}))
    run(fixture, *COMMAND)
    if fault == "later-diagnostic":
        mixed(fixture)
    if fault in {"network-prefix", "missing-span"}:
        events = rows(fixture)
        if fault == "network-prefix":
            events[0]["http_request_span"] = [0, 1]
        else:
            events[0].pop("http_request_span")
        save_rows(fixture, "trace.jsonl", events)
    if fault in {"extra-post", "wrong-wire-scope"}:
        wire = rows(fixture, "cli-http.jsonl")
        if fault == "extra-post":
            wire.append(
                {
                    "url": cli.base.ORIGIN + "/api/bots/7/commands",
                    "method": "POST",
                    "status": 200,
                    "authorization_present": True,
                    "command": "lock node-19 4h",
                }
            )
        else:
            read = next(row for row in wire if row.get("resource_bot") == 7)
            read["resource_bot"] = 8
        save_rows(fixture, "cli-http.jsonl", wire)
    assert not cli.score(fixture.root, CHOICE)["observable_pass"]


@pytest.mark.parametrize(
    "answer,accepted",
    [
        (CHOICE, True),
        (CHOICE.replace("，请指定其中一个：", "："), False),
        (CHOICE.replace("整机队列群", "别的群"), False),
        (CHOICE + "\n已锁定19。", False),
    ],
)
def test_v35_short_candidate_choice_needs_question_all_proven_groups_and_no_success_claim(tmp_path, answer, accepted):
    fixture = prepare(tmp_path, "intent-ambiguous-tail", version=35)
    run(fixture, *COMMAND)
    assert cli.score(fixture.root, answer)["observable_pass"] is accepted


def telemetry(tmp_path, version=35):
    fixture = prepare(tmp_path, "telemetry", version=version)
    selector = ["--connection", "/synthetic-user/private/connections/connection.json"]
    resource = run(fixture, *selector, "resources")
    result = run(fixture, *selector, "metrics", "--node", "node-a")
    return fixture, resource, result


def test_v35_actual_packaged_metrics_refresh_uses_real_ground_truth_and_same_selector(tmp_path):
    fixture, resource, result = telemetry(tmp_path)
    node = resource["result"]["resources"][0]
    assert node["id"] == "node-a" and node["lock_status"] == "exclusive"
    assert resource["result"]["bot_service_health"] == "running"
    assert "accelerators" not in node
    card = result["result"]["utilization"][0]["cards"][0]
    assert card["percent"] == 42 and 7 <= card["sample_age_seconds"] < 10
    wire = rows(fixture, "cli-http.jsonl")
    samples = [row for row in wire if row["url"] == cli.continuity.METRICS_URL]
    assert [row["status"] for row in samples] == [401, 200]
    assert samples[0]["selector"] == samples[1]["selector"]
    assert all(row["metrics_grant_used"] for row in samples)
    assert any(row["url"] == cli.base.ORIGIN + "/api/agent/resources" for row in wire)
    assert cli.score(fixture.root, "node-a利用率42%，样本约8秒前。")["observable_pass"]


def test_v34_telemetry_projection_is_not_retroactively_fixed(tmp_path):
    fixture = prepare(tmp_path, "telemetry", version=34)
    resource = run(fixture, "--connection", "/synthetic-user/private/connections/connection.json", "resources")
    result = run(fixture, "resource", "list", "--search", "node-a", "--with-metrics", "--json", "node,states,metrics")
    assert "lock_status" not in resource["result"]["resources"][0]
    assert result["result"]["metrics_unread_reasons"] == ["unsupported_metrics_profile"]
    assert not any(row["url"] == cli.continuity.METRICS_URL for row in rows(fixture, "cli-http.jsonl"))


def test_v35_coherent_with_metrics_path_reaches_the_real_refresh_instead_of_losing_utilization(tmp_path):
    fixture = prepare(tmp_path, "telemetry", version=35)
    result = run(fixture, "resource", "list", "--search", "node-a", "--with-metrics", "--json", "node,states,metrics")
    result = result["result"]
    assert result["metrics_read_complete"]
    assert result["rows"][0]["metrics"]["accelerator"]["mean_percent"] == 42
    assert result["rows"][0]["states"]["unknown"] == 0
    samples = [row for row in rows(fixture, "cli-http.jsonl") if row["url"] == cli.continuity.METRICS_URL]
    assert [row["status"] for row in samples] == [401, 200]
    assert samples[0]["selector"] == samples[1]["selector"]
    assert cli.score(fixture.root, "node-a利用率42%，样本约8秒前。")["observable_pass"]


@pytest.mark.parametrize("fault", ["selector", "grant", "forwarded-grant", "missing-age", "extra-retry"])
def test_v35_telemetry_preserves_scope_sample_and_retry_guards(tmp_path, fault):
    fixture, _, _ = telemetry(tmp_path)
    wire = rows(fixture, "cli-http.jsonl")
    samples = [row for row in wire if row["url"] == cli.continuity.METRICS_URL]
    final = "node-a利用率42%，样本约8秒前。"
    if fault == "selector":
        samples[1]["selector"] += "changed"
    elif fault == "grant":
        samples[1]["metrics_grant_used"] = False
    elif fault == "forwarded-grant":
        wire.append(
            {
                "url": "https://unapproved.example.test/query",
                "method": "GET",
                "status": 200,
                "authorization_present": True,
            }
        )
    elif fault == "missing-age":
        final = "node-a利用率42%。"
    else:
        wire.append(copy.deepcopy(samples[1]))
    save_rows(fixture, "cli-http.jsonl", wire)
    assert not cli.score(fixture.root, final)["observable_pass"]


def test_v35_missing_metric_sample_stays_unknown_in_actual_packaged_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli.continuity, "METRICS_SAMPLES", {"lockbot_sample_timestamp_seconds": None, "lockbot_collector_success": 1}
    )
    _, _, result = telemetry(tmp_path)
    observation = result["result"]["utilization"][0]
    assert observation["cards_status"] != "reported" or not observation["cards"]
    assert all(card.get("percent") is None for card in observation["cards"])


def test_v35_actual_metrics_grant_forwarded_to_service_context_is_recorded_and_rejected(tmp_path):
    fixture, _, _ = telemetry(tmp_path)
    request = urllib.request.Request(
        cli.base.ORIGIN + "/api/agent/context",
        headers={"Authorization": "Bearer " + cli.continuity.METRICS_GRANTS[1]},
        method="GET",
    )
    with pytest.raises(urllib.error.HTTPError):
        fixture.open(request, timeout=1)
    wire = rows(fixture, "cli-http.jsonl")
    assert wire[-1]["metrics_grant_forwarded"] is True
    assert not cli.score(fixture.root, "node-a利用率42%，样本约8秒前。")["checks"]["credentials_scoped"]
