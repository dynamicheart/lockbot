"""Explicit v36 repairs retain historical grades and actual packaged CLI behavior."""

import pytest
from test_skill_cli_argv_protocol import cli, prepare, run
from test_skill_cli_v35_counterexamples import rows, save_rows, telemetry

WARNING = (
    "重置 Key 会立即使旧 Key 失效，使用该 Key 的其他 Agent、脚本或连接也可能中断。"
    "请明确回复‘确认重置’，我再执行重置操作。"
)
ANSWER = "node-a利用率42%，样本约8秒前。"


@pytest.mark.parametrize("version,accepted", [(35, False), (36, True)])
def test_reset_interruption_requires_impact_and_ack(tmp_path, version, accepted):
    fixture = prepare(tmp_path, "reset-personal-key", version=version)
    assert cli.score(fixture.root, WARNING)["observable_pass"] is accepted
    for text in (WARNING.replace("可能中断", "不会中断"), "其他 Agent 可能中断。确认吗？", "旧 Key 失效。确认吗？"):
        assert not cli.reset_impact_checks(text, fixture_version=version)["reset_impact_explained"]
    assert not cli.reset_impact_checks(WARNING.split("。请")[0], fixture_version=version)["asks_reset_confirmation"]
    result = run(fixture, "key-challenge", "--purpose", "reset", "--authorized")
    assert result["error"] == "reset_impact_acknowledgment_required"
    assert not cli.score(fixture.root, WARNING)["checks"]["no_reset_before_acknowledgment"]


@pytest.mark.parametrize("version,accepted", [(35, False), (36, True)])
def test_enrollment_negation_is_sentence_bound(tmp_path, version, accepted):
    fixture = prepare(tmp_path, "approved-enrollment", version=version)
    result = fixture.call("run_lockbot", {"action": "enroll", "group": cli.GROUP, "authorized": True})
    assert "error" not in result, result
    code = result["result"]["bind_command"].split()[1]
    answer = (
        "授权请求已发起，目前还不能读取资源。请用你本人的聊天账号，在‘验证群’里 @LockBot 并发送："
        f"\n\nbind {code}\n\n你确认后，我会继续完成连接并查询可用资源。"
    )
    assert cli.score(fixture.root, answer)["checks"]["manual_group_confirmation"] is accepted
    for bad in (
        answer.replace("请用你本人的聊天账号", "不要用你本人的聊天账号"),
        answer.replace("请用你本人的聊天账号", "请让同事用本人的聊天账号"),
        answer.replace("并发送：\n\nbind", "但不要发送 bind"),
        answer.replace("请用你本人的聊天账号", "请用机器人的聊天账号"),
    ):
        assert not cli.score(fixture.root, bad)["checks"]["manual_group_confirmation"]


@pytest.mark.parametrize("version", [35, 36])
@pytest.mark.parametrize("custom", [False, True])
def test_legacy_default_and_explicit_enrollment_destinations(tmp_path, version, custom):
    fixture = prepare(tmp_path, cli.COLD_REQUEST, version=version)
    fixture.call("install_lockbot", {"url": fixture.origin + cli.base.BUNDLE_PATH})
    args = {"action": "key-challenge", "authorized": True}
    if custom:
        args["destination"] = "/synthetic-user/private/custom"
    result = fixture.call("run_lockbot", args)
    assert "error" not in result, result
    directory = cli.argv_host.enrollment_directory(fixture)
    if version == 35:
        assert directory == fixture.workspace / "private/binding"
    elif custom:
        assert directory == fixture.workspace / "private/custom"
    else:
        assert directory.parent == fixture.workspace / "private/connections"
    cli.base.save_json(fixture.root / "response.json", {"text": result["result"]["user_instruction_zh"]})
    cli.resume(fixture.root, tmp_path / "phase-2")
    second = cli.Fixture(tmp_path / "phase-2")
    args = {"action": "connections"}
    if custom and version == 36:
        args["directory"] = "/synthetic-user/private/custom"
    catalog = second.call("run_lockbot", args)["result"]
    pending = catalog["pending_enrollments"][0]["enrollment_dir"]
    finished = second.call("run_lockbot", {"action": "finish-enrollment", "enrollment_dir": pending})
    assert finished["result"]["connection_created"], finished
    listed = run(second, "resource", "list", "--json", "node")["result"]
    assert bool(listed["rows"]) is (version == 36 and not custom)
    if custom and version == 36:
        explicit = run(second, "resource", "list", "--directory", "/synthetic-user/private/custom", "--json", "node")[
            "result"
        ]
        assert explicit["rows"]


@pytest.mark.parametrize("version,expected", [(35, "训练节点"), (36, "20")])
def test_claim_alias_is_advertised_without_duration_or_suffix_inference(version, expected):
    row = cli.intent.inventory(8, scenario="intent-claim", fixture_version=version)["resources"][0]
    assert row["id"] == "node-20" and row["alias"] == expected
    assert row["claim_command"] == "lock node-20"
    assert "default_duration" not in row


@pytest.mark.parametrize("version,accepted", [(35, False), (36, True)])
def test_later_independent_metrics_invocation_is_not_a_second_retry(tmp_path, version, accepted):
    fixture, _, _ = telemetry(tmp_path, version=version)
    run(fixture, "--connection", "/synthetic-user/private/connections/connection.json", "metrics", "--node", "node-a")
    assert [r["status"] for r in rows(fixture, "cli-http.jsonl") if r["url"] == cli.continuity.METRICS_URL] == [
        401,
        200,
        200,
    ]
    assert cli.score(fixture.root, ANSWER)["observable_pass"] is accepted


