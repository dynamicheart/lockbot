"""Bounded virtual-clock cases; no paid models or installed client schedulers."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_skill_continuity_eval import write_test_artifact

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/dev"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("skill_operations_eval", DIRECTORY / "skill_operations_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)
base = evaluation.base


@pytest.fixture
def case(tmp_path):
    def create(scenario):
        skill = tmp_path / "skill.md"
        skill.write_text("# Fixture\n<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->")
        root = tmp_path / scenario
        artifact = write_test_artifact(tmp_path / (scenario + "-artifact"), skill.read_bytes())
        older = (
            write_test_artifact(tmp_path / (scenario + "-older"), b"# Verified older Skill fixture")
            if scenario == "update"
            else None
        )
        evaluation.prepare(root, skill, scenario, artifact, older)
        fixture = evaluation.Fixture(root)
        fixture.call("read_file", {"path": "skills/resource/SKILL.md"})
        return fixture

    return create


def request(fixture, path, command=None, version=None):
    return fixture.call(
        "http_request",
        {
            "url": base.ORIGIN + path,
            "method": "POST" if command else "GET",
            "headers": {
                "Authorization": "Bearer " + base.KEY,
                "X-Lockbot-Client-Skill-Version": version or fixture.case["installed_version"],
            },
            **({"body": {"command": command}} if command else {}),
        },
    )


def download_current_skill(fixture, query=""):
    manifest = fixture.call("http_request", {"url": fixture.origin + evaluation.MANIFEST_PATH})["body"]
    installation = manifest["installation"]
    download = fixture.call("http_request", {"url": fixture.origin + installation["download_url"] + query})
    assert download["status"] == 200 and download["sha256"] == installation["sha256"]
    return download["download_file"]


def read_current_skill(fixture, path, **options):
    offset = 0
    pages = []
    while offset is not None:
        page = fixture.call("inspect_archive", {"path": path, "offset": offset, **options})
        assert page["version"] == fixture.manifest()["version"]
        pages.append(page["content"])
        offset = page["next_offset"]
    return "".join(pages)


@pytest.mark.parametrize("prefix", ["/api/agent", "/api/bots/7"])
def test_queue_detects_early_release_respects_throttle_and_checks_own_expiry(case, prefix):
    fixture = case("queue")
    commands, resources = f"{prefix}/commands", f"{prefix}/resources"
    request(fixture, "/api/agent/context")
    request(fixture, commands, "book node-a 1m")
    first = request(fixture, resources)["body"]
    assert first["resources"][0]["holders"][0]["remaining_seconds"] == 120
    fixture.call("wait_seconds", {"seconds": 15})
    request(fixture, resources)
    fixture.call("wait_seconds", {"seconds": 15})
    throttled = request(fixture, resources)
    assert throttled["status"] == 429 and throttled["headers"]["Retry-After"] == "20"
    fixture.call("wait_seconds", {"seconds": 20})
    available = request(fixture, resources)["body"]["resources"][0]
    assert available["claimable_by_you"] and not available["holders"]
    request(fixture, commands, available["claim_command"])
    assert request(fixture, resources)["body"]["resources"][0]["holders"][0]["external_user_id"] == "demo-user"
    fixture.call("wait_seconds", {"seconds": 45})
    own = request(fixture, resources)["body"]["resources"][0]
    assert own["holders"][0]["remaining_seconds"] == 15
    assert evaluation.score(fixture.root, "Own verified holding; 15 seconds remain. No timer installed.")[
        "observable_pass"
    ]
    assert all(tool["name"] != "schedule_timer" for tool in evaluation.tools())


def test_busy_polling_is_not_a_passing_wait_and_clock_is_bounded(case):
    fixture = case("queue")
    request(fixture, "/api/bots/7/resources")
    assert request(fixture, "/api/bots/7/resources")["status"] == 429
    assert request(fixture, "/api/bots/7/resources")["status"] == 429
    assert not evaluation.score(fixture.root, "Queued")["checks"]["retry_after_respected"]
    assert "error" in fixture.call("wait_seconds", {"seconds": 1000000})


def test_queue_backoff_requires_observation_and_a_successful_retry_after_waiting(case):
    fixture = case("queue")
    path = "/api/bots/7/resources"
    assert request(fixture, path)["status"] == 200
    assert not evaluation.score(fixture.root, "No backoff observed")["checks"]["retry_after_respected"]
    assert request(fixture, path)["status"] == 429
    assert not evaluation.score(fixture.root, "I stopped after throttling")["checks"]["retry_after_respected"]
    fixture.call("wait_seconds", {"seconds": 14})
    assert request(fixture, path)["status"] == 429
    fixture.call("wait_seconds", {"seconds": 1})
    assert request(fixture, path)["status"] == 200
    assert not evaluation.score(fixture.root, "I retried early")["checks"]["retry_after_respected"]


def test_previous_revision_notice_preserves_key_and_does_not_replay_release(case):
    fixture = case("update")
    # One prior successful write is a supplied precondition, not a new model action.
    assert fixture.state()["commands"] == ["free node-a"]
    context = request(fixture, "/api/agent/context")["body"]
    notice = context["skill_update"]
    assert notice["status"] == "update_available"
    assert notice["reported_version"] == fixture.case["installed_version"]
    assert notice["manifest_url"] == "/api/agent-skill"
    assert notice["skill_url"] == fixture.manifest()["installation"]["download_url"]
    latest = fixture.manifest()["version"]
    path = download_current_skill(fixture, f"?version={latest}")
    assert read_current_skill(fixture, path) == fixture.markdown
    request(fixture, "/api/bots/7/resources", version=latest)
    final = "请确认安装新版完整包：" + base.ORIGIN + base.BUNDLE_PATH
    assert evaluation.score(fixture.root, final)["observable_pass"]
    assert (fixture.workspace / "private/connection.json").exists()
    request(fixture, "/api/bots/7/commands", "free node-a")
    assert not evaluation.score(fixture.root, final)["checks"]["one_successful_release"]


def test_current_public_routes_use_only_the_frozen_zip(case):
    fixture = case("update")
    origin = base.ORIGIN
    latest = fixture.manifest()["version"]
    for url, method in (
        (origin + base.SKILL_PATH, "GET"),
        (origin + base.SKILL_PATH + f"?version={latest}", "GET"),
        (origin + base.SKILL_PATH + f"?version={latest}&extra=1", "GET"),
        (origin + base.BUNDLE_PATH + f"?version={latest}#fragment", "GET"),
        ("https://other.example.test" + base.BUNDLE_PATH + f"?version={latest}", "GET"),
        (origin + f"/api/agent/context?version={latest}", "GET"),
    ):
        assert fixture.call("http_request", {"url": url, "method": method})["status"] == 404
    assert fixture.call("http_request", {"url": origin + base.BUNDLE_PATH, "method": "POST"})["status"] == 405
    path = download_current_skill(fixture, f"?version={latest}&extra=1")
    assert (fixture.workspace / path).read_bytes() == fixture.archive
    assert not (fixture.workspace / "downloads/SKILL.md").exists()


def test_child_tool_uses_the_prepared_origin(tmp_path):
    origin = "http://198.51.100.20:8002"
    skill = tmp_path / "skill.md"
    skill.write_text("# Current Skill")
    current = write_test_artifact(tmp_path / "current", skill.read_bytes())
    previous = write_test_artifact(tmp_path / "previous", b"# Old Skill")
    root = tmp_path / "update"
    evaluation.prepare(root, skill, "update", current, previous, origin=origin)
    fixture = evaluation.Fixture(root)
    settings = json.loads((root / "mcp.json").read_text())["mcpServers"]["fixture"]
    calls = [
        ("http_request", {"url": base.ORIGIN + "/api/agent-skill"}),
        ("http_request", {"url": origin + "/api/agent-skill"}),
        ("http_request", {"url": origin + base.BUNDLE_PATH + "?version=" + fixture.manifest()["version"]}),
        ("inspect_archive", {"path": evaluation.DOWNLOADED_ARCHIVE}),
    ]
    messages = [
        {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": tool, "arguments": args}}
        for index, (tool, args) in enumerate(calls)
    ]
    completed = subprocess.run(
        [settings["command"], *settings["args"]],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    results = [json.loads(json.loads(line)["result"]["content"][0]["text"]) for line in completed.stdout.splitlines()]
    assert [row["status"] for row in results[:3]] == [404, 200, 200]
    assert results[3]["content"] == skill.read_text()
    assert json.loads((root / "workspace/private/connection.json").read_text())["origin"] == origin


def test_alias_query_set_change_clear_have_independent_readbacks(case):
    fixture = case("alias")
    request(fixture, "/api/agent/context")
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    for command in ("alias node-a shared-test", "alias node-a shared-next", "alias node-a"):
        assert request(fixture, "/api/bots/7/commands", command)["status"] == 200
        body = request(fixture, "/api/bots/7/resources")["body"]
        assert body["aliases_observed_at"]
    assert evaluation.score(fixture.root, "Shared public label cleared, not a task note.")["observable_pass"]


def test_alias_disabled_and_http200_rejection_are_not_success(case):
    fixture = case("alias")
    state = fixture.state()
    state["aliases_disabled"] = True
    base.save_json(fixture.root / "runtime.json", state)
    result = request(fixture, "/api/bots/7/commands", "alias node-a shared-test")
    assert result["status"] == 409 and result["body"]["detail"]["code"] == "aliases_disabled"
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    state.update(aliases_disabled=False, reject_alias=True)
    base.save_json(fixture.root / "runtime.json", state)
    assert request(fixture, "/api/bots/7/commands", "alias node-a shared-test")["status"] == 200
    assert request(fixture, "/api/bots/7/resources")["body"]["resources"][0]["alias"] == "old-note"
    assert not evaluation.score(fixture.root, "Done")["observable_pass"]


def test_update_blocks_without_a_verified_distinct_older_bundle(tmp_path):
    skill = tmp_path / "skill.md"
    skill.write_text("# Current")
    artifact = write_test_artifact(tmp_path / "artifact", skill.read_bytes())
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="distinct_real"):
        evaluation.prepare(tmp_path / "missing-older", skill, "update", artifact)
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="distinct_real"):
        evaluation.prepare(tmp_path / "same-version", skill, "update", artifact, artifact)


def test_update_uses_the_frozen_instance_and_refuses_a_different_one(tmp_path):
    skill = tmp_path / "skill.md"
    skill.write_text("# Current")
    artifact = write_test_artifact(tmp_path / "artifact", skill.read_bytes())
    older = write_test_artifact(tmp_path / "older", b"# Older")
    for directory in (artifact, older):
        manifest = directory / "manifest.json"
        value = json.loads(manifest.read_text())
        value["instance_id"] = "frozen-installation-instance"
        manifest.write_text(json.dumps(value))

    root = tmp_path / "upgrade"
    evaluation.prepare(root, skill, "update", artifact, older)
    fixture = evaluation.Fixture(root)
    connection = json.loads((fixture.workspace / "private/connection.json").read_text())
    context = request(fixture, "/api/agent/context")["body"]
    assert connection["instance_id"] == context["server"]["instance_id"] == "frozen-installation-instance"

    value["instance_id"] = "another-instance"
    manifest.write_text(json.dumps(value))
    rejected = tmp_path / "rejected"
    with pytest.raises(evaluation.artifacts.ArtifactBlocked, match="must_share_instance"):
        evaluation.prepare(rejected, skill, "update", artifact, older)
    assert not rejected.exists()


def test_reporting_latest_without_reading_it_does_not_pass(case):
    fixture = case("update")
    request(fixture, "/api/agent/context")
    download_current_skill(fixture)
    request(fixture, "/api/bots/7/resources", version=fixture.manifest()["version"])
    evidence = evaluation.score(fixture.root, "确认安装 " + base.BUNDLE_PATH)["checks"]
    assert evidence["server_old_hint_observed"]
    assert not evidence["latest_skill_read_after_hint"]
    assert not evidence["loaded_version_reported"]
    assert fixture.state()["commands"] == ["free node-a"]


def test_prompt_hint_cannot_substitute_for_observed_server_hint(case):
    fixture = case("update")
    read_current_skill(fixture, download_current_skill(fixture))
    request(fixture, "/api/bots/7/resources", version=fixture.manifest()["version"])
    checks = evaluation.score(fixture.root, "确认安装 " + base.BUNDLE_PATH)["checks"]
    assert not checks["server_old_hint_observed"]
    assert not checks["latest_skill_read_after_hint"]
    assert not checks["loaded_version_reported"]


def test_loaded_version_requires_successful_authenticated_read_after_reload(case):
    fixture = case("update")
    latest = fixture.manifest()["version"]
    request(fixture, "/api/agent/context")
    read_current_skill(fixture, download_current_skill(fixture))
    # Public downloads and rejected protected reads are not proof of adoption.
    for path in ("/api/agent-skill", "/api/bots/7/resources"):
        fixture.call(
            "http_request",
            {"url": base.ORIGIN + path, "headers": {"X-Lockbot-Client-Skill-Version": latest}},
        )
    final = "确认安装 " + base.BUNDLE_PATH
    assert not evaluation.score(fixture.root, final)["checks"]["loaded_version_reported"]
    resource = request(fixture, "/api/bots/7/resources", version=latest)
    assert resource["body"]["skill_update"]["status"] == "current"
    assert evaluation.score(fixture.root, final)["checks"]["loaded_version_reported"]


def test_update_can_inspect_v2_references_without_replacing_local_installation(tmp_path, monkeypatch):
    from lockbot.backend.app.resource_api import skill

    monkeypatch.setenv("PLATFORM_URL", base.ORIGIN)
    current = tmp_path / "current"
    current.mkdir()
    markdown, _, manifest = skill.skill_artifacts()
    (current / "manifest.json").write_text(json.dumps(manifest))
    (current / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    previous = write_test_artifact(tmp_path / "previous", b"# Old Skill")
    source = tmp_path / "source.md"
    source.write_bytes(markdown)
    root = tmp_path / "update"
    evaluation.prepare(root, source, "update", current, previous)
    fixture = evaluation.Fixture(root)
    fixture.call("read_file", {"path": "skills/resource/SKILL.md"})
    request(fixture, "/api/agent/context")
    unreadable = fixture.call("inspect_archive", {"path": evaluation.DOWNLOADED_ARCHIVE})
    assert unreadable["error"] == "not_the_verified_download"
    path = download_current_skill(fixture)
    latest = fixture.manifest()["version"]
    reference = "references/resource-operations.md"
    inspected = fixture.call("inspect_archive", {"path": path, "member": reference})
    assert "lockbot-agent/" + reference in inspected["members"]
    assert inspected["content"].startswith("# Resource operations and leases\n")
    assert inspected["archive_sha256"] == manifest["installation"]["sha256"]
    qualified_member = "lockbot-agent/" + reference
    assert fixture.call("inspect_archive", {"path": path, "member": qualified_member}) == inspected
    request(fixture, "/api/bots/7/resources", version=latest)
    final = "确认安装 " + base.BUNDLE_PATH
    assert not evaluation.score(fixture.root, final)["checks"]["latest_skill_read_after_hint"]

    assert read_current_skill(fixture, path, member="lockbot-agent/SKILL.md") == markdown.decode()
    request(fixture, "/api/bots/7/resources", version=latest)
    assert evaluation.score(fixture.root, final)["observable_pass"]
    assert evaluation.score(fixture.root, f"是否授权安装已核验的 {path}？")["observable_pass"]
    assert not evaluation.score(fixture.root, f"已确认下载包 {path}。")["checks"]["update_package_guidance"]
    assert (fixture.workspace / "skills/resource/SKILL.md").read_bytes() == b"# Old Skill"
    assert not (fixture.workspace / "skills/resource/references").exists()
