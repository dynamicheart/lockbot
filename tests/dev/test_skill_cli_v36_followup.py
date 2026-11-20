"""Independent-review counterexamples for v36 composed with the E SDK."""

import pytest
from test_skill_cli_argv_protocol import cli, prepare, run
from test_skill_cli_v35_counterexamples import rows, save_rows, telemetry


@pytest.mark.parametrize(
    "negative",
    [
        "不要（重要！）使用你本人的聊天账号",
        "不要(重要!)使用你本人的聊天账号",
        "不要（重要！（再次！））用本人账号",
        "（不要！）使用你本人的聊天账号",
        "请（不要使用本人账号！）发送",
        "请（不要（注意。务必！）用本人账号！）发送",
        "不要（注意。务必！）用本人账号",
    ],
)
def test_parenthetical_negations_remain_negative(negative):
    assert cli.enrollment_instruction_negated(negative, fixture_version=36)
    assert not cli.enrollment_instruction_negated("不能读取资源。请用你本人的聊天账号", fixture_version=36)


@pytest.mark.parametrize("generations", [[False, True], [0.0, 1.0], [0, True], [False, 1], [0, 1.0]])
def test_grant_generations_require_exact_integers(tmp_path, generations):
    fixture, _, _ = telemetry(tmp_path, version=36)
    wire = rows(fixture, "cli-http.jsonl")
    events = rows(fixture)
    assert cli.telemetry_invocation_retry(wire, events)
    samples = [row for row in wire if row["url"] == cli.continuity.METRICS_URL]
    for sample, value in zip(samples, generations, strict=True):
        sample["metrics_grant_generation"] = value
    assert not cli.telemetry_invocation_retry(wire, events)


def claim_sequence(tmp_path, version=36, target="20"):
    fixture = prepare(tmp_path, "intent-claim", version=version)
    diagnostic = run(fixture, "command", "--operation", "lock", "--node", target)
    assert diagnostic["error"] == "booking_claimable_use_explicit_claim"
    assert diagnostic["command_sent"] is False
    assert not [row for row in rows(fixture, "cli-http.jsonl") if row["method"] == "POST"]
    claim = run(fixture, "command", "--operation", "claim", "--node", target)
    assert claim["result"]["verified"] is True
    return fixture


@pytest.mark.parametrize("version,accepted", [(35, False), (36, True)])
def test_positive_claim_diagnostic_is_versioned_and_wire_bound(tmp_path, version, accepted):
    fixture = claim_sequence(tmp_path, version=version, target="node-20" if version == 35 else "20")
    grade = cli.score(fixture.root, "已领取 node-20，沿用预约时长2小时。")
    assert grade["observable_pass"] is accepted, grade
    assert grade["cli_command_metrics"]["tool_attempts"] == 2
    assert grade["cli_command_metrics"]["resource_posts"] == 1
    assert grade["cli_command_metrics"]["mutation_attempts"] == (1 if accepted else 2)
    if accepted:
        assert grade["cli_command_metrics"]["read_only_claim_diagnostics"] == 1
        assert grade["cli_command_metrics"]["sdk_resolved_writes"] == 1
        tools = cli.argv_host.for_grading(rows(fixture), fixture.case)
        wire = rows(fixture, "cli-http.jsonl")
        for row in wire:
            if row.get("resource_bot") is not None:
                row["url"] = row["url"].replace("/api/agent/", f"/api/bots/{row['resource_bot']}/")
        assert any(proof["positive"] for proof in cli.verified_sdk_resolutions(fixture, tools, wire).values())


