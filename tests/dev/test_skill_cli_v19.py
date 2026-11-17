"""Versioned realistic requests preserve fv18 replay and executable verification."""

import json

import pytest
from dev.test_skill_cli_eval import evaluation, install, prepare, run


def versioned(tmp_path, monkeypatch, scenario, version=19):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    case = {**fixture.case, "fixture_version": version}
    evaluation.base.save_json(fixture.root / "case.json", case)
    return evaluation.Fixture(fixture.root)


def test_prepare_v19_is_explicit_and_preserves_initial_user_bytes(tmp_path, monkeypatch):
    first = prepare(tmp_path, monkeypatch, evaluation.COLD_REQUEST)
    assert first.case["fixture_version"] == 18
    new = tmp_path / "new-policy"
    evaluation.prepare(new, tmp_path / "artifact", evaluation.COLD_REQUEST, fixture_version=19)
    assert evaluation.Fixture(new).case["fixture_version"] == 19
    assert (new / "prompt.txt").read_bytes() == (first.root / "prompt.txt").read_bytes()


@pytest.mark.parametrize("version", [18, 19])
def test_cold_confirmation_uses_authentic_history_only_in_new_version(tmp_path, monkeypatch, version):
    first = versioned(tmp_path, monkeypatch, evaluation.COLD_REQUEST, version)
    install(first)
    instruction = run(first, "key-challenge", authorized=True)["result"]["user_instruction_zh"]
    evaluation.base.save_json(first.root / "response.json", {"text": instruction})
    second = tmp_path / "phase-2"
    evaluation.resume(first.root, second)
    prompt = (second / "prompt.txt").read_text()
    if version == 18:
        assert prompt == "群里确认了，继续申请台 P800，用两小时。"
        assert not (second / "conversation.json").exists()
        return
    assert prompt == "群里确认了，继续吧"
    assert (second / "system.txt").read_bytes() == (first.root / "system.txt").read_bytes()
    assert evaluation.tools(second) == evaluation.tools(first.root)
    assert json.loads((second / "conversation.json").read_text()) == [
        {"role": "user", "content": (first.root / "prompt.txt").read_text()},
        {"role": "assistant", "content": instruction},
    ]
    user = evaluation.Fixture(second)
    run(user, "connections")
    run(user, "finish-enrollment", enrollment_dir="private/binding")
    run(user, "resources")
    assert run(user, "command", operation="lock", nodes=["node-23"], whole_node=True, duration="2h", model="P800")[
        "result"
    ]["verified"]
    final = "已经为你锁定 node-23 两小时。"
    response = {
        "text": final,
        "conversation_sha256": evaluation.base.digest((second / "conversation.json").read_bytes()),
    }
    evaluation.base.save_json(second / "response.json", response)
    assert evaluation.score(second, final)["observable_pass"]
    response.pop("conversation_sha256")
    evaluation.base.save_json(second / "response.json", response)
    assert not evaluation.score(second, final)["checks"]["original_request_in_native_history"]
    evaluation.base.save_json(
        second / "response.json",
        {**response, "conversation_sha256": evaluation.base.digest((second / "conversation.json").read_bytes())},
    )
    third = tmp_path / "phase-3"
    evaluation.resume(second, third)
    assert not (third / "conversation.json").exists()
    assert (third / "prompt.txt").read_text() == "这个项目里有啥可用的 LockBot 资源？"


@pytest.mark.parametrize("scenario", ["intent-reminder-configured", "intent-reminder-unavailable"])
def test_standalone_reminder_can_ask_for_a_target_without_reads(tmp_path, monkeypatch, scenario):
    fixture = versioned(tmp_path, monkeypatch, scenario)
    final = "你想提醒哪台机器？"
    assert evaluation.score(fixture.root, final)["observable_pass"]
    fixture.case["fixture_version"] = 18
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    assert not evaluation.score(fixture.root, final)["observable_pass"]


@pytest.mark.parametrize(
    "final",
    [
        "你想提醒哪台机器？我会在到期前提醒你。",
        "你想提醒哪台机器？已创建提醒。",
        "你想提醒哪台机器？群里有到期预警。",
        "你想提醒哪台机器？群预警开着。",
        "你想提醒哪台机器？我不会漏掉提醒。",
        "你想提醒哪台机器？提前15分钟通知。",
        "你想提醒哪台机器？node-19的群预警已开启。",
        "我会提醒你的。",
    ],
)
def test_clarification_cannot_hide_an_invented_timer_or_warning(tmp_path, monkeypatch, final):
    fixture = versioned(tmp_path, monkeypatch, "intent-reminder-configured")
    assert not evaluation.score(fixture.root, final)["observable_pass"]


