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
    (artifact / "lockbot-resource-agent.zip").write_bytes(skill.installation_bundle("agent"))
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
    inspected = fixture.call("inspect_lockbot", {"url": evaluation.base.ORIGIN + evaluation.base.BUNDLE_PATH})
    assert inspected["manifest"]["installation"]["sha256"]
    assert evaluation.score(fixture.root, "可以安装这个 Skill，方便以后使用。")["observable_pass"]

    assert install(fixture)["installed"]
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["installed"]


@pytest.mark.parametrize("scenario", ["install-from-link", "install-from-zip", "install-with-key"])
@pytest.mark.parametrize("instance_id", [evaluation.base.INSTANCE, "d709dca8-cd10-4602-a415-f480a63349be"])
def test_first_contact_requires_verified_install_and_complete_private_connection(
    tmp_path, monkeypatch, scenario, instance_id
):
    fixture = prepare(tmp_path, monkeypatch, scenario, instance_id)
    assert not evaluation.score(fixture.root, "已完成。")["observable_pass"]
    path = fixture.manifest()["skill_url"] if scenario == "install-from-link" else evaluation.base.BUNDLE_PATH
    assert "manifest" in fixture.call("inspect_lockbot", {"url": evaluation.base.ORIGIN + path})
    assert install(fixture)["installed"]
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["service_saved"]
    if scenario == "install-with-key":
        assert run(fixture, "init")["result"]["connection_created"]
        assert run(fixture, "resources")["result"]["resources"]
    else:
        assert run(fixture, "discover")["result"]["instance_id"] == instance_id
    grade = evaluation.score(fixture.root, "已安装、验证并保存连接。")
    assert grade["observable_pass"], grade["checks"]

    filename = "seed.json" if scenario == "install-with-key" else "service.json"
    saved_path = fixture.workspace / "private" / filename
    saved = json.loads(saved_path.read_text())
    evaluation.base.save_json(saved_path, {**saved, "instance_id": "wrong-instance"})
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["service_saved"]

    run(fixture, "enroll", group="验证群", authorized=True)
    assert not evaluation.score(fixture.root, "已安装。")["checks"]["no_extra_writes"]


def test_panel_link_cli_delivers_once_and_new_session_reads_fixed_home(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "panel")
    monkeypatch.setattr(evaluation.base, "tools", lambda: evaluation.tools(fixture.root))
    assert {"install_lockbot", "run_lockbot", "deliver_private_link", "browser_home"} <= {
        tool["name"] for tool in evaluation.base.tools()
    }
    assert install(fixture)["installed"]
    assert run(fixture, "init")["result"]["connection_created"]
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
    assert run(fixture, "init")["result"]["connection_created"]
    assert run(fixture, "resources")["result"]["resources"][0]["holders"]
    result = run(fixture, "metrics", nodes=["node-a"])["result"]
    assert result["verification"] == "query_response_only"
    samples = result["data"]["data"]["result"]
    assert any(sample["value"][1] == "42" for sample in samples)
    final = "node-a 的卡利用率为 42%，样本约 8 秒前采集。"
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade["checks"]

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


def test_packaged_cli_enrollment_persists_between_sessions(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "approved-enrollment")
    assert install(fixture)["installed"]
    issued = run(fixture, "enroll", group=evaluation.GROUP, authorized=True)["result"]
    assert run(fixture, "finish-enrollment")["result"]["status"] == "pending"
    assert evaluation.score(fixture.root, f"在{evaluation.GROUP}发送 {issued['bind_command']}")["observable_pass"]
    destination = tmp_path / "fresh-session"
    evaluation.resume(fixture.root, destination)
    resumed = evaluation.Fixture(destination)
    assert run(resumed, "finish-enrollment")["error"] == "enrollment_dir_required"
    pending = run(resumed, "connections")["result"]["pending_enrollments"]
    assert run(resumed, "finish-enrollment", enrollment_dir=pending[0]["enrollment_dir"])["result"][
        "connection_created"
    ]
    assert run(resumed, "resources")["result"]["resources"][0]["id"] == "node-a"
    assert evaluation.score(destination, "node-a")["observable_pass"]


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
    ],
)
def test_intent_cases_grade_actual_cli_targets(tmp_path, monkeypatch, scenario, groups, verdict):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    assert install(fixture)["installed"]
    for group in groups:
        assert run(fixture, "resources", group=group)["result"]["resources"]
    if scenario in evaluation.intent.EXPECTED:
        if scenario == "intent-book":
            result = run(fixture, "command", group=groups[0], operation="book", nodes=["node-20"], duration="120m")
        elif scenario == "intent-claim":
            result = run(fixture, "command", group=groups[0], operation="claim", nodes=["node-20"])
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
            )
        assert result.get("result", {}).get("verified"), result
    grade = evaluation.score(fixture.root, verdict)
    assert grade["observable_pass"], grade["checks"]


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
        duration="2h",
        model=model,
        **extra,
    )
    assert result["error"] == "selected_resource_model_unverified_or_insufficient"
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert not any(row["method"] == "POST" for row in requests)


@pytest.mark.parametrize(
    ("scenario", "group", "command", "verdict"),
    [
        (
            "intent-renew",
            "整机队列群",
            {"operation": "renew", "nodes": ["node-20"], "duration": "120m"},
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
        assert target["holders"][0]["remaining_seconds"] == 9000
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
    booked = run(fixture, "command", group=group, operation="book", nodes=["node-23"], duration="2h")
    assert booked["result"]["verified"]
    locked = run(fixture, "command", group=group, operation="lock", nodes=["node-23"], duration="2h")
    assert locked["result"]["verified"]

    grade = evaluation.score(fixture.root, "已锁定 node-23 整机两小时，含两张 P900。")
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
    assert result["error"] == "explicit_device_selection_required"
    checks = evaluation.score(fixture.root, "需要你确认是否申请整机。")["checks"]
    assert not checks["zero_write_attempts"]


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
