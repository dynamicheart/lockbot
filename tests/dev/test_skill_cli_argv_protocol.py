"""The versioned host preserves shell argv, packaged IO, and private boundaries."""

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api.skill import _skill_artifacts

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/dev"))
import skill_cli_eval as cli  # noqa: E402
import skill_cli_release_eval as release  # noqa: E402
from skill_eval_artifact import ArtifactBlocked, load  # noqa: E402
from skill_eval_packaged_cli import PackagedCliHost  # noqa: E402


def artifact(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir(mode=0o700)
    md, bundle, manifest = _skill_artifacts(cli.base.ORIGIN)
    for name, data in (
        ("SKILL.md", md),
        ("lockbot-agent.zip", bundle),
        ("manifest.json", json.dumps(manifest).encode()),
    ):
        (root / name).write_bytes(data)
    return root


def variant_artifact(source, destination, *, drop=(), extra=None):
    from lockbot.backend.app.agent_skill.scripts.lockbot_cli import canonical_package_version, content_version

    frozen = load(source)
    contents = {name: data for name, data in frozen["contents"].items() if name not in drop}
    contents["SKILL.md"] += b"\nSynthetic prior instructions for the update test.\n"
    if extra:
        contents[extra] = b"Unexpected member"
    references = {name: data for name, data in contents.items() if name.startswith("references/")}
    version = content_version(contents["SKILL.md"], contents["scripts/lockbot_cli.py"], references)
    stamp = json.loads(contents["installation.json"])
    package = canonical_package_version(
        contents["SKILL.md"], contents["scripts/lockbot_cli.py"], stamp["deployment_origin"], references
    )
    stamp.update(
        version=version,
        package_version=package,
        files={
            name: hashlib.sha256(data).hexdigest() for name, data in contents.items() if name != "installation.json"
        },
    )
    contents["installation.json"] = json.dumps(stamp).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in contents.items():
            archive.writestr("lockbot-agent/" + name, data)
    bundle = output.getvalue()
    manifest = frozen["manifest"]
    manifest.update(
        version=version, package_version=package, skill_sha256=hashlib.sha256(contents["SKILL.md"]).hexdigest()
    )
    manifest["installation"]["sha256"] = hashlib.sha256(bundle).hexdigest()
    destination.mkdir(mode=0o700)
    (destination / "manifest.json").write_text(json.dumps(manifest))
    (destination / "lockbot-agent.zip").write_bytes(bundle)
    (destination / "SKILL.md").write_bytes(contents["SKILL.md"])
    return destination


def prepare(tmp_path, scenario="intent-find-p800", version=33):
    root = tmp_path / "case"
    cli.prepare(root, artifact(tmp_path), scenario, fixture_version=version)
    return cli.Fixture(root)


def run(fixture, *argv):
    return fixture.call("run_lockbot", {"argv": list(argv)})


def test_new_cli_executes_list_and_exact_view_without_rewriting_model_args(tmp_path):
    fixture = prepare(tmp_path)
    argv = ["resource", "list", "--model", "P800", "--limit", "5", "--json", "ref,node,hardware"]
    page = run(fixture, *argv)
    assert page.get("exit_code") == 0, page
    assert page["result"]["rows"]
    ref = page["result"]["rows"][0]["ref"]
    detail = run(fixture, "resource", "view", ref, "--json", "ref,node")
    assert detail.get("exit_code") == 0, detail
    rows = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert rows[0]["args"] == {"argv": argv}
    assert rows[0]["parsed_cli"] == {
        "action": "resource",
        "resource_action": "list",
        "model": "P800",
        "limit": "5",
        "json": "ref,node,hardware",
    }
    assert rows[0]["argv"][1:] == argv
    assert all(
        row["method"] == "GET" for row in map(json.loads, (fixture.root / "cli-http.jsonl").read_text().splitlines())
    )


def test_nested_parser_keeps_bare_json_discovery_zero_http(tmp_path):
    fixture = prepare(tmp_path)
    result = run(fixture, "resource", "list", "--json")
    assert result.get("exit_code") == 0, result
    assert "json_fields" in result
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_v32_host_does_not_gain_new_argv_semantics(tmp_path):
    fixture = prepare(tmp_path, version=32)
    assert "cli_argv_protocol" not in fixture.case
    descriptor = next(row for row in cli.tools(fixture.root) if row["name"] == "run_lockbot")
    assert descriptor["inputSchema"]["required"] == ["action"]
    assert "argv" not in descriptor["inputSchema"]["properties"]
    assert run(fixture, "resource", "list").get("error")


@pytest.mark.parametrize(
    "argv",
    [
        ["resource", "list", "--directory", "/tmp"],
        ["resource", "list", "--resource-sources", "/tmp/source.json"],
        ["--connection", "../../credential.json", "resources"],
    ],
)
def test_new_host_rejects_external_paths_and_provider_execution(tmp_path, argv):
    fixture = prepare(tmp_path)
    assert run(fixture, *argv).get("error")
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_mixed_args_cannot_silently_override_argv(tmp_path):
    fixture = prepare(tmp_path)
    assert fixture.call("run_lockbot", {"argv": ["resource", "list"], "action": "command"}) == {
        "error": "use_argv_or_legacy_action"
    }
    assert not (fixture.root / "cli-http.jsonl").exists()


@pytest.mark.parametrize(
    "scenario,argv",
    [
        ("queue-book", ["command", "--operation", "book", "--node", "19", "--whole-node", "--duration", "1d"]),
        ("queue-claim", ["command", "--operation", "claim", "--node", "19", "--whole-node"]),
        ("queue-mine", ["resource", "list", "--mine", "--json", "node,holdings,bookings"]),
        ("release-reconcile", ["resource", "list", "--mine", "--json", "node,holdings,bookings"]),
    ],
)
def test_release_current_cases_support_real_argv(tmp_path, scenario, argv):
    root = tmp_path / scenario
    release.prepare(root, artifact(tmp_path), scenario)
    fixture = release.ReleaseFixture(root)
    assert fixture.case["release_fixture_version"] == 4
    result = run(fixture, *argv)
    assert result.get("exit_code") == 0, result
    assert release.score(root, "已完成核对。")["observable_pass"]


def test_shared_parser_only_adds_nested_resource_with_explicit_marker(tmp_path):
    fixture = prepare(tmp_path)
    host = PackagedCliHost(fixture, artifact=fixture.artifact)
    execution = {}
    assert host.arguments(["resource", "devices", "s-test/node-10", "--limit", "1", "--json"], execution) == [
        "resource",
        "devices",
        "s-test/node-10",
        "--limit",
        "1",
        "--json",
    ]
    assert execution["cli_action"] == "resource"
    assert execution["cli_resource_action"] == "devices"
    assert execution["cli_resource_ref"] == "s-test/node-10"
    fixture.case.pop("cli_argv_protocol")
    old = PackagedCliHost(fixture, artifact=fixture.artifact)
    with pytest.raises(ValueError, match="Unsupported fixture CLI action"):
        old.arguments(["resource", "list"], {})


def test_original_find_case_grades_delivered_bounded_facts_without_faking_raw_tools(tmp_path):
    fixture = prepare(tmp_path)
    page = run(fixture, "resource", "list", "--model", "P800", "--json", "node,unit,hardware,states")
    assert page["exit_code"] == 0, page
    trace = (fixture.root / "trace.jsonl").read_bytes()
    grade = cli.score(fixture.root, "node-23 有 P800，当前空闲。")
    assert grade["observable_pass"], grade
    assert (fixture.root / "trace.jsonl").read_bytes() == trace
    assert all("action" not in row["args"] for row in map(json.loads, trace.decode().splitlines()))


def test_cold_install_argv_challenge_uses_real_default_location(tmp_path):
    fixture = prepare(tmp_path, cli.COLD_REQUEST)
    fixture.call("install_lockbot", {"url": fixture.origin + cli.base.BUNDLE_PATH})
    response = run(fixture, "key-challenge", "--origin", fixture.origin, "--authorized")
    assert response.get("exit_code") == 0, response
    enrollment = response["result"]["enrollment_dir"]
    assert enrollment != "/synthetic-user/private/binding"
    result = cli.score(fixture.root, response["result"]["user_instruction_zh"])
    assert result["observable_pass"], result


@pytest.mark.parametrize(
    "scenario,alias_args",
    [
        ("alias-set", ["--set", "跑测试"]),
        ("alias-clear", ["--clear"]),
    ],
)
def test_release_alias_argv_uses_the_real_returned_connection(tmp_path, scenario, alias_args):
    root = tmp_path / scenario
    release.prepare(root, artifact(tmp_path), scenario)
    fixture = release.ReleaseFixture(root)
    connections = run(fixture, "connections")
    connection = connections["result"]["connections"][0]["connection"]
    response = run(fixture, "--connection", connection, "alias", "--node", "node-19", *alias_args)
    assert response.get("exit_code") == 0, response
    assert release.score(root, "已完成")["observable_pass"]


def test_cold_install_continuation_keeps_real_catalog_and_ref_write(tmp_path):
    fixture = prepare(tmp_path, cli.COLD_REQUEST)
    fixture.call("install_lockbot", {"url": fixture.origin + cli.base.BUNDLE_PATH})
    challenge = run(fixture, "key-challenge", "--origin", fixture.origin, "--authorized")["result"]
    cli.base.save_json(fixture.root / "response.json", {"text": challenge["user_instruction_zh"]})
    destination = tmp_path / "phase-2"
    cli.resume(fixture.root, destination)
    second = cli.Fixture(destination)
    catalog = run(second, "connections")
    directory = catalog["result"]["pending_enrollments"][0]["enrollment_dir"]
    assert run(second, "enrollment-status", "--enrollment-dir", directory)["result"]["status"] == "confirmed"
    assert run(second, "finish-enrollment", "--enrollment-dir", directory)["result"]["connection_created"]
    rows = run(second, "resource", "list", "--model", "P800", "--json", "node,unit,hardware,states")["result"]["rows"]
    ref = next(row["ref"] for row in rows if row["node"] == "node-23")
    changed = run(
        second, "command", "--operation", "lock", "--ref", ref, "--whole-node", "--model", "P800", "--duration", "2h"
    )
    assert changed.get("exit_code") == 0, changed
    assert changed["result"]["verified"]
    final = "已锁定 node-23，约2小时后到期。"
    cli.base.save_json(
        destination / "response.json",
        {"text": final, "conversation_sha256": cli.base.digest((destination / "conversation.json").read_bytes())},
    )
    scored = cli.score(destination, final)
    assert scored["observable_pass"], scored
    third_path = tmp_path / "phase-3"
    cli.resume(destination, third_path)
    third = cli.Fixture(third_path)
    assert run(third, "connections")["result"]["connections"]
    assert run(third, "resource", "list", "--json", "node,holdings,bookings")["exit_code"] == 0
    assert cli.score(third_path, "node-23 当前是你的占用。")["observable_pass"]


def test_update_inquiry_argv_observes_actual_package_notice_and_keeps_installation(tmp_path):
    current = artifact(tmp_path)
    previous = variant_artifact(current, tmp_path / "older")
    root = tmp_path / "update"
    release.prepare(root, current, "update-inquiry", installed_artifact=previous)
    fixture = release.ReleaseFixture(root)
    fixture.call("read_lockbot_skill", {"offset": 0, "reference": "installation.json"})
    response = run(fixture, "resource", "list", "--json", "node,hardware")
    assert response.get("exit_code") == 0, response
    assert response["result"]["update_required"] is True
    assert run(fixture, "command", "--operation", "lock", "--node", "19").get("error")
    scored = release.score(root, "有新版，尚未安装。")
    assert scored["observable_pass"], scored
    assert scored["installation_unchanged"]


def test_loader_accepts_exact_known_prior_reference_layout_with_full_integrity(tmp_path):
    current = artifact(tmp_path)
    prior = variant_artifact(current, tmp_path / "prior", drop=("references/resource-queries.md",))
    old = load(prior)
    assert "references/resource-queries.md" not in old["contents"]
    assert old["manifest"]["package_version"] != load(current)["manifest"]["package_version"]
    update = tmp_path / "prior-update"
    release.prepare(update, current, "update-inquiry", installed_artifact=prior)
    installed = update / "workspace/skills/lockbot-agent"
    assert {path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file()} == set(
        old["contents"]
    )
    for index, (drop, extra) in enumerate(
        [
            (("references/connections.md",), None),
            ((), "references/unexpected.md"),
            ((), "../escaped.txt"),
        ]
    ):
        invalid = variant_artifact(current, tmp_path / f"invalid-{index}", drop=drop, extra=extra)
        with pytest.raises(ArtifactBlocked, match="complete_helper_bundle_required"):
            load(invalid)


@pytest.mark.parametrize("family", ["onboarding", "continuity"])
def test_existing_argv_hosts_can_follow_new_resource_entrypoint(tmp_path, family):
    import skill_agent_eval as onboarding
    import skill_continuity_eval as continuity

    frozen = artifact(tmp_path)
    root = tmp_path / family
    if family == "onboarding":
        onboarding.prepare(root, None, "existing-key", artifacts=frozen, fixture_version=7)
    else:
        continuity.prepare(root, frozen / "SKILL.md", "recover", frozen, fixture_version=11)
    case = json.loads((root / "case.json").read_text())
    cli.base.save_json(root / "case.json", {**case, "cli_argv_protocol": 1})
    fixture = (onboarding if family == "onboarding" else continuity).Fixture(root)
    script = "/synthetic-user/skills/lockbot-agent/scripts/lockbot_cli.py"
    if family == "onboarding":
        fixture.call("http_request", {"url": fixture.origin + "/api/agent-skill"})
        assert fixture.call(
            "install_skill",
            {
                "url": fixture.origin + cli.base.BUNDLE_PATH,
                "sha256": fixture.manifest()["installation"]["sha256"],
                "destination": "/synthetic-user/skills/lockbot-agent",
            },
        )["installed"]
        initialized = fixture.call(
            "run_lockbot",
            {"script": script, "argv": ["init", "--origin", fixture.origin, "--key-file", "private/current.key"]},
        )
        assert initialized["exit_code"] == 0, initialized
    else:
        fixture.call("read_file", {"path": "/synthetic-user/skills/lockbot-agent/SKILL.md"})
    page = fixture.call("run_lockbot", {"script": script, "argv": ["resource", "list", "--json", "node,unit,states"]})
    assert page["exit_code"] == 0, page
    assert page["result"]["rows"]
    scored = (onboarding if family == "onboarding" else continuity).score(root, "已查看已保存的资源。")
    assert scored["observable_pass"], scored


@pytest.mark.parametrize("tamper", ["manifest-package", "both-packages", "stamp-format", "deployment-origin"])
def test_loader_rejects_package_identity_mismatch_even_with_correct_zip_hash(tmp_path, tamper):
    source = variant_artifact(artifact(tmp_path), tmp_path / "tampered")
    manifest = json.loads((source / "manifest.json").read_text())
    if tamper == "manifest-package":
        manifest["package_version"] = "0" * 16
    else:
        with zipfile.ZipFile(source / "lockbot-agent.zip") as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        stamp_name = "lockbot-agent/installation.json"
        stamp = json.loads(members[stamp_name])
        if tamper == "both-packages":
            manifest["package_version"] = stamp["package_version"] = "0" * 16
        elif tamper == "stamp-format":
            stamp["package_format"] = "canonical-origin-v1"
        else:
            stamp["deployment_origin"] = "http://another.example.test"
        members[stamp_name] = json.dumps(stamp).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        bundle = output.getvalue()
        (source / "lockbot-agent.zip").write_bytes(bundle)
        manifest["installation"]["sha256"] = hashlib.sha256(bundle).hexdigest()
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ArtifactBlocked):
        load(source)