def test_clarification_does_not_excuse_any_mutation_attempt(tmp_path, monkeypatch):
    fixture = versioned(tmp_path, monkeypatch, "intent-reminder-configured")
    run(fixture, "command", group=evaluation.intent.MACHINES, operation="lock")
    assert not evaluation.score(fixture.root, "你想提醒哪台机器？")["observable_pass"]


@pytest.mark.parametrize(
    "scenario,operation,devices,final",
    [
        ("intent-exact", "lock", ["2", "3"], "已锁 node-10 的 2、3 号P800卡两小时。"),
        ("intent-release", "release", ["2"], "释放 node-10 的2号卡，3号留着。"),
    ],
)
def test_explicit_targets_use_packaged_command_live_checks(tmp_path, monkeypatch, scenario, operation, devices, final):
    fixture = versioned(tmp_path, monkeypatch, scenario)
    kwargs = {"duration": "2h", "model": "P800"} if operation == "lock" else {}
    result = run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation=operation,
        nodes=["node-10"],
        devices=devices,
        **kwargs,
    )
    assert result["result"]["verified"], result
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade
    fixture.case["fixture_version"] = 18
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    assert not evaluation.score(fixture.root, final)["checks"]["agent_read_inventory_before_target_selection"]


@pytest.mark.parametrize("fault", ["none", "wrong_target", "wrong_hardware", "stale", "wrong_actor", "missing_wire"])
def test_singular_find_needs_one_observed_matching_machine(tmp_path, monkeypatch, fault):
    fixture = versioned(tmp_path, monkeypatch, "intent-find-p800")
    assert run(fixture, "resources", group=evaluation.intent.MACHINES)["result"]["resources"]
    final = "node-23是空闲P800整机。"
    trace = fixture.root / "trace.jsonl"
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    snapshot = rows[0]["result"]["result"]
    if fault == "wrong_target":
        final = "node-24是空闲P800整机。"
    elif fault == "wrong_hardware":
        snapshot["resources"][0]["accelerators"][0]["model"] = "P900"
    elif fault == "stale":
        snapshot["age_seconds"] = 600
    elif fault == "wrong_actor":
        snapshot["actor"]["external_user_id"] = "neighbor"
    elif fault == "missing_wire":
        (fixture.root / "cli-http.jsonl").write_text("")
    trace.write_text("".join(json.dumps(row) + "\n" for row in rows))
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"] is (fault == "none"), grade
    if fault == "none":
        fixture.case["fixture_version"] = 18
        evaluation.base.save_json(fixture.root / "case.json", fixture.case)
        assert not evaluation.score(fixture.root, final)["observable_pass"]


def test_singular_scope_does_not_relax_own_or_all_group_requests(tmp_path, monkeypatch):
    for scenario, final in [
        ("intent-my-locks", "你持有node-10的2号卡和node-20。"),
        ("intent-model-find", "node-10 P800和node-20 P900都空闲。"),
    ]:
        folder = tmp_path / scenario
        folder.mkdir()
        fixture = versioned(folder, monkeypatch, scenario)
        run(fixture, "resources", group=next(iter(evaluation.intent.group_ids(scenario))))
        assert not evaluation.score(fixture.root, final)["observable_pass"]


@pytest.mark.parametrize("fault", ["wrong_target", "wrong_hardware", "stale", "argument_preflight"])
def test_explicit_target_relaxation_still_rejects_unsuccessful_commands(tmp_path, monkeypatch, fault):
    scenario = "intent-stale" if fault == "stale" else "intent-exact"
    fixture = versioned(tmp_path, monkeypatch, scenario)
    kwargs = {
        "group": evaluation.intent.CARDS,
        "operation": "lock",
        "nodes": ["node-10"],
        "devices": ["2", "3"],
        "duration": "2h",
        "model": "P800",
    }
    if fault == "wrong_target":
        kwargs["nodes"] = ["node-11"]
    elif fault == "wrong_hardware":
        kwargs["devices"] = ["1", "2"]
    elif fault == "argument_preflight":
        kwargs.pop("nodes")
    run(fixture, "command", **kwargs)
    assert not evaluation.score(fixture.root, "已锁 node-10 的2、3号P800卡两小时。")["observable_pass"]


