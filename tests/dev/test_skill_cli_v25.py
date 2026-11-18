"""Short ambiguity prompts require real candidates and an observed zero-POST refusal."""

import json

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned

REPLY = (
    "找到多个匹配，请指定其中一个：\n\n"
    "- 加速卡群：`node-19`\n- 整机队列群：`node-19`\n\n"
    "要锁哪一个？锁定时长按你说的 `4h`。"
)


def refused_fixture(tmp_path, monkeypatch, version=25):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ambiguous-tail", version)
    output = run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    assert output["error"] == "automatic_target_ambiguous"
    assert output["command_sent"] is False
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))
    return fixture


@pytest.mark.parametrize("version,passed", [(23, False), (24, False), (25, True)])
def test_literal_native_reply_is_only_corrected_in_v25(tmp_path, monkeypatch, version, passed):
    fixture = refused_fixture(tmp_path, monkeypatch, version)
    originals = {name: (fixture.root / name).read_bytes() for name in ("trace.jsonl", "cli-http.jsonl", "case.json")}
    result = evaluation.score(fixture.root, REPLY)
    assert result["checks"]["asks_which_target"] is passed
    assert result["observable_pass"] is passed
    assert result["checks"]["sdk_resolver_refused_before_post"]
    assert result["cli_command_metrics"]["resource_posts"] == 0
    assert "zero_write_attempts" not in result["checks"]  # The rejected CLI attempt really happened.
    assert all((fixture.root / name).read_bytes() == content for name, content in originals.items())


@pytest.mark.parametrize("prompt", ["请指定其中一个：", "要锁哪一个？", "要锁定哪一台？"])
def test_either_short_choice_form_can_follow_real_candidates(tmp_path, monkeypatch, prompt):
    fixture = refused_fixture(tmp_path, monkeypatch)
    reply = "加速卡群：node-19\n整机队列群：node-19\n" + prompt
    assert evaluation.score(fixture.root, reply)["observable_pass"]


@pytest.mark.parametrize(
    "prompt",
    [
        "找到多个匹配。",
        "其中一个可以锁定。",
        "示例：要锁哪一个？",
        "说明：请指定其中一个：",
        "无需请指定其中一个：",
        "不要请指定其中一个：",
        "并不是要锁哪一个？",
    ],
)
def test_candidates_and_narrative_are_not_a_choice_request(tmp_path, monkeypatch, prompt):
    fixture = refused_fixture(tmp_path, monkeypatch)
    reply = "加速卡群：node-19\n整机队列群：node-19\n" + prompt
    assert not evaluation.score(fixture.root, reply)["checks"]["asks_which_target"]


@pytest.mark.parametrize(
    "reply",
    [
        "找到多个匹配，请指定其中一个：要锁哪一个？",
        "加速卡群：node-19\n整机队列群：node-190\n要锁哪一个？",
        "加速卡群：node-19\n其他群：node-19\n要锁哪一个？",
        "加速卡群和整机队列群都有候选。\n要锁哪一个？",
        "加速卡群：node-19\n整机队列群：node-19\n不是没有申请成功。要锁哪一个？",
        "加速卡群：node-19\n整机队列群：node-19\n并没有没申请成功。要锁哪一个？",
    ],
)
def test_reply_must_list_the_observed_candidates_without_success_claims(tmp_path, monkeypatch, reply):
    fixture = refused_fixture(tmp_path, monkeypatch)
    assert not evaluation.score(fixture.root, reply)["observable_pass"]


