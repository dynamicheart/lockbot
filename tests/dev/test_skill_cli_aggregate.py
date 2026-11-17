"""fv18 aggregate observations come from the packaged CLI and recorded HTTP."""

import json

import pytest
from dev.test_skill_cli_eval import evaluation, prepare, run


@pytest.mark.parametrize("mine", [False, True])
@pytest.mark.parametrize("coherent", [False, True])
def test_actual_catalog_cli_reads_both_groups_without_fabricating_tool_calls(tmp_path, monkeypatch, mine, coherent):
    fixture = prepare(tmp_path, monkeypatch, "intent-my-books")
    assert fixture.case["fixture_version"] == 18
    if not coherent:
        fixture.case["coherent_snapshot"] = False
        evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    before = {str(p): p.read_bytes() for p in (fixture.workspace / "private").rglob("*") if p.is_file()}
    output = run(fixture, "resources-all", mine=mine)["result"]
    assert output["complete"] and len(output["groups"]) == 2
    assert output["http_requests"] == (4 if coherent else 6)
    if mine:
        cards = output["groups"][0]["snapshot"]
        assert [entry["device"] for entry in cards["holdings"]] == [2]
        assert [entry["device"] for entry in cards["bookings"]] == [3]
    grade = evaluation.score(fixture.root, "你book了node-10的3号卡和node-21整机；node-20是已持有的机器。")
    assert grade["observable_pass"], grade
    assert grade["manual_review_required"]
    calls = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert len(calls) == 1 and "resources-all" in calls[0]["argv"]
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert len(requests) == output["http_requests"] and all(row["method"] == "GET" for row in requests)
    assert before == {str(p): p.read_bytes() for p in (fixture.workspace / "private").rglob("*") if p.is_file()}
    assert (fixture.root / "prompt.txt").read_text() == evaluation.intent.PROMPTS["intent-my-books"]


@pytest.mark.parametrize("fault", ["partial", "wrong_actor", "missing_wire", "missing_span"])
def test_aggregate_grade_rejects_partial_or_unproven_observations(tmp_path, monkeypatch, fault):
    fixture = prepare(tmp_path, monkeypatch, "intent-my-books")
    original = fixture.http

    def partial(args):
        if (
            args["url"].endswith("/resources")
            and args["headers"].get("Authorization") == "Bearer " + evaluation.continuity.SECOND_KEY
        ):
            return {"status": 403, "body": {}}
        return original(args)

    if fault == "partial":
        monkeypatch.setattr(fixture, "http", partial)
    output = run(fixture, "resources-all", mine=True)["result"]
    if fault == "partial":
        assert output["complete"] is False and output["unread"]
    else:
        trace = fixture.root / "trace.jsonl"
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        if fault == "wrong_actor":
            events[0]["result"]["result"]["groups"][1]["snapshot"]["actor"]["external_user_id"] = "demo-user-other"
        elif fault == "missing_span":
            events[0].pop("http_request_span")
        else:
            (fixture.root / "cli-http.jsonl").write_text("")
        trace.write_text("".join(json.dumps(event) + "\n" for event in events))
    grade = evaluation.score(fixture.root, "你book了node-10的3号卡和node-21整机。")
    assert not grade["observable_pass"]
    assert not grade["checks"]["both_candidate_groups_examined"]


def test_catalog_locator_supports_real_followup_command_with_one_verified_post(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-lock-tail")
    catalog = run(fixture, "connections")["result"]
    assert len(catalog["connections"]) == 2
    result = run(fixture, "resources-all")["result"]
    group = next(row for row in result["groups"] if row["identity"]["bot"]["id"] == 8)
    locked = run(
        fixture,
        "command",
        connection=group["connection"]["connection"],
        operation="lock",
        nodes=["node-19"],
        whole_node=True,
    )
    assert locked["result"]["verified"]
    grade = evaluation.score(fixture.root, "19已锁三小时。")
    assert grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["resource_posts"] == 1


def test_catalog_discovery_survives_actual_conversation_resume(tmp_path, monkeypatch):
    first = prepare(tmp_path, monkeypatch, "intent-context-lock-day")
    catalog = run(first, "resources-all")["result"]
    group = next(row for row in catalog["groups"] if row["identity"]["bot"]["id"] == 8)
    evaluation.base.save_json(first.root / "response.json", {"text": "node-23是空闲P800整机。"})
    second_path = tmp_path / "phase-2"
    evaluation.resume(first.root, second_path)
    second = evaluation.Fixture(second_path)
    result = run(
        second,
        "command",
        connection=group["connection"]["connection"],
        operation="lock",
        nodes=["node-23"],
        whole_node=True,
        duration="1d",
    )
    assert result["result"]["verified"]
    answer = "node-23已锁一天。"
    evaluation.base.save_json(
        second_path / "response.json",
        {
            "text": answer,
            "conversation_sha256": evaluation.base.digest((second_path / "conversation.json").read_bytes()),
        },
    )
    grade = evaluation.score(second_path, answer)
    assert grade["observable_pass"], grade


def test_own_summary_does_not_prove_available_inventory(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-find-p800")
    run(fixture, "resources-all", mine=True)
    grade = evaluation.score(fixture.root, "node-23是空闲P800整机。")
    assert not grade["observable_pass"] and not grade["checks"]["full_inventory_scope_read"]


def test_version17_keeps_original_fixture_tool_protocol(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-my-books")
    fixture.case["fixture_version"] = 17
    fixture.case.pop("resource_aggregation")
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    cli = next(tool for tool in evaluation.tools(fixture.root) if tool["name"] == "run_lockbot")
    schema = cli["inputSchema"]["properties"]
    assert "resources-all" not in schema["action"]["enum"] and "mine" not in schema
    assert run(fixture, "connections")["result"]["groups"] == list(evaluation.intent.GROUP_IDS)
    assert run(fixture, "resources-all")["error"] == "unsupported_fixture_action"