def test_bounded_read_does_not_gain_omitted_state_facts_for_grading(tmp_path):
    fixture = prepare(tmp_path)
    page = run(fixture, "resource", "list", "--model", "P800", "--json", "node,hardware")
    assert page["exit_code"] == 0
    before = (fixture.root / "trace.jsonl").read_bytes()
    assert not cli.score(fixture.root, "node-23 有 P800，当前空闲。")["observable_pass"]
    assert (fixture.root / "trace.jsonl").read_bytes() == before


def test_resource_catalog_key_symlink_is_rejected_before_http(tmp_path):
    fixture = prepare(tmp_path)
    key = fixture.workspace / "private/group-7.key"
    external = tmp_path / "outside.key"
    external.write_bytes(key.read_bytes())
    external.chmod(0o600)
    key.unlink()
    key.symlink_to(external)
    assert run(fixture, "resource", "list", "--json", "node").get("error")
    assert not (fixture.root / "cli-http.jsonl").exists()


@pytest.mark.parametrize(
    "scenario,final",
    [
        ("intent-my-locks", "你当前占用 node-10 的 2 号卡，以及 node-20 整机。"),
        ("intent-my-books", "你预约了 node-10 的 3 号卡，以及 node-21 整机。"),
    ],
)
def test_original_own_resource_cases_use_bounded_rows_and_keep_owner_semantics(tmp_path, scenario, final):
    fixture = prepare(tmp_path, scenario)
    page = run(fixture, "resource", "list", "--mine", "--json", "node,unit,states,holdings,bookings")
    assert page["exit_code"] == 0, page
    assert cli.score(fixture.root, final)["observable_pass"]
    bad = (
        "你占用 node-10 的 4 号卡，以及 node-20。"
        if scenario == "intent-my-locks"
        else "你预约 node-10 的 4 号卡，以及 node-21。"
    )
    assert not cli.score(fixture.root, bad)["observable_pass"]


def test_native_history_keeps_the_bounded_selected_ref_for_duration_only_followup(tmp_path):
    fixture = prepare(tmp_path, "intent-context-lock-day")
    response = run(fixture, "resource", "list", "--model", "P800", "--json", "node,unit,hardware,states")
    ref = next(row["ref"] for row in response["result"]["rows"] if row["node"] == "node-23")
    previous = "node-23 有 P800，当前空闲。"
    assert cli.score(fixture.root, previous)["observable_pass"]
    cli.base.save_json(fixture.root / "response.json", {"text": previous})
    destination = tmp_path / "followup"
    cli.resume(fixture.root, destination)
    second = cli.Fixture(destination)
    changed = run(second, "command", "--operation", "lock", "--ref", ref, "--whole-node", "--duration", "1d")
    assert changed.get("exit_code") == 0, changed
    final = "已锁定 node-23，使用一天。"
    cli.base.save_json(
        destination / "response.json",
        {"text": final, "conversation_sha256": cli.base.digest((destination / "conversation.json").read_bytes())},
    )
    grade = cli.score(destination, final)
    assert grade["observable_pass"], grade