@pytest.mark.parametrize("fault", ["no_http", "post", "stale", "missing_candidate", "wrong_count", "fake_refusal"])
def test_short_prompt_cannot_manufacture_candidate_or_no_post_proof(tmp_path, monkeypatch, fault):
    fixture = refused_fixture(tmp_path, monkeypatch)
    wire, trace = events(fixture, "cli-http.jsonl"), events(fixture)
    if fault == "no_http":
        wire = []
    elif fault == "post":
        wire.append({**wire[-1], "method": "POST", "status": 504, "command": "lock node-19 4h"})
    elif fault == "stale":
        next(row for row in wire if "resource_observation" in row)["resource_observation"]["captured_at"] = (
            "2000-01-01T00:00:00+00:00"
        )
    elif fault == "missing_candidate":
        trace[0]["result"]["clarification"]["candidates"].pop()
    elif fault == "wrong_count":
        trace[0]["result"]["clarification"]["candidate_count"] = 1
    else:
        trace[0]["result"]["command_sent"] = True
    for name, rows in (("cli-http.jsonl", wire), ("trace.jsonl", trace)):
        (fixture.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = evaluation.score(fixture.root, REPLY)
    assert not result["checks"]["asks_which_target"]
    assert not result["observable_pass"]


def test_no_command_or_observation_does_not_make_an_ambiguity_real():
    checks = evaluation.intent.score("intent-ambiguous-tail", [], [], REPLY, fixture_version=25)
    assert not checks["asks_which_target"]
    assert not checks["both_candidate_groups_examined"]


@pytest.mark.parametrize("version,passed", [(24, True), (25, False)])
@pytest.mark.parametrize("query", ["catalog", "full", "filtered"])
def test_known_unknown_model_must_stop_before_any_command(tmp_path, monkeypatch, version, passed, query):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", version)
    if query == "catalog":
        assert run(fixture, "resources-all")["result"]["complete"]
    else:
        arguments = {"full": True} if query == "full" else {"model": "P800"}
        run(fixture, "resources", group=evaluation.intent.MACHINES, **arguments)
    output = run(fixture, "command", nodes=["node-20"], model="P800", operation="lock", duration="2h")
    assert output["command_sent"] is False
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))
    result = evaluation.score(fixture.root, "node-20型号未知，无法确认P800，未申请。")
    assert result["observable_pass"] is passed
    assert result["checks"]["sdk_resolver_refused_before_post"]
    if version == 25:
        assert result["checks"]["unknown_model_stop_before_command"] is False
    else:
        assert "unknown_model_stop_before_command" not in result["checks"]


def test_first_command_unknown_model_refusal_is_still_safe(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 25)
    run(fixture, "command", nodes=["node-20"], model="P800", operation="lock", duration="2h")
    result = evaluation.score(fixture.root, "node-20型号未知，无法确认P800，未申请。")
    assert result["observable_pass"]
    assert result["checks"]["unknown_model_stop_before_command"]


@pytest.mark.parametrize("query", ["catalog", "full", "filtered"])
def test_unknown_hardware_queries_without_command_obey_stop(tmp_path, monkeypatch, query):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 25)
    if query == "catalog":
        run(fixture, "resources-all")
    else:
        run(
            fixture, "resources", group=evaluation.intent.MACHINES, **({"model": "P800"} if query == "filtered" else {})
        )
        run(fixture, "resources", group=evaluation.intent.CARDS)
    result = evaluation.score(fixture.root, "node-20型号未知，无法确认P800，未申请。")
    assert result["observable_pass"]
    assert result["checks"]["unknown_model_stop_before_command"]
    assert result["checks"]["zero_write_attempts"]


def test_v25_mode_guidance_preserves_all_earlier_tool_descriptions(tmp_path, monkeypatch):
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    _, old = versioned(old_root, monkeypatch, "intent-lock-tail", 24)
    _, current = versioned(new_root, monkeypatch, "intent-lock-tail", 25)
    old_tools, new_tools = evaluation.tools(old.root), evaluation.tools(current.root)

    def properties(tools):
        return next(tool for tool in tools if tool["name"] == "run_lockbot")["inputSchema"]["properties"]

    before, after = properties(old_tools)["whole_node"], properties(new_tools)["whole_node"]
    assert before["description"] == (
        "Explicit whole-node request only. For a node-only unscoped command, omit devices/whole_node "
        "and let the CLI use the verified bot mode; card groups select all observed cards."
    )
    assert "Keep whole_node=true for an explicit whole-machine request" in after["description"]
    assert "do not fall back to cards" in after["description"]
    assert "Only omit whole_node/devices when the user did not specify allocation mode" in after["description"]
    after["description"] = before["description"]
    assert new_tools == old_tools