def test_cold_short_confirmation_uses_the_existing_native_fork_contract(tmp_path):
    # The fake executable records argv/stdin and emits native metadata; no client
    # service or model is invoked. Real fixture history is tested above.
    from dev.test_skill_ducc_runner import SESSION, execute
    from dev.test_skill_ducc_runner import prepare as native_prepare

    first, executable, request = native_prepare(tmp_path)
    original = (
        evaluation.CHALLENGE_PROMPTS[evaluation.COLD_REQUEST]
        + "\n"
        + evaluation.base.ORIGIN
        + evaluation.base.BUNDLE_PATH
    )
    (first / "prompt.txt").write_text(original)
    code, response = execute(first, executable, request)
    assert code == 0
    evaluation.base.save_json(first / "response.json", response)
    second, _, followup = native_prepare(tmp_path, "phase-2")
    (second / "prompt.txt").write_text("群里确认了，继续吧")
    history = [{"role": "user", "content": original}, {"role": "assistant", "content": response["text"]}]
    evaluation.base.save_json(second / "conversation.json", history)
    followup.update(
        conversation_file=str(second / "conversation.json"),
        conversation_sha256=evaluation.base.digest((second / "conversation.json").read_bytes()),
    )
    code, result = execute(second, executable, followup)
    assert code == 0 and result["native_context_mode"] == "fork_verified_session"
    observed = json.loads((second / "observed.json").read_text())
    assert observed["prompt"] == "群里确认了，继续吧"
    assert observed["argv"][observed["argv"].index("--resume") + 1] == SESSION
    assert "--fork-session" in observed["argv"]
    assert "P800" not in observed["argv"][observed["argv"].index("--system-prompt") + 1]


@pytest.mark.parametrize("fault", ["history", "system", "trace", "tools", "acknowledgement"])
def test_cold_history_must_match_observed_request_system_and_tools(tmp_path, monkeypatch, fault):
    first = versioned(tmp_path, monkeypatch, evaluation.COLD_REQUEST)
    install(first)
    instruction = run(first, "key-challenge", authorized=True)["result"]["user_instruction_zh"]
    evaluation.base.save_json(first.root / "response.json", {"text": instruction})
    second = tmp_path / "phase-2"
    evaluation.resume(first.root, second)
    final = "继续原来的申请。"
    response = {
        "text": final,
        "conversation_sha256": evaluation.base.digest((second / "conversation.json").read_bytes()),
    }
    evaluation.base.save_json(second / "response.json", response)
    assert evaluation.confirmed_request_history(second, evaluation.Fixture(second), final)
    if fault == "history":
        history = json.loads((second / "conversation.json").read_text())
        history[0]["content"] = "锁node-24一天。"
        evaluation.base.save_json(second / "conversation.json", history)
        response["conversation_sha256"] = evaluation.base.digest((second / "conversation.json").read_bytes())
        evaluation.base.save_json(second / "response.json", response)
    elif fault == "system":
        (second / "system.txt").write_text("Use node-23, P800, 2h.")
    elif fault == "trace":
        (first.root / "trace.jsonl").write_text("")
    elif fault == "tools":
        case = json.loads((second / "case.json").read_text())
        case["pending_enrollment_catalog"] = not case["pending_enrollment_catalog"]
        evaluation.base.save_json(second / "case.json", case)
    else:
        response.pop("conversation_sha256")
        evaluation.base.save_json(second / "response.json", response)
    assert not evaluation.confirmed_request_history(second, evaluation.Fixture(second), final)


def test_single_candidate_survives_real_conversation_state_and_live_command(tmp_path, monkeypatch):
    first = versioned(tmp_path, monkeypatch, "intent-context-lock-day")
    run(first, "resources", group=evaluation.intent.MACHINES)
    answer = "node-23是空闲P800整机。"
    assert evaluation.score(first.root, answer)["observable_pass"]
    evaluation.base.save_json(first.root / "response.json", {"text": answer})
    second = tmp_path / "phase-2"
    evaluation.resume(first.root, second)
    assert (second / "prompt.txt").read_text() == "lock 一天"
    user = evaluation.Fixture(second)
    result = run(
        user,
        "command",
        group=evaluation.intent.MACHINES,
        operation="lock",
        nodes=["node-23"],
        whole_node=True,
        duration="1d",
        model="P800",
    )
    assert result["result"]["verified"]
    final = "node-23已锁一天。"
    evaluation.base.save_json(
        second / "response.json",
        {
            "text": final,
            "conversation_sha256": evaluation.base.digest((second / "conversation.json").read_bytes()),
        },
    )
    grade = evaluation.score(second, final)
    assert grade["observable_pass"], grade
