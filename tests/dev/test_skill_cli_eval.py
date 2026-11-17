"""Exercise the model fixture with the production bundle and no model service."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill
from lockbot.backend.app.resource_api.snapshot import build_resources

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
    (artifact / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    if instance_id is not None:
        manifest = {**manifest, "instance_id": instance_id}
    evaluation.base.save_json(artifact / "manifest.json", manifest)
    case = tmp_path / scenario
    evaluation.prepare(case, artifact, scenario)
    return evaluation.Fixture(case)


def install(fixture):
    # Day-to-day tasks start with a verified, installed Skill. Do not add a
    # simulated installation call just to satisfy the old test setup.
    if fixture.installation.is_dir():
        assert all(
            (fixture.installation / name).read_bytes() == content
            for name, content in fixture.artifact["contents"].items()
        )
        return {"installed": True}
    return fixture.call("install_lockbot", {"url": fixture.origin + evaluation.base.BUNDLE_PATH})


def run(fixture, action, **kwargs):
    return fixture.call("run_lockbot", {"action": action, **kwargs})


@pytest.mark.parametrize("bot", [7, 8])
def test_booking_fixture_matches_public_api_without_lease_fields(tmp_path, monkeypatch, bot):
    fixture = prepare(tmp_path, monkeypatch, "intent-my-books")
    assert fixture.case["fixture_version"] == 16
    result = run(fixture, "resources", group=evaluation.intent.CARDS if bot == 7 else evaluation.intent.MACHINES)
    resources = result["result"]["resources"]
    booked = resources[0]["devices"][2] if bot == 7 else resources[1]
    now = datetime.now(timezone.utc)
    # Actual allocator state may carry requested duration and notification time.
    # Neither becomes an unclaimed booking's lease expiry in the public payload.
    state = {
        "status": "exclusive",
        "current_users": [{"user_id": "neighbor", "start_time": int(now.timestamp()), "duration": 7200}],
        "booking_list": [{"user_id": "demo-user", "start_time": int(now.timestamp()), "duration": 14400}],
    }
    actual = build_resources(
        captured_at=now,
        state={"machine": [{**state, "dev_id": 3}] if bot == 7 else state},
        bot_type="QUEUE_DEVICE" if bot == 7 else "QUEUE",
        platform="infoflow",
        caller_id="demo-user",
    )[0]
    actual = actual["devices"][0] if bot == 7 else actual
    assert booked["booking_list"] == actual["booking_list"]
    own = resources[0]["devices"][1] if bot == 7 else resources[0]
    assert own["holders"][0]["remaining_time"] == "2h"
    assert set(booked["booking_list"][0]) == {"position", "external_user_id", "source", "notified"}


@pytest.mark.parametrize(
    ("scenario", "command"),
    [
        ("intent-book-tail", evaluation.intent.EXPECTED["intent-book-tail"][1]),
        ("intent-model-p800-whole-machine", evaluation.intent.WHOLE_MACHINE_BOOKING),
        ("intent-claim", ""),
        ("intent-book", evaluation.intent.EXPECTED["intent-book"][1]),
    ],
)
def test_all_intent_booking_builders_preserve_legacy_replay_without_fabricating_new_expiry(scenario, command):
    historical = evaluation.intent.inventory(8, command, scenario=scenario, fixture_version=15)
    current = evaluation.intent.inventory(8, command, scenario=scenario, fixture_version=16)
    old_rows = [row for node in historical["resources"] for row in node.get("booking_list", [])]
    new_rows = [row for node in current["resources"] for row in node.get("booking_list", [])]
    assert old_rows and new_rows
    assert all("remaining_seconds" in row and "expires_at" in row for row in old_rows)
    assert all(set(row) == {"position", "external_user_id", "source", "notified"} for row in new_rows)
    assert [row["external_user_id"] for row in old_rows] == [row["external_user_id"] for row in new_rows]


def test_historical_cli_fixture_retains_old_booking_payload(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-my-books")
    evaluation.base.save_json(fixture.root / "case.json", {**fixture.case, "fixture_version": 15})
    replay = evaluation.Fixture(fixture.root)
    snapshot = run(replay, "resources", group=evaluation.intent.MACHINES)["result"]
    assert "expires_at" in snapshot["resources"][1]["booking_list"][0]


@pytest.mark.parametrize(
    ("scenario", "operation", "nodes", "duration", "final"),
    [
        ("intent-lock-tail", "lock", ["node-19"], None, "19 已锁三小时。"),
        (
            "intent-lock-tail-list",
            "lock",
            ["node-22", "node-21", "node-20", "node-19"],
            "3h",
            "四台机器都已锁三小时。",
        ),
        (
            "intent-lock-tail-list-4h",
            "lock",
            ["node-11", "node-12", "node-13", "node-14"],
            "240m",
            "四台机器都已锁四小时。",
        ),
        ("intent-book-tail", "book", ["node-19"], "4h", "已预约 19，使用四小时。"),
        ("intent-shared-tail", "slock", ["node-19"], "4h", "19 的 0 号卡已共享锁定四小时。"),
        ("intent-lock-duration-only", None, [], None, "你要锁哪台机器？"),
        ("intent-find-p800", None, [], None, "node-23 是空闲的 P800 整机。"),
        ("intent-ambiguous-tail", None, [], None, "加速卡群还是整机队列群，你想锁哪个群里的机器？"),
    ],
)
def test_short_prompts_use_saved_environment(tmp_path, monkeypatch, scenario, operation, nodes, duration, final):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    prompt = (fixture.root / "prompt.txt").read_text()
    assert prompt == evaluation.intent.SHORT_PROMPTS[scenario]
    assert not (fixture.root / "cli-http.jsonl").exists()
    groups = run(fixture, "connections")["result"]["groups"]
    for group in groups:
        assert run(fixture, "resources", group=group)["result"]["resources"]

    if operation:
        group = evaluation.intent.CARDS if operation == "slock" else evaluation.intent.MACHINES
        kwargs = {"devices": ["0"]} if operation == "slock" else {"whole_node": True}
        if duration is not None:
            kwargs["duration"] = duration
        if scenario == "intent-lock-tail-list":
            rules = run(fixture, "context", group=group)["result"]["bot_rules"]
            assert rules["default_duration_seconds"] == 10_800
        response = run(fixture, "command", group=group, operation=operation, nodes=nodes, **kwargs)
        assert response.get("result", {}).get("verified"), response
        if duration is None:
            assert response["result"]["duration_source"] == "group-default"

    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade["checks"]


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
    grade = evaluation.score(fixture.root, "装好了。下次告诉我需要哪台机器，需要确认身份时我会提示你。")
    assert grade["observable_pass"]
    assert grade["manual_review_required"]
    assert not evaluation.score(fixture.root, "")["observable_pass"]


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
    assert Path(events[-1]["argv"][0]).name == "lockbot_cli.py"
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
        assert checks["service_saved"]
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
    fixture = prepare(tmp_path, monkeypatch, "no-key")
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
    if scenario == "reset-personal-key":
        warning = "重置后其他 Agent 会失效，需要重新配置。确认重置吗？"
        assert evaluation.score(fixture.root, warning)["observable_pass"]
        evaluation.base.save_json(fixture.root / "response.json", {"text": warning})
        acknowledged = tmp_path / "reset-acknowledgment"
        evaluation.resume(fixture.root, acknowledged)
        fixture = evaluation.Fixture(acknowledged)
        assert (acknowledged / "prompt.txt").read_text() == "行，重置吧"
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
        assert reader.case["phase"] == (4 if scenario == "reset-personal-key" else 3)
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
        ("整机队列群有 node-20，加速卡群有 node-10。你要哪一个？若选加速卡群，请指定卡号。", True),
        ("19 matches resources in both 加速卡群 and 整机队列群. Which group should I use for the 4-hour lock?", True),
        ("Which group should I use?", True),
        ("Which node do you want?", True),
        ("你要哪一个？", False),
        ("node-20 正被占用，你想怎么处理？", False),
        ("已确认整机队列群和加速卡群都有训练节点。", False),
        ("我已经决定用哪个群了。", False),
        ("Both 加速卡群 and 整机队列群 contain matching resources.", False),
        ("I know which group I should use.", False),
        ("I already decided which group I should use. Is that clear?", False),
        ("整机队列群有 node-20，加速卡群有 node-10。你要哪一个，我已经知道了。", False),
        ("请确认。", False),
    ],
)
@pytest.mark.parametrize("scenario", ["intent-ambiguous", "intent-ambiguous-tail"])
def test_ambiguous_target_accepts_explicit_options_without_keyword(reply, asks, scenario):
    assert evaluation.intent.score(scenario, [], [], reply)["asks_which_target"] is asks


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
    before = trace.read_bytes() if trace.is_file() else b""
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
    assert (trace.read_bytes() if trace.is_file() else b"") == before


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


@pytest.mark.parametrize(
    ("scenario", "reply"),
    [
        ("intent-my-locks", "你当前占用 node-10 的 2 号卡，以及 node-20 整机。"),
        ("intent-my-books", "你预约了 node-10 的 3 号卡，以及 node-21 整机。"),
        ("intent-reminder-configured", "node-19 已有群内提前提醒；我没有新增提醒或修改群设置。"),
        ("intent-reminder-unavailable", "当前只能查看到期状态，不能通过此接口新建提醒；我没有设置提醒。"),
    ],
)
def test_own_resources_and_reminder_requests_are_read_only(tmp_path, monkeypatch, scenario, reply):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert (fixture.root / "prompt.txt").read_text() == evaluation.intent.OWN_RESOURCE_PROMPTS[scenario]
    for group in evaluation.intent.GROUP_IDS:
        assert run(fixture, "resources", group=group)["result"]["actor"]["external_user_id"] == "demo-user"
    grade = evaluation.score(fixture.root, reply)
    assert grade["observable_pass"], grade
    if scenario.startswith("intent-reminder-"):
        assert grade["manual_review_required"]
    if scenario == "intent-my-locks":
        assert not evaluation.score(fixture.root, "你占用了 node-10、node-20、node-21 和 node-22。2 号卡也在用。")[
            "observable_pass"
        ]
    if scenario == "intent-my-books":
        assert not evaluation.score(fixture.root, "你预约了 node-10 的3号卡、node-20 和 node-21。")["observable_pass"]


@pytest.mark.parametrize(
    "scenario", [s for s in evaluation.intent.CONVERSATIONAL_PROMPTS if s not in evaluation.intent.INSTALL_FOLLOWUPS]
)
def test_contextual_followup_preserves_observed_conversation(tmp_path, monkeypatch, scenario):
    first = prepare(tmp_path, monkeypatch, scenario)
    for group in evaluation.intent.GROUP_IDS:
        assert run(first, "resources", group=group)["result"]
    expected = evaluation.intent.task_expectation(scenario, 1)
    first_reply = "node-23 是空闲的 P800 整机。"
    if expected:
        assert run(
            first,
            "command",
            group=evaluation.intent.MACHINES,
            operation="lock",
            nodes=["node-19"],
            whole_node=True,
            duration="2h",
        )["result"]["verified"]
        first_reply = "node-19 已锁两小时。"
    assert evaluation.score(first.root, first_reply)["observable_pass"]
    evaluation.base.save_json(first.root / "response.json", {"text": first_reply})
    second_path = tmp_path / "followup"
    evaluation.resume(first.root, second_path)
    second = evaluation.Fixture(second_path)
    prompt = (second_path / "prompt.txt").read_text()
    assert prompt == evaluation.intent.CONVERSATIONAL_PROMPTS[scenario][1]
    assert "node-" not in prompt and "19" not in prompt and "23" not in prompt
    assert json.loads((second_path / "conversation.json").read_text()) == [
        {"role": "user", "content": (first.root / "prompt.txt").read_text()},
        {"role": "assistant", "content": first_reply},
    ]
    for group in evaluation.intent.GROUP_IDS:
        assert run(second, "resources", group=group)["result"]
    bot, command, operation = evaluation.intent.task_expectation(scenario, 2)
    node = command.split()[1]
    kwargs = {} if operation == "release" else {"duration": "1d" if scenario.endswith("lock-day") else "2h"}
    result = run(
        second,
        "command",
        group=evaluation.intent.MACHINES,
        operation=operation,
        nodes=[node],
        whole_node=True,
        **kwargs,
    )
    assert result["result"]["verified"], result
    assert evaluation.score(second_path, node + " 操作完成。")["observable_pass"]
    # The other resource owned by this user is not the target of the follow-up.
    if not scenario.endswith("lock-day"):
        snapshot = run(second, "resources", group=evaluation.intent.MACHINES)["result"]
        other = next(row for row in snapshot["resources"] if row["id"] == "node-20")
        assert other["holders"][0]["external_user_id"] == "demo-user"


@pytest.mark.parametrize("target", ["node-20", "node-21"])
def test_contextual_renew_rejects_another_target(tmp_path, monkeypatch, target):
    fixture = prepare(tmp_path, monkeypatch, "intent-context-renew")
    for group in evaluation.intent.GROUP_IDS:
        run(fixture, "resources", group=group)
    run(
        fixture,
        "command",
        group=evaluation.intent.MACHINES,
        operation="lock",
        nodes=["node-19"],
        whole_node=True,
        duration="2h",
    )
    evaluation.base.save_json(fixture.root / "response.json", {"text": "node-19 已锁两小时。"})
    second_path = tmp_path / "followup"
    evaluation.resume(fixture.root, second_path)
    second = evaluation.Fixture(second_path)
    for group in evaluation.intent.GROUP_IDS:
        run(second, "resources", group=group)
    result = run(
        second,
        "command",
        group=evaluation.intent.MACHINES,
        operation="renew",
        nodes=[target],
        whole_node=True,
        duration="2h",
    )
    assert not result.get("result", {}).get("verified")
    assert not evaluation.score(second.root, target + " 已续期。")["observable_pass"]


def test_runner_must_acknowledge_native_prior_turns(tmp_path):
    root = tmp_path / "phase"
    root.mkdir()
    evaluation.base.save_json(
        root / "conversation.json",
        [
            {"role": "user", "content": "帮我找台P800的机器"},
            {"role": "assistant", "content": "node-23 是空闲的P800整机。"},
        ],
    )
    (root / "prompt.txt").write_text("lock 一天")
    (root / "system.txt").write_text("Synthetic test")
    evaluation.base.save_json(root / "mcp.json", {"mcpServers": {}})
    runner = tmp_path / "adapter.py"
    runner.write_text("import json,sys; r=json.load(sys.stdin); print(json.dumps({'text':'完成'}))")

    def grade(*_):
        return {"observable_pass": True}

    result = evaluation.base.evaluate(root, [sys.executable, str(runner)], "synthetic", grade, timeout=5)
    assert result["status"] == "unsupported_context" and not result["observable_pass"]
    runner.write_text(
        "import json,sys; r=json.load(sys.stdin); "
        "assert json.load(open(r['conversation_file']))[0]['role']=='user'; "
        "print(json.dumps({'text':'完成','conversation_sha256':r['conversation_sha256']}))"
    )
    result = evaluation.base.evaluate(root, [sys.executable, str(runner)], "synthetic", grade, timeout=5)
    assert result["observable_pass"]


@pytest.mark.parametrize("coherent", [True, False])
def test_frozen_cli_exercises_coherent_and_legacy_service_protocols(tmp_path, monkeypatch, coherent):
    fixture = prepare(tmp_path, monkeypatch, "intent-lock-tail")
    assert "coherent_resource_snapshot" not in fixture.artifact["manifest"].get("capabilities", [])
    fixture.case["coherent_snapshot"] = coherent
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    assert ("coherent_resource_snapshot" in fixture.manifest()["capabilities"]) is coherent
    trace = fixture.root / "cli-http.jsonl"
    for group in evaluation.intent.GROUP_IDS:
        before = len(trace.read_text().splitlines()) if trace.exists() else 0
        assert run(fixture, "resources", group=group)["result"]["resources"]
        requests = [json.loads(line) for line in trace.read_text().splitlines()][before:]
        assert len(requests) == (2 if coherent else 3)
        assert sum(row["url"].endswith("/api/agent/context") for row in requests) == (0 if coherent else 1)
    before = len(trace.read_text().splitlines())
    result = run(
        fixture, "command", group=evaluation.intent.MACHINES, operation="lock", nodes=["node-19"], whole_node=True
    )
    assert result["result"]["verified"], result
    requests = [json.loads(line) for line in trace.read_text().splitlines()][before:]
    assert len(requests) == (3 if coherent else 5)
    post = next(index for index, row in enumerate(requests) if row["method"] == "POST")
    assert bool(requests[post].get("resource_observation")) is coherent
    assert sum(row["method"] == "GET" for row in requests[post + 1 :]) == (0 if coherent else 1)
    grade = evaluation.score(fixture.root, "node-19 已锁三小时。")
    assert grade["observable_pass"], grade
    if coherent:
        # An arbitrary CLI claim is insufficient: it must match the response actually served.
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        next(row for row in rows if row["method"] == "POST")["resource_observation"]["captured_at"] = "forged"
        trace.write_text("".join(json.dumps(row) + "\n" for row in rows))
        assert not evaluation.score(fixture.root, "node-19 已锁三小时。")["checks"]["verified_before_and_after"]


def test_advertised_coherent_fixture_does_not_downgrade_missing_observation(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-lock-tail")
    original = fixture.http

    def missing(args):
        result = original(args)
        if args["method"] == "POST" and args["url"].endswith("/commands") and result["status"] == 200:
            result["body"].pop("resource_snapshot", None)
        return result

    monkeypatch.setattr(fixture, "http", missing)
    result = run(
        fixture, "command", group=evaluation.intent.MACHINES, operation="lock", nodes=["node-19"], whole_node=True
    )
    assert result["error"] == "mutation_not_confirmed_read_resources_do_not_replay"
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    post = next(index for index, row in enumerate(requests) if row["method"] == "POST")
    assert not any(row["method"] == "GET" for row in requests[post + 1 :])


def test_reset_cannot_issue_challenge_or_simulate_ack_before_warning(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "reset-personal-key")
    assert not fixture.case.get("reset_impact_acknowledged")
    assert not run(fixture, "key-challenge", purpose="reset", authorized=True).get("result")
    assert not (fixture.workspace / "private/binding/enrollment.json").exists()
    assert not evaluation.score(fixture.root, "已申请重置码。")["observable_pass"]
    evaluation.base.save_json(fixture.root / "response.json", {"text": "好，我来重置。"})
    with pytest.raises(ValueError, match="impact"):
        evaluation.resume(fixture.root, tmp_path / "bad-ack")


@pytest.mark.parametrize(
    ("warning", "accepted"),
    [
        ("可以。重置后旧 Key 将失效，其他共用该 Key 的 Agent 也必须更换。确认要重置吗？", True),
        ("旧key将作废，其他Agent需要换成新Key。确定吗？", True),
        ("重置后其他 Agent 会失效，需要重新配置。确认重置吗？", True),
        ("其他共用该Key的Agent也必须更换。确认要重置吗？", False),
        ("重置后旧Key将失效。确认要重置吗？", False),
        ("旧Key不会失效，其他Agent无需重新配置。确认要重置吗？", False),
    ],
)
def test_reset_impact_accepts_plain_replacement_wording_without_omitting_consequences(warning, accepted):
    checks = evaluation.reset_impact_checks(warning)
    assert checks["reset_impact_explained"] is accepted
    assert checks["asks_reset_confirmation"]


@pytest.mark.parametrize(
    ("scenario", "wrong"),
    [
        ("intent-my-locks", "你占用 node-10 的3号卡和 node-20。"),
        ("intent-my-locks", "你占用 node-10 的4号卡和 node-20。"),
        ("intent-my-locks", "你占用 node-10 dev4 和 node-20。"),
        ("intent-my-locks", "你占用 node-100 和 node-200。"),
        ("intent-my-books", "你预约了 node-10 的2号卡和 node-21。"),
        ("intent-my-books", "你预约了 node-10 的4号卡和 node-21。"),
    ],
)
def test_personal_resource_grading_rejects_wrong_explicit_card_or_node_prefix(tmp_path, monkeypatch, scenario, wrong):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    for group in evaluation.intent.GROUP_IDS:
        run(fixture, "resources", group=group)
    result = evaluation.score(fixture.root, wrong)
    assert not result["observable_pass"]
    assert result["manual_review_required"]
    correct = "你占用 node-10 和 node-20。" if scenario == "intent-my-locks" else "你预约了 node-10 和 node-21。"
    assert evaluation.score(fixture.root, correct)["observable_pass"]


@pytest.mark.parametrize("scenario", list(evaluation.intent.INSTALL_FOLLOWUPS))
def test_install_only_then_next_task_reuses_the_actual_installation(tmp_path, monkeypatch, scenario):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert not fixture.installation.exists()
    assert (fixture.root / "prompt.txt").read_text() == "安装Skill: " + fixture.origin + "/api/agent-skill"
    assert install(fixture)["installed"]
    first = evaluation.score(fixture.root, "已安装。")
    assert first["observable_pass"], first
    evaluation.base.save_json(fixture.root / "response.json", {"text": "已安装。"})
    destination = tmp_path / "next-task"
    evaluation.resume(fixture.root, destination)
    second = evaluation.Fixture(destination)
    assert (destination / "prompt.txt").read_text() == evaluation.intent.INSTALL_FOLLOWUPS[scenario][1]
    assert json.loads((destination / "conversation.json").read_text())[-1]["content"] == "已安装。"
    assert all((second.installation / name).read_bytes() == data for name, data in second.artifact["contents"].items())
    if scenario != "intent-install-then-how-use":
        for group in evaluation.intent.GROUP_IDS:
            assert run(second, "resources", group=group)["result"]
    if scenario == "intent-install-then-lock":
        assert run(
            second,
            "command",
            group=evaluation.intent.MACHINES,
            operation="lock",
            nodes=["node-19"],
            whole_node=True,
            duration="2h",
        )["result"]["verified"]
    result = evaluation.score(
        destination, "node-19 操作完成。" if scenario.endswith("lock") else "可查询资源，使用lock申请机器。"
    )
    assert result["observable_pass"], result
    trace = (destination / "trace.jsonl").read_text() if (destination / "trace.jsonl").is_file() else ""
    assert '"tool": "install_lockbot"' not in trace


def test_minimal_shared_lock_does_not_require_a_card_number_in_the_user_prompt(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-shared-minimal")
    assert (fixture.root / "prompt.txt").read_text() == "slock 19 4h"
    snapshots = {group: run(fixture, "resources", group=group)["result"] for group in evaluation.intent.GROUP_IDS}
    target = next(node for node in snapshots[evaluation.intent.CARDS]["resources"] if node["id"] == "node-19")
    result = run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation="slock",
        nodes=[target["id"]],
        devices=[str(card["id"]) for card in target["devices"]],
        duration="4h",
    )
    assert result.get("result", {}).get("verified"), result
    assert evaluation.score(fixture.root, "19 已共享四小时。")["observable_pass"]


@pytest.mark.parametrize(
    "scenario", [s for s in evaluation.intent.IP_TAIL_PROMPTS if s not in evaluation.intent.PUBLIC_IP_ALIAS_PROMPTS]
)
def test_ip_tail_cases_use_only_current_authorized_address_evidence(tmp_path, monkeypatch, scenario):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    snapshots = {group: run(fixture, "resources", group=group)["result"] for group in evaluation.intent.GROUP_IDS}
    nodes = [node for snapshot in snapshots.values() for node in snapshot["resources"]]
    assert all("19" not in node["id"] and "19" not in node["alias"] for node in nodes)
    if scenario == "intent-ip-tail-unavailable":
        assert all(not node["holders"] and "node_access" not in node for node in nodes)
        result = evaluation.score(fixture.root, "当前没有可授权的IP映射，无法确定19是哪台，请提供节点名。")
    elif scenario == "intent-ip-tail-ambiguous":
        assert all(node["node_access"]["ip"].split(".")[-1] == "19" for node in nodes)
        result = evaluation.score(fixture.root, "两个授权群都有IP尾号19，你指哪台？")
    else:
        match = [node for node in nodes if node["node_access"]["ip"].split(".")[-1] == "19"]
        assert [node["id"] for node in match] == ["raven"]
        assert all(node["holders"][0]["external_user_id"] == "demo-user" for node in nodes)
        assert run(
            fixture,
            "command",
            group=evaluation.intent.MACHINES,
            operation="renew",
            nodes=[match[0]["id"]],
            whole_node=True,
            duration="4h",
        )["result"]["verified"]
        result = evaluation.score(fixture.root, "raven 已续四小时。")
    assert result["observable_pass"], result


@pytest.mark.parametrize("ambiguous", [False, True])
def test_idle_ip_tail_uses_public_ipv4_alias_without_private_node_access(tmp_path, monkeypatch, ambiguous):
    scenario = "intent-public-ip-alias-ambiguous" if ambiguous else "intent-public-ip-alias"
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert (fixture.root / "prompt.txt").read_text() == "lock 19"
    snapshots = {group: run(fixture, "resources", group=group)["result"] for group in evaluation.intent.GROUP_IDS}
    nodes = [(group, node) for group, snapshot in snapshots.items() for node in snapshot["resources"]]
    assert all("19" not in node["id"] and not node["holders"] and "node_access" not in node for _, node in nodes)
    assert all(node["alias"].count(".") == 3 for _, node in nodes)
    matches = [(group, node) for group, node in nodes if node["alias"].split(".")[-1] == "19"]
    if ambiguous:
        assert len(matches) == 2
        grade = evaluation.score(fixture.root, "两个资源群都有IP尾号19，指哪台？")
        assert grade["observable_pass"], grade
        assert grade["manual_review_required"]
        requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
        assert all(row["method"] == "GET" for row in requests)
    else:
        assert [(group, node["id"]) for group, node in matches] == [(evaluation.intent.MACHINES, "raven")]
        group, selected = matches[0]
        result = run(fixture, "command", group=group, operation="lock", nodes=[selected["id"]], whole_node=True)
        assert result.get("result", {}).get("verified"), result
        assert result["result"]["duration_source"] == "group-default"
        grade = evaluation.score(fixture.root, "raven 已锁三小时。")
        assert grade["observable_pass"], grade
        trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
        mutations = [row for row in trace if row["args"].get("action") == "command"]
        assert len(mutations) == 1 and mutations[0]["args"]["nodes"] == ["raven"]


def test_public_alias_119_does_not_satisfy_tail_19(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "intent-public-ip-alias")
    for group in evaluation.intent.GROUP_IDS:
        run(fixture, "resources", group=group)
    wrong = run(fixture, "command", group=evaluation.intent.CARDS, operation="lock", nodes=["cedar"], whole_node=True)
    assert not wrong.get("result", {}).get("verified")
    assert not evaluation.score(fixture.root, "cedar 已锁定。")["observable_pass"]


@pytest.mark.parametrize(
    "provenance",
    [
        "valid",
        "excluded_nonmatching",
        "bad_digest",
        "bad_reply",
        "wrong_actor",
        "failed_parent",
        "ambiguous_parent",
        "no_ack",
    ],
)
def test_followup_reuses_only_verified_prior_discovery(tmp_path, monkeypatch, provenance):
    first = prepare(tmp_path, monkeypatch, "intent-context-lock-day")
    for group in evaluation.intent.GROUP_IDS:
        run(first, "resources", group=group)
    answer = "node-23 是空闲的 P800 整机。"
    if provenance == "excluded_nonmatching":
        answer += "\nnode-24 也是空闲整机，但装的是8张P900，不是P800。"
    if provenance == "failed_parent":
        failed = run(
            first, "command", group=evaluation.intent.MACHINES, operation="lock", nodes=["node-23"], whole_node=True
        )
        assert failed.get("error")
    if provenance == "ambiguous_parent":
        answer = "node-23 和 node-24 都有P800，可以选择。"
    if provenance == "wrong_actor":
        path = first.root / "trace.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        for event in events:
            if event.get("args", {}).get("group") == evaluation.intent.CARDS:
                event["result"]["result"]["actor"]["external_user_id"] = "other-user"
                event["result"]["result"]["context"]["actor"]["external_user_id"] = "other-user"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    evaluation.base.save_json(first.root / "response.json", {"text": answer})
    destination = tmp_path / "followup"
    evaluation.resume(first.root, destination)
    second = evaluation.Fixture(destination)
    if provenance == "bad_digest":
        second.case["parent_evidence_digest"] = "0" * 64
        evaluation.base.save_json(destination / "case.json", second.case)
    if provenance == "bad_reply":
        history = json.loads((destination / "conversation.json").read_text())
        history[-1]["content"] = "node-24 是P800。"
        evaluation.base.save_json(destination / "conversation.json", history)
    result = run(
        second,
        "command",
        group=evaluation.intent.MACHINES,
        operation="lock",
        nodes=["node-23"],
        whole_node=True,
        duration="1d",
    )
    assert result["result"]["verified"], result
    final = "node-23 已锁一天。"
    response = {"text": final}
    if provenance != "no_ack":
        response["conversation_sha256"] = evaluation.base.digest((destination / "conversation.json").read_bytes())
    evaluation.base.save_json(destination / "response.json", response)
    grade = evaluation.score(destination, final)
    assert grade["checks"]["exactly_one_correct_cli_write"]
    assert grade["checks"]["verified_before_and_after"]
    assert grade["checks"]["both_candidate_groups_examined"] is (provenance in {"valid", "excluded_nonmatching"})
    assert grade["observable_pass"] is (provenance in {"valid", "excluded_nonmatching"})


@pytest.mark.parametrize(
    "evidence", ["recorded", "legacy_argv", "missing", "network_used", "repeated_command", "unknown_post_replay"]
)
def test_local_argument_correction_is_not_a_mutation_retry(tmp_path, monkeypatch, evidence):
    fixture = prepare(tmp_path, monkeypatch, "intent-release")
    run(fixture, "resources", group=evaluation.intent.CARDS)
    bad = run(fixture, "command", group=evaluation.intent.CARDS, operation="release", node="node-10", devices=["2"])
    assert bad["error"] == "invalid_cli_arguments"
    path = fixture.root / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    malformed = rows[-1]
    assert malformed["http_request_span"][0] == malformed["http_request_span"][1]
    if evidence in {"legacy_argv", "missing"}:
        malformed.pop("http_request_span")
    if evidence == "missing":
        malformed.pop("argv")
    if evidence == "network_used":
        malformed["http_request_span"][1] += 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    good = run(fixture, "command", group=evaluation.intent.CARDS, operation="release", nodes=["node-10"], devices=["2"])
    assert good["result"]["verified"]
    if evidence == "repeated_command":
        repeated = run(
            fixture, "command", group=evaluation.intent.CARDS, operation="release", nodes=["node-10"], devices=["2"]
        )
        assert repeated.get("result", {}).get("verified")
    if evidence == "unknown_post_replay":
        trace = fixture.root / "cli-http.jsonl"
        requests = [json.loads(line) for line in trace.read_text().splitlines()]
        previous = next(row for row in requests if row["method"] == "POST")
        requests.insert(requests.index(previous), {**previous, "status": 504})
        trace.write_text("".join(json.dumps(row) + "\n" for row in requests))
    grade = evaluation.score(fixture.root, "已释放node-10的2号卡，3号卡不变。")
    assert grade["observable_pass"] is (evidence in {"recorded", "legacy_argv"}), grade
    assert grade["cli_command_metrics"]["tool_attempts"] == (3 if evidence == "repeated_command" else 2)
    assert grade["cli_command_metrics"]["resource_posts"] == (
        2 if evidence in {"unknown_post_replay", "repeated_command"} else 1
    )


@pytest.mark.parametrize("extra", ["node-24是P800", "node-24", "node-24不是P800，而是P800和P900混合"])
def test_prior_candidate_exclusion_requires_verified_nonmatching_hardware_and_unambiguous_words(extra):
    machines = [
        {"id": "node-23", "accelerators": [{"model": "P800", "count": 8}]},
        {"id": "node-24", "accelerators": [{"model": "P900", "count": 8}]},
    ]
    candidates = evaluation.intent.contextual_p800_candidates("node-23是P800。\n" + extra, machines)
    assert candidates == {"node-23", "node-24"}
    machines[1]["accelerators"] = [{"model": "P800", "count": 8}]
    assert evaluation.intent.contextual_p800_candidates("node-23是P800。\nnode-24不是P800而是P900", machines) == {
        "node-23",
        "node-24",
    }


@pytest.mark.parametrize("hardware", [[], [{"model": "P900"}], [{"model": "P900", "count": 8}, {}]])
def test_prior_candidate_exclusion_rejects_incomplete_hardware_evidence(hardware):
    machines = [{"id": "node-23"}, {"id": "node-24", "accelerators": hardware}]
    assert evaluation.intent.contextual_p800_candidates("node-23是P800。\nnode-24是P900，不是P800", machines) == {
        "node-23",
        "node-24",
    }


@pytest.mark.parametrize(
    ("scenario", "reply", "passed"),
    [
        (
            "intent-my-locks",
            "你目前lock了：\n|群|机器|占用|\n|加速卡群|`node-10`|卡号`2`|\n|整机群|`node-20`|整机|\n"
            "另外你还有预约 `node-10` 的卡`3`和`node-21`，尚未持有。",
            True,
        ),
        (
            "intent-my-locks",
            "## 正在持有\n加速卡群(DEVICE)\n|node-10|卡 #2|\n整机队列群(QUEUE)\n|node-20|整机|\n"
            "## 另外预约\n- node-10 的3号卡，当前由neighbor持有\n- node-21，当前持有者是neighbor\n"
            "## 其他说明\n- node-10 卡 #4 属于demo-user-other，与你无关。",
            True,
        ),
        (
            "intent-my-books",
            "你book了：\n- node-10 的3号卡\n- node-21 整机\n另外你已lock持有 node-20 和 node-10 的2号卡，不属于排队。",
            True,
        ),
        (
            "intent-my-books",
            "## 预约（不是锁定）\n- node-10 的3号卡，当前由neighbor持有\n- node-21，当前由neighbor持有\n"
            "## 已锁定（不属于预约）\n- node-10 的2号卡\n- node-20 整机",
            True,
        ),
        ("intent-my-locks", "## 你持有\nnode-10 的3号卡和node-20\n## 另外预约\nnode-10 的3号卡和node-21", False),
        ("intent-my-locks", "## 你持有\n|node-10|卡 #4|\n|node-20|整机|\n## 另外预约\nnode-10卡3和node-21", False),
        ("intent-my-books", "## 你预约\nnode-10 的2号卡和node-21\n## 已持有\nnode-10 的2号卡和node-20", False),
        ("intent-my-locks", "你持有node-10 的2、4号卡和node-20", False),
    ],
)
def test_ownership_grading_distinguishes_explanatory_roles_and_explicit_contradictions(
    tmp_path, monkeypatch, scenario, reply, passed
):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    for group in evaluation.intent.GROUP_IDS:
        run(fixture, "resources", group=group)
    grade = evaluation.score(fixture.root, reply)
    assert grade["observable_pass"] is passed, grade
    assert grade["manual_review_required"]