@pytest.mark.parametrize(
    "fault",
    [
        "target",
        "mode",
        "model",
        "device",
        "group",
        "duration",
        "selector",
        "unowned",
        "not-claimable",
        "stale",
        "unknown-age",
        "unhealthy",
        "wrong-actor",
        "missing-snapshot",
        "uncertain",
        "wrong-error",
        "retry",
        "diagnostic-post",
        "unverified",
        "uncertain-write",
        "post-failed",
        "wrong-post",
        "missing-span",
        "overlap",
        "boolean-span",
        "extra-probe",
    ],
)
def test_claim_diagnostic_never_waives_changed_criteria_or_unproven_observations(tmp_path, fault):
    fixture = claim_sequence(tmp_path)
    answer = "已领取 node-20，沿用预约时长2小时。"
    assert cli.score(fixture.root, answer)["observable_pass"]
    events, wire = rows(fixture), rows(fixture, "cli-http.jsonl")
    commands = [row for row in events if row["tool"] == "run_lockbot"]
    diagnostic, claim = commands
    if fault in {"target", "mode", "model", "device", "group", "duration", "selector"}:
        key, value, option = {
            "target": ("nodes", ["21"], "--node"),
            "mode": ("whole_node", True, "--whole-node"),
            "model": ("model", "P800", "--model"),
            "device": ("devices", ["dev0"], "--device"),
            "group": ("requested_group", "other-group", "--group"),
            "duration": ("duration", "1h", "--duration"),
            "selector": ("connection", "private/group-8.json", "--connection"),
        }[fault]
        diagnostic["parsed_cli"][key] = value
        if fault == "target":
            diagnostic["argv"][-1] = "21"
        else:
            diagnostic["argv"] += [option] + ([] if value is True else value if isinstance(value, list) else [value])
    elif fault in {"unowned", "not-claimable", "stale", "unknown-age", "unhealthy", "wrong-actor", "missing-snapshot"}:
        for row in wire[: diagnostic["http_request_span"][1]]:
            if row.get("resource_bot") != 8 or "resource_resolution_snapshot" not in row:
                continue
            snapshot = row["resource_resolution_snapshot"]
            node = snapshot["resources"][0]
            if fault == "unowned":
                node["booking_list"][0]["external_user_id"] = "someone-else"
            elif fault == "not-claimable":
                node["claimable_by_you"] = False
            elif fault in {"stale", "unknown-age"}:
                snapshot["age_seconds"] = 600 if fault == "stale" else None
                row["resource_read_state"]["age_seconds"] = snapshot["age_seconds"]
            elif fault == "unhealthy":
                snapshot["health"] = row["resource_read_state"]["health"] = "unknown"
            elif fault == "wrong-actor":
                snapshot["actor"]["external_user_id"] = "someone-else"
            else:
                row.pop("resource_resolution_snapshot")
    elif fault == "uncertain":
        diagnostic["result"]["command_sent"] = None
    elif fault == "wrong-error":
        diagnostic["result"]["error"] = "selected_resource_model_unverified_or_insufficient"
    elif fault == "retry":
        diagnostic["result"]["retry_mutation"] = True
    elif fault == "diagnostic-post":
        wire[diagnostic["http_request_span"][0]]["method"] = "POST"
    elif fault == "unverified":
        claim["result"]["result"]["verified"] = False
    elif fault == "uncertain-write":
        claim["result"]["error"] = "command_outcome_unknown"
    elif fault == "post-failed":
        next(row for row in wire if row["method"] == "POST")["status"] = 504
    elif fault == "wrong-post":
        next(row for row in wire if row["method"] == "POST")["command"] = "lock node-21"
    elif fault == "missing-span":
        diagnostic.pop("http_request_span")
    elif fault == "overlap":
        diagnostic["http_request_span"][1] = claim["http_request_span"][1]
    elif fault == "boolean-span":
        diagnostic["http_request_span"][0] = False
    else:
        events.insert(1, dict(diagnostic))
    save_rows(fixture, "trace.jsonl", events)
    save_rows(fixture, "cli-http.jsonl", wire)
    grade = cli.score(fixture.root, answer)
    assert not grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["read_only_claim_diagnostics"] == 0


def test_complete_enrollment_rejects_parenthetical_negative_instruction(tmp_path):
    fixture = prepare(tmp_path, "approved-enrollment", version=36)
    result = fixture.call("run_lockbot", {"action": "enroll", "group": cli.GROUP, "authorized": True})
    code = result["result"]["bind_command"]
    positive = f"目前还不能读取资源。请用你本人的聊天账号，在验证群 @LockBot 发送 {code}。"
    assert cli.score(fixture.root, positive)["observable_pass"]
    for negative in ("不要（重要！）使用你本人的聊天账号", "（不要！）使用你本人的聊天账号"):
        assert not cli.score(fixture.root, positive.replace("请用你本人的聊天账号", negative))["observable_pass"]
