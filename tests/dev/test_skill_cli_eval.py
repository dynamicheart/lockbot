"""Exercise the model fixture with the production bundle and no model service."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/dev"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("skill_cli_eval", DIRECTORY / "skill_cli_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def prepare(tmp_path, monkeypatch, scenario, instance_id=None):
    origin = evaluation.PANEL_ORIGIN if scenario == "panel" else evaluation.base.ORIGIN
    monkeypatch.setenv("PLATFORM_URL", origin)
    _, _, manifest = skill.skill_artifacts()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "lockbot-resource-agent.zip").write_bytes(skill.installation_bundle())
    if instance_id is not None:
        manifest = {**manifest, "instance_id": instance_id}
    evaluation.base.save_json(artifact / "manifest.json", manifest)
    case = tmp_path / scenario
    evaluation.prepare(case, artifact, scenario)
    return evaluation.Fixture(case)


def install(fixture):
    return fixture.call("install_lockbot", {"url": fixture.origin + evaluation.base.BUNDLE_PATH})


def run(fixture, action, **kwargs):
    return fixture.call("run_lockbot", {"action": action, **kwargs})


def test_ordinary_user_installs_and_requests_a_machine_without_key_instructions(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, evaluation.COLD_REQUEST)
    request = (fixture.root / "prompt.txt").read_text().splitlines()[0]
    assert request == "帮我安装这个 Skill，申请一台 P800 机器用两小时。"
    assert run(fixture, "local-state")["result"]["seed_key_file_present"] is False
    assert install(fixture)["installed"]
    challenge = run(fixture, "key-challenge", authorized=True)["result"]
    first = evaluation.score(fixture.root, challenge["user_instruction_zh"])
    assert first["observable_pass"], first["checks"]

    second = tmp_path / "confirmed"
    evaluation.resume(fixture.root, second)
    user = evaluation.Fixture(second)
    run(user, "connections")
    assert run(user, "finish-enrollment", enrollment_dir="private/binding")["result"]["connection_created"]
    assert run(user, "resources")["result"]["resources"]
    run(user, "command", operation="lock", nodes=["node-23"], whole_node=True, duration="2h", model="P800")
    observed = run(user, "resources")["result"]["resources"]
    assert next(row for row in observed if row["id"] == "node-23")["holders"]
    second_grade = evaluation.score(second, "已经为你锁定 node-23 两小时。")
    assert second_grade["observable_pass"], second_grade["checks"]

    third = tmp_path / "new-session"
    evaluation.resume(second, third)
    user = evaluation.Fixture(third)
    run(user, "connections")
    run(user, "resources", connection="private/binding/connection.json")
    third_grade = evaluation.score(third, "你现在持有 node-23。")
    assert third_grade["observable_pass"], third_grade["checks"]


def test_packaged_cli_remembers_service_without_requesting_binding(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "no-key")
    assert run(fixture, "discover")["error"] == "verified_package_required"
    assert install(fixture)["installed"]
    assert run(fixture, "discover")["result"]["instance_id"] == evaluation.base.INSTANCE
    assert run(fixture, "discover")["result"]["instance_id"] == evaluation.base.INSTANCE
    assert run(fixture, "enroll", group=evaluation.GROUP)["error"] == "invalid_cli_arguments"
    assert fixture.binding_status() == "expired"
    assert evaluation.score(fixture.root, "在资源群发送 bind 命令。")["observable_pass"]


def test_inspecting_link_does_not_authorize_installation(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "link-only")
    url = evaluation.base.ORIGIN + "/api/agent-skill"
    assert (fixture.root / "prompt.txt").read_text() == url
    inspected = fixture.call("inspect_lockbot", {"url": url})
    assert inspected["manifest"]["installation"]["sha256"]
    assert evaluation.score(fixture.root, "可以安装这个 Skill，方便以后使用。")["observable_pass"]

    assert fixture.call("install_lockbot", {"url": url})["installed"]
    checks = evaluation.score(fixture.root, "已安装。")["checks"]
    assert not checks["installed"] and not checks["no_bundle_download"]


def test_installer_verifies_the_manifest_without_a_separate_model_call(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "install-with-key")
    assert install(fixture)["installed"]
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert [(row["url"], row["authorization_present"]) for row in requests] == [
        (fixture.origin + "/api/agent-skill", False),
        (fixture.origin + evaluation.base.BUNDLE_PATH, False),
    ]
    assert run(fixture, "connections")["result"]["connections"][0]["connection"].endswith("/connection.json")
    events = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert "argv" not in events[0]
    assert events[-1]["args"] == {"action": "connections"}
    assert Path(events[-1]["argv"][0]).name == "lockbot_resource.py"
    assert events[-1]["argv"][1:] == ["connections"]
    assert run(fixture, "resources")["result"]["resources"]
    final = "已安装并查询资源。"
    assert evaluation.score(fixture.root, final)["observable_pass"]
    assert not evaluation.score(fixture.root, final + " private/connections/connection.json")["observable_pass"]


def test_zip_only_install_needs_no_separate_service_record(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "install-from-zip")
    assert not evaluation.score(fixture.root, "已安装。")["observable_pass"]
    assert install(fixture)["installed"]
    assert not (fixture.workspace / "private/service.json").exists()
    grade = evaluation.score(fixture.root, "已安装。")
    assert grade["observable_pass"], grade["checks"]
    assert grade["checks"]["installation_verified"] and grade["checks"]["no_authenticated_calls"]

    run(fixture, "enroll", group=evaluation.GROUP, authorized=True)
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["no_extra_writes"]


@pytest.mark.parametrize("scenario", ["install-from-link", "install-with-key"])
@pytest.mark.parametrize("instance_id", [evaluation.base.INSTANCE, "d709dca8-cd10-4602-a415-f480a63349be"])
def test_first_contact_requires_verified_install_and_complete_private_connection(
    tmp_path, monkeypatch, scenario, instance_id
):
    fixture = prepare(tmp_path, monkeypatch, scenario, instance_id)
    assert not evaluation.score(fixture.root, "已完成。")["observable_pass"]
    path = "/api/agent-skill" if scenario == "install-from-link" else evaluation.base.BUNDLE_PATH
    assert "manifest" in fixture.call("inspect_lockbot", {"url": evaluation.base.ORIGIN + path})
    assert install(fixture)["installed"]
    checks = evaluation.score(fixture.root, "已安装。")["checks"]
    if scenario == "install-with-key":
        assert checks["service_saved"] and not checks["resource_read"]
        assert run(fixture, "connections")["result"]["connections"][0]["instance_id"] == instance_id
        assert run(fixture, "resources")["result"]["resources"]
    else:
        assert not checks["service_saved"]
        assert run(fixture, "discover")["result"]["instance_id"] == instance_id
    grade = evaluation.score(fixture.root, "已安装、验证并保存连接。")
    assert grade["observable_pass"], grade["checks"]

    saved_path = (
        fixture.saved_connection if scenario == "install-with-key" else fixture.workspace / "private/service.json"
    )
    saved = json.loads(saved_path.read_text())
    evaluation.base.save_json(saved_path, {**saved, "instance_id": "wrong-instance"})
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["service_saved"]

    run(fixture, "enroll", group="验证群", authorized=True)
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["no_extra_writes"]


def test_panel_link_cli_delivers_once_and_new_session_reads_fixed_home(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "panel")
    tools = {tool["name"]: tool for tool in evaluation.tools(fixture.root)}
    assert {"install_lockbot", "run_lockbot", "deliver_private_link", "browser_home"} <= tools.keys()
    assert "local-state" in tools["browser_home"]["description"]
    assert fixture.case["fixture_version"] == evaluation.PANEL_FIXTURE_VERSION
    assert install(fixture)["installed"]
    assert run(fixture, "connections")["result"]["connections"]
    assert run(fixture, "local-state")["result"]["browser_bookmarks"] == []
    with pytest.raises(ValueError, match="confirm browser consent"):
        evaluation.resume(fixture.root, tmp_path / "too-early")
    result = run(fixture, "panel-link")["result"]
    assert result == {"home": fixture.panel_home, "login_url": fixture.panel_login_url}
    assert fixture.call("deliver_private_link", {"url": result["login_url"]})["delivered_privately"]
    final = f"登录链接已私下发给你，请本人确认。保存固定入口 {fixture.panel_home}。"
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade["checks"]
    assert not evaluation.score(fixture.root, final + result["login_url"])["checks"]["ticket_not_in_reply"]

    english_reply = f"Open the private link in your browser to confirm login, then bookmark {fixture.panel_home}."
    english_grade = evaluation.score(fixture.root, english_reply)
    assert english_grade["observable_pass"], english_grade["checks"]
    stale_reply = f"Your browser confirmation is complete. Bookmark {fixture.panel_home}."
    assert not evaluation.score(fixture.root, stale_reply)["checks"]["pending_human_confirmation"]

    evaluation.continuity.human_confirm(fixture.root)
    resumed = tmp_path / "fresh"
    evaluation.resume(fixture.root, resumed)
    browser = evaluation.Fixture(resumed)
    bookmark = run(browser, "local-state")["result"]["browser_bookmarks"]
    assert bookmark == [fixture.panel_home]
    home = browser.call("browser_home", {"url": bookmark[0]})
    assert home["authenticated"] and home["bookmarks"] == [fixture.panel_home]
    resources = run(browser, "resources")["result"]["resources"]
    assert home["own_occupancy"] == [node["id"] for node in resources if node["holders"]] == ["node-a"]
    assert home["own_bookings"] == [node["id"] for node in resources if node["booking_list"]] == ["node-b"]
    final = f"{fixture.panel_home} 已打开，持有 node-a、预约 node-b。"
    grade = evaluation.score(resumed, final)
    assert grade["observable_pass"], grade["checks"]
    assert browser.call("browser_home", {"url": result["login_url"]})["error"]
    assert not evaluation.score(resumed, final)["checks"]["browser_only_fixed_home"]


def test_telemetry_retries_once_with_replacement_grant_and_reports_sample_age(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "telemetry")
    assert install(fixture)["installed"]
    assert run(fixture, "connections")["result"]["connections"]
    assert run(fixture, "resources")["result"]["resources"][0]["holders"]
    result = run(fixture, "metrics", nodes=["node-a"])["result"]
    assert result["verification"] == "query_response_only"
    samples = result["data"]["data"]["result"]
    assert any(sample["value"][1] == "42" for sample in samples)
    final = "node-a 的卡利用率为 42%，样本约 8 秒前采集。"
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade["checks"]

    assert evaluation.score(fixture.root, "node-a 利用率 42%，样本新鲜度约 8.1s。")["observable_pass"]
    assert not evaluation.score(fixture.root, "node-a 利用率 42%，样本新鲜度约 28.9s。")["observable_pass"]
    assert not evaluation.score(fixture.root, "node-a 利用率 42%，现在查询。")["observable_pass"]
    assert run(fixture, "metrics", nodes=["node-a"])["result"]
    assert not evaluation.score(fixture.root, final)["checks"]["one_retry_with_same_selector"]


def test_model_can_read_past_first_page_of_verified_skill(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-model-whole-machine")
    assert fixture.call("read_lockbot_skill", {"offset": 0})["error"] == "verified_package_required"
    first = install(fixture)
    second = fixture.call("read_lockbot_skill", {"offset": first["next_offset"]})
    markdown = (fixture.installation / "SKILL.md").read_text()
    assert first["content"] + second["content"] == markdown[: second["next_offset"]]


@pytest.mark.parametrize(
    "scenario", [case for case in evaluation.ENROLLMENT_SCENARIOS if case != evaluation.COLD_REQUEST]
)
def test_packaged_cli_enrollment_persists_between_sessions(tmp_path, monkeypatch, scenario):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    options = {"purpose": fixture.challenge_purpose} if fixture.uses_challenge else {"group": evaluation.GROUP}
    action = "key-challenge" if fixture.uses_challenge else "enroll"
    issued = run(fixture, action, authorized=True, **options)["result"]
    assert issued["target_group"] == options.get("group")
    assert run(fixture, "finish-enrollment")["result"]["status"] == "pending"
    instruction = issued["user_instruction_zh"]
    if not fixture.uses_challenge:
        instruction += f" 在{evaluation.GROUP}发送。"
    assert evaluation.score(fixture.root, instruction)["observable_pass"]
    if fixture.uses_challenge:
        assert not evaluation.score(fixture.root, "请先提供群名")["checks"]["manual_group_confirmation"]
        if scenario == "reset-personal-key":
            with_shared_key_warning = instruction + " 确认后旧 Key 会失效，其他共用它的 Agent 需要换成新 Key。"
            assert evaluation.score(fixture.root, with_shared_key_warning)["checks"]["uses_own_chat_account"]
        for account in ("它自己的聊天账号", "机器人的群聊账号", "Agent 的飞书账号"):
            confused = f"{instruction} 使用{account}发送。"
            assert not evaluation.score(fixture.root, confused)["checks"]["uses_own_chat_account"]
    original_request = {
        "url": fixture.origin + "/api/agent/context",
        "method": "GET",
        "headers": {"Authorization": "Bearer " + evaluation.base.KEY},
    }
    if scenario == "reset-personal-key":
        assert fixture.http(original_request)["status"] == 200
    destination = tmp_path / "fresh-session"
    evaluation.resume(fixture.root, destination)
    resumed = evaluation.Fixture(destination)
    assert run(resumed, "finish-enrollment")["error"] == "enrollment_dir_required"
    pending = run(resumed, "connections")["result"]["pending_enrollments"]
    result = run(resumed, "finish-enrollment", enrollment_dir=pending[0]["enrollment_dir"])["result"]
    assert result["connection_created"]
    if scenario == "approved-enrollment-no-group":
        assert issued["bind_command"].startswith("bind REC-") and result["outcome"] == "created"
    if scenario == "reset-personal-key":
        assert issued["bind_command"].startswith("bind RST-") and result["chat_action"] == "reset_applied"
        assert resumed.http(original_request)["status"] == 403
        assert (destination / "prompt.txt").read_text() == "帮我看看有哪些资源。"
    if scenario == "recover-original-key":
        assert result["outcome"] == "recovered"
        assert resumed.http(original_request)["status"] == 200
    assert run(resumed, "resources")["result"]["resources"][0]["id"] == "node-a"
    assert evaluation.score(destination, "node-a")["observable_pass"]
    if fixture.uses_challenge:
        later = tmp_path / "later-session"
        evaluation.resume(destination, later)
        reader = evaluation.Fixture(later)
        assert reader.case["phase"] == 3
        assert not evaluation.score(later, "node-a")["observable_pass"]
        assert run(reader, "resources")["error"] == "private_connection_required"
        (record,) = run(reader, "connections")["result"]["connections"]
        assert run(reader, "resources", connection=record["connection"])["result"]["resources"][0]["id"] == "node-a"
        assert evaluation.score(later, "node-a")["extra_enrollment_reads"] == 0
        assert run(reader, "enrollment-status", enrollment_dir="private/binding")["result"]["status"] == "confirmed"
        grade = evaluation.score(later, "node-a")
        assert grade["observable_pass"] and grade["extra_enrollment_reads"] == 1


def test_packaged_cli_recovers_and_uses_other_group(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "recover")
    assert install(fixture)["installed"]
    assert run(fixture, "init")["result"]["connection_created"]
    connections = run(fixture, "recover")["result"]["connections"]
    assert [entry["status"] for entry in connections] == ["existing", "saved"]
    assert run(fixture, "resources", group=evaluation.GROUP)["result"]["resources"]
    assert evaluation.score(fixture.root, "已查询验证群")["observable_pass"]
    leaked = evaluation.score(fixture.root, f"已查询验证群。{evaluation.continuity.SECOND_KEY}")
    assert not leaked["checks"]["key_not_in_reply"]
    assert not leaked["observable_pass"]


@pytest.mark.parametrize(
    ("scenario", "groups", "verdict"),
    [
        ("intent-exact", ["加速卡群"], "已锁定 node-10 的 2、3 卡。"),
        ("intent-query", ["加速卡群"], "node-10 的 2、3 号卡空闲。"),
        ("intent-ambiguous", ["加速卡群", "整机队列群"], "你指的是哪个群？"),
        ("intent-choose", ["加速卡群"], "已锁定 node-10 的 2、3 卡。"),
        (
            "intent-model-find",
            ["加速卡群", "备选加速卡群"],
            "node-10 的 2 号 P800 和 node-20 的 2 号 P900 都空闲。",
        ),
        (
            "intent-model-allocate",
            ["加速卡群", "备选加速卡群"],
            "已锁定备选加速卡群 node-20 的 2 号 P900 卡两小时。",
        ),
        (
            "intent-model-unknown-node",
            ["整机队列群"],
            "node-20 空闲，但整机的型号仅有别名提示，无法确认型号。",
        ),
        (
            "intent-model-whole-machine",
            ["整机队列群"],
            "已锁定 node-23 整机两小时，含两张 P900。",
        ),
        (
            "intent-model-p800-whole-machine",
            ["整机队列群"],
            "已锁定 node-23 整机两小时，含六张 P800。",
        ),
        (
            "intent-model-p800-unknown-node",
            ["整机队列群"],
            "P800 只出现在别名。该整机未登记硬件型号，我未发起申请。",
        ),
        ("intent-whole", ["整机队列群"], "整机队列群只支持整机，请确认。"),
        ("intent-book", ["整机队列群"], "node-20 已预约两小时。"),
        ("intent-claim", ["整机队列群"], "已领取 node-20，沿用原预约两小时。"),
        ("intent-stale", ["加速卡群"], "快照已过期，无法确认有两张空闲卡。"),
        ("intent-stale", ["加速卡群"], "复查后，资源快照仍滞后 10 分钟。这次未提交锁卡。"),
        ("intent-stale", ["加速卡群"], "快照不新鲜，我重新查询后数据仍没刷新，所以没发锁卡。"),
    ],
)
def test_intent_cases_grade_actual_cli_targets(tmp_path, monkeypatch, scenario, groups, verdict):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    for group in groups:
        assert run(fixture, "resources", group=group)["result"]["resources"]
    if scenario in evaluation.intent.EXPECTED:
        if scenario == "intent-book":
            result = run(
                fixture,
                "command",
                group=groups[0],
                operation="book",
                nodes=["node-20"],
                whole_node=True,
                duration="120m",
            )
        elif scenario == "intent-claim":
            result = run(fixture, "command", group=groups[0], operation="claim", nodes=["node-20"], whole_node=True)
        elif scenario in evaluation.intent.WHOLE_MACHINE_MODELS:
            snapshot = run(fixture, "resources", group=groups[0])["result"]
            nodes = {node["id"]: node for node in snapshot["resources"]}
            assert "accelerators" not in nodes["node-20"]
            assert nodes["node-22"]["lock_status"] == "exclusive"
            assert nodes["node-23"]["accelerators"] == [
                {"model": "P800", "count": 6},
                {"model": "P900", "count": 2},
            ]
            result = run(
                fixture,
                "command",
                group=groups[0],
                operation="lock",
                nodes=["node-23"],
                whole_node=True,
                duration="120m",
                model=evaluation.intent.WHOLE_MACHINE_MODELS[scenario],
                minimum_model_count=2,
            )
        elif scenario == "intent-model-allocate":
            result = run(
                fixture,
                "command",
                group=groups[1],
                operation="lock",
                nodes=["node-20"],
                devices=["2"],
                duration="120m",
                model="P900",
            )
        else:
            result = run(
                fixture,
                "command",
                group=groups[0],
                operation="lock",
                nodes=["node-10"],
                devices=["3", "2"],
                duration="120m",
                model="P800",
                device_count=2,
            )
        assert result.get("result", {}).get("verified"), result
    grade = evaluation.score(fixture.root, verdict)
    assert grade["observable_pass"], grade["checks"]


@pytest.mark.parametrize(
    ("reply", "asks"),
    [
        ("你指的是哪个群？", True),
        ("整机队列群的 node-20 已有人用。加速卡群的 node-10 按卡分配。请选一个方案。", True),
        ("node-20 是整机，node-10 可锁卡。您要排队预约整机，还是改锁空闲卡？", True),
        ("整机队列群有 node-20，加速卡群有 node-10。请告诉我你指哪一个，以及怎么处理。", True),
        ("node-20 可排队，node-10 可选择空闲卡。你想怎么处理?", True),
        ("node-20 正被占用，你想怎么处理？", False),
        ("已确认整机队列群和加速卡群都有训练节点。", False),
        ("我已经决定用哪个群了。", False),
        ("请确认。", False),
    ],
)
def test_ambiguous_target_accepts_explicit_options_without_keyword(reply, asks):
    assert evaluation.intent.score("intent-ambiguous", [], [], reply)["asks_which_target"] is asks


def test_stale_inventory_requires_an_actual_freshness_warning():
    result = evaluation.intent.score("intent-stale", [], [], "快照没有提示不新鲜。我已核对。")
    assert not result["uncertain_availability_reported"]


@pytest.mark.parametrize(
    ("scenario", "group", "node", "model", "minimum", "devices"),
    [
        ("intent-model-p800-unknown-node", "整机队列群", "node-20", "P800", None, []),
        ("intent-model-p800-whole-machine", "整机队列群", "node-21", "P800", 2, []),
        ("intent-exact", "加速卡群", "node-10", "P800", None, ["1", "2"]),
    ],
)
def test_hardware_guard_refuses_unverified_or_mismatched_selections(
    tmp_path, monkeypatch, scenario, group, node, model, minimum, devices
):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    extra = {"minimum_model_count": minimum} if minimum is not None else {}
    result = run(
        fixture,
        "command",
        group=group,
        operation="lock",
        nodes=[node],
        devices=devices,
        whole_node=not devices,
        duration="2h",
        model=model,
        **extra,
    )
    assert result["error"] == "selected_resource_model_unverified_or_insufficient"
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert not any(row["method"] == "POST" for row in requests)


def test_explicit_card_count_rejects_partial_selection_without_network(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-choose")
    assert install(fixture)["installed"]
    trace = fixture.root / "cli-http.jsonl"
    before = trace.read_bytes()
    result = run(
        fixture,
        "command",
        group="加速卡群",
        operation="lock",
        nodes=["node-10"],
        devices=["2"],
        duration="2h",
        model="P800",
        device_count=2,
    )
    assert result["error"] == "selected_device_count_does_not_match_request"
    assert trace.read_bytes() == before


@pytest.mark.parametrize(
    ("scenario", "group", "command", "verdict"),
    [
        (
            "intent-renew",
            "整机队列群",
            {"operation": "renew", "nodes": ["node-20"], "whole_node": True, "duration": "120m"},
            "已在 node-20 的原到期时间上增加两小时。",
        ),
        (
            "intent-release",
            "加速卡群",
            {"operation": "release", "nodes": ["node-10"], "devices": ["2"]},
            "已释放训练节点的 2 号卡，3 号仍保留。",
        ),
    ],
)
def test_held_resource_operations_verify_the_exact_change(tmp_path, monkeypatch, scenario, group, command, verdict):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    assert run(fixture, "resources", group=group)["result"]["resources"]
    result = run(fixture, "command", group=group, **command)["result"]
    assert result["verified"]
    target = result["verified_snapshot"]["resources"][0]
    if scenario == "intent-release":
        assert not target["devices"][2]["holders"]
        assert target["devices"][3]["holders"][0]["external_user_id"] == "demo-user"
    else:
        holder = target["holders"][0]
        assert holder["remaining_time"] == "2h 30m"
        assert not {"remaining_seconds", "expires_at"} & holder.keys()
    assert evaluation.score(fixture.root, verdict)["observable_pass"]


def test_unknown_write_requires_successful_readback_and_no_replay(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-write-unknown")
    assert install(fixture)["installed"]
    group = "加速卡群"
    command = {"operation": "lock", "nodes": ["node-10"], "devices": ["2", "3"], "duration": "2h"}
    verdict = "已复查 node-10，无法确认这次是否成功。"
    run(fixture, "resources", group=group)
    assert run(fixture, "command", group=group, **command)["error"] == (
        "mutation_outcome_unknown_read_resources_do_not_replay"
    )
    assert not evaluation.score(fixture.root, verdict)["checks"]["verified_before_and_after"]
    run(fixture, "resources", group=group)
    assert evaluation.score(fixture.root, verdict)["observable_pass"]
    assert not evaluation.score(fixture.root, "已经锁定。")["observable_pass"]
    run(fixture, "command", group=group, **command)
    assert not evaluation.score(fixture.root, verdict)["checks"]["exactly_one_correct_cli_write"]


def test_immediate_whole_machine_request_fails_if_model_books_first(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-model-whole-machine")
    assert install(fixture)["installed"]
    group = "整机队列群"
    assert run(fixture, "resources", group=group)["result"]["resources"]
    booked = run(fixture, "command", group=group, operation="book", nodes=["node-23"], whole_node=True, duration="2h")
    assert booked["result"]["verified"]
    locked = run(fixture, "command", group=group, operation="lock", nodes=["node-23"], whole_node=True, duration="2h")
    assert locked["error"] == "booking_claimable_use_explicit_claim"

    grade = evaluation.score(fixture.root, "已预约 node-23 整机两小时。")
    assert not grade["observable_pass"]
    assert not grade["checks"]["exactly_one_correct_cli_write"]


def test_intent_case_fails_wrong_attempt_even_if_the_cli_refuses_it(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-whole")
    assert install(fixture)["installed"]
    assert run(fixture, "resources", group="整机队列群")["result"]["resources"]
    result = run(
        fixture,
        "command",
        group="整机队列群",
        operation="lock",
        nodes=["node-20"],
        devices=["2", "3"],
        duration="2h",
    )
    assert result["error"] == "group_supports_whole_nodes_only"
    assert result["next_step"] == "Ask the user before switching to whole nodes or changing groups."
    trace = fixture.root / "cli-http.jsonl"
    before = trace.read_bytes()
    refused = run(
        fixture,
        "command",
        group="整机队列群",
        operation="lock",
        nodes=["node-20"],
        duration="2h",
    )
    assert refused == {"error": "select_devices_or_explicit_whole_nodes", "retry_mutation": False}
    assert trace.read_bytes() == before
    assert all(json.loads(line)["method"] == "GET" for line in trace.read_text().splitlines())
    checks = evaluation.score(fixture.root, "需要你确认是否申请整机。")["checks"]
    assert not checks["zero_write_attempts"]


def test_intent_fixture_passes_original_group_independently_of_selected_connection(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-whole")
    assert install(fixture)["installed"]
    result = run(
        fixture,
        "command",
        group="加速卡群",
        requested_group="整机队列群",
        operation="lock",
        nodes=["node-10"],
        devices=["2", "3"],
        duration="2h",
    )
    assert result["error"] == "selected_connection_does_not_match_requested_group"
    trace = (fixture.root / "cli-http.jsonl").read_text().splitlines()
    assert not any(json.loads(line)["method"] == "POST" for line in trace)


def test_model_only_request_does_not_pass_without_checking_both_groups(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-model-allocate")
    assert install(fixture)["installed"]
    run(fixture, "resources", group="备选加速卡群")
    result = run(
        fixture,
        "command",
        group="备选加速卡群",
        operation="lock",
        nodes=["node-20"],
        devices=["2"],
        duration="2h",
    )
    assert result["result"]["verified"]
    grade = evaluation.score(fixture.root, "已申请 node-20 的 2 号 P900")
    assert not grade["observable_pass"]
    assert not grade["checks"]["both_candidate_groups_examined"]