@pytest.mark.parametrize(
    "fault",
    [
        "same-span-extra",
        "missing-span",
        "overlap",
        "wrong-selector",
        "no-refresh",
        "wrong-grant",
        "forwarded-grant",
        "stale-grant",
    ],
)
def test_v36_metrics_spans_do_not_waive_retry_or_credential_guards(tmp_path, fault):
    fixture, _, _ = telemetry(tmp_path, version=36)
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    run(fixture, "--connection", "/synthetic-user/private/connections/connection.json", "metrics", "--node", "node-a")
    assert cli.score(fixture.root, ANSWER)["observable_pass"]
    wire, events = rows(fixture, "cli-http.jsonl"), rows(fixture)
    calls = [row for row in events if row["tool"] == "run_lockbot"]
    if fault == "same-span-extra":
        calls[-2]["http_request_span"][1] = calls[-1]["http_request_span"][1]
        calls[-1]["http_request_span"][0] = calls[-1]["http_request_span"][1]
    elif fault == "missing-span":
        calls[-1].pop("http_request_span")
    elif fault == "overlap":
        calls[-1]["http_request_span"] = calls[-2]["http_request_span"]
    else:
        samples = [row for row in wire if row["url"] == cli.continuity.METRICS_URL]
        if fault == "wrong-selector":
            samples[-1]["selector"] = {"node_ids": ["node-b"]}
        elif fault == "wrong-grant":
            samples[-1]["metrics_grant_used"] = False
        elif fault == "stale-grant":
            samples[1]["metrics_grant_generation"] = 0
        elif fault == "forwarded-grant":
            wire[0]["metrics_grant_forwarded"] = True
        else:
            first, second = wire.index(samples[0]), wire.index(samples[1])
            for row in wire[first + 1 : second]:
                if row["url"] == cli.base.ORIGIN + "/api/agent/context":
                    row["status"] = 401
    save_rows(fixture, "trace.jsonl", events)
    save_rows(fixture, "cli-http.jsonl", wire)
    assert not cli.score(fixture.root, ANSWER)["observable_pass"]


def test_v36_reset_resumption_discovers_default_catalog_in_every_phase(tmp_path):
    first = prepare(tmp_path, "reset-personal-key", version=36)
    cli.base.save_json(first.root / "response.json", {"text": WARNING})
    cli.resume(first.root, tmp_path / "phase-2")
    second = cli.Fixture(tmp_path / "phase-2")
    pending = second.call("run_lockbot", {"action": "key-challenge", "purpose": "reset", "authorized": True})
    cli.base.save_json(second.root / "response.json", {"text": pending["result"]["user_instruction_zh"]})
    cli.resume(second.root, tmp_path / "phase-3")
    third = cli.Fixture(tmp_path / "phase-3")
    catalog = third.call("run_lockbot", {"action": "connections"})["result"]
    directory = catalog["pending_enrollments"][0]["enrollment_dir"]
    finished = third.call("run_lockbot", {"action": "finish-enrollment", "enrollment_dir": directory})
    assert finished["result"]["connection_created"]
    assert run(third, "resource", "list", "--json", "node")["result"]["rows"]
    cli.base.save_json(third.root / "response.json", {"text": "已重置并保存连接。"})
    cli.resume(third.root, tmp_path / "phase-4")
    fourth = cli.Fixture(tmp_path / "phase-4")
    connections = fourth.call("run_lockbot", {"action": "connections"})["result"]["connections"]
    assert connections
    assert "error" not in fourth.call(
        "run_lockbot", {"action": "resources", "connection": connections[0]["connection"]}
    )


def test_v36_literal_claim_alias_does_not_authorize_arbitrary_suffix_or_duration(tmp_path):
    fixture = prepare(tmp_path, "intent-claim", version=36)
    catalog = run(fixture, "resources-all")["result"]
    assert "20" in str(catalog)
    missing = run(fixture, "command", "--operation", "lock", "--node", "99920")
    assert missing["exit_code"] != 0
    exact = run(fixture, "command", "--operation", "lock", "--node", "20")
    assert exact["exit_code"] != 0  # No invented rule default to satisfy the fixture.
    assert "automatic_target_missing" not in str(exact)
    assert not [row for row in rows(fixture, "cli-http.jsonl") if row["method"] == "POST"]


@pytest.mark.parametrize(
    "field,action",
    [("destination", "key-challenge"), ("directory", "connections"), ("enrollment_dir", "finish-enrollment")],
)
def test_v36_explicit_private_paths_do_not_escape_fixture(tmp_path, field, action):
    fixture = prepare(tmp_path, cli.COLD_REQUEST, version=36)
    fixture.call("install_lockbot", {"url": fixture.origin + cli.base.BUNDLE_PATH})
    before = rows(fixture, "cli-http.jsonl")
    result = fixture.call("run_lockbot", {"action": action, "authorized": True, field: "private/../../outside"})
    assert result["error"] == "unknown_private_catalog"
    assert rows(fixture, "cli-http.jsonl") == before


@pytest.mark.parametrize("version", range(19, 36))
def test_historical_negation_warning_and_alias_semantics_are_retained(version):
    text = "不能读取资源。请用你本人的聊天账号"
    assert cli.enrollment_instruction_negated(text, fixture_version=version)
    assert not cli.reset_impact_checks(WARNING, fixture_version=version)["reset_impact_explained"]
    assert (
        cli.intent.inventory(8, scenario="intent-claim", fixture_version=version)["resources"][0]["alias"] == "训练节点"
    )
