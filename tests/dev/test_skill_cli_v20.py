"""Opt-in query flags exercise the packaged CLI without changing historical protocols."""

import importlib
import json
import sys

import pytest
from dev.test_skill_cli_eval import evaluation, install, prepare, run

matrix = importlib.import_module("skill_eval_matrix")


def versioned(tmp_path, monkeypatch, scenario, version=20):
    original = prepare(tmp_path, monkeypatch, scenario)
    root = tmp_path / f"fixture-{version}"
    evaluation.prepare(root, tmp_path / "artifact", scenario, fixture_version=version)
    return original, evaluation.Fixture(root)


def events(fixture, name="trace.jsonl"):
    return [json.loads(line) for line in (fixture.root / name).read_text().splitlines()]


@pytest.mark.parametrize("scenario", ["intent-find-p800", evaluation.COLD_REQUEST])
def test_prepare_v20_preserves_prompt_system_and_legacy_schema(tmp_path, monkeypatch, scenario):
    original, current = versioned(tmp_path, monkeypatch, scenario)
    assert original.case["fixture_version"] == 18
    assert current.case["fixture_version"] == 20
    for filename in ("prompt.txt", "system.txt"):
        assert (current.root / filename).read_bytes() == (original.root / filename).read_bytes()
    legacy = evaluation.tools(original.root)
    evaluation.base.save_json(original.root / "case.json", {**original.case, "fixture_version": 19})
    assert evaluation.tools(original.root) == legacy
    old = next(tool for tool in legacy if tool["name"] == "run_lockbot")["inputSchema"]["properties"]
    new = next(tool for tool in evaluation.tools(current.root) if tool["name"] == "run_lockbot")["inputSchema"][
        "properties"
    ]
    assert "with_metrics" not in old and "full" not in old
    assert old["nodes"]["description"] == "Exact node IDs."
    assert "before writing" in old["model"]["description"]
    assert set(new) == {*old, "full", "with_metrics"}
    assert "current aliases" in new["nodes"]["description"]
    assert "observed exact node IDs" in new["nodes"]["description"]
    assert "resources/resources-all only" in new["full"]["description"]
    assert "separately approved" in new["with_metrics"]["description"]
    assert new["action"] == old["action"]


@pytest.mark.parametrize("action", ["resources", "resources-all"])
@pytest.mark.parametrize("node", ["node-23", "23"])
def test_query_flags_reach_actual_packaged_cli_and_http(tmp_path, monkeypatch, action, node):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-find-p800")
    private = fixture.workspace / "private"
    before = {str(path): path.read_bytes() for path in private.rglob("*") if path.is_file()}
    result = run(
        fixture,
        action,
        group=evaluation.intent.MACHINES,
        nodes=[node],
        model="P800",
        full=True,
        with_metrics=True,
    )["result"]
    snapshots = [row["snapshot"] for row in result["groups"]] if action == "resources-all" else [result]
    selected = [row for snapshot in snapshots for row in snapshot["resources"]]
    assert [row["id"] for row in selected] == ["node-23"]
    assert all(snapshot["selection"]["full_inventory"] is False for snapshot in snapshots)
    assert all(snapshot["selection"]["nodes"] == [node] for snapshot in snapshots)
    assert all(snapshot["selection"]["model"] == "p800" for snapshot in snapshots)
    telemetry = next(snapshot["metrics"] for snapshot in snapshots if snapshot["resources"])
    assert telemetry["complete"] is False and telemetry["unread"]
    assert telemetry["unread"][0]["error"] == "unsupported_metrics_profile"
    assert "responses" in telemetry  # --full also reaches the combined telemetry projection.
    (call,) = events(fixture)
    assert call["argv"][0].endswith("/skills/lockbot-agent/scripts/lockbot_cli.py")
    for flag in ("--model", "--node", "--full", "--with-metrics"):
        assert call["argv"].count(flag) == 1
    assert call["argv"][call["argv"].index("--node") + 1] == node
    wire = events(fixture, "cli-http.jsonl")
    assert any(row["url"].endswith("/resources") and row["status"] == 200 for row in wire)
    assert all(row["method"] == "GET" for row in wire)
    after = {str(path): path.read_bytes() for path in private.rglob("*") if path.is_file()}
    assert all(after[path] == content for path, content in before.items())
    # Single-resource reads already maintain the SDK's node-access guard. This
    # query interface must introduce no additional persisted query state.
    expected_new = {str(private / "group-8.json.node-access.lock")} if action == "resources" else set()
    assert set(after) - set(before) == expected_new


def test_query_unknown_hardware_remains_partial_information(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node")
    result = run(fixture, "resources-all", model="P800")["result"]
    machines = next(row["snapshot"] for row in result["groups"] if row["identity"]["bot"]["id"] == 8)
    assert machines["resources"] == []
    assert machines["selection"]["unknown_hardware_nodes"] == ["node-20"]
    assert machines["selection"]["full_inventory"] is False
    assert machines["health"] == "running"
    assert machines["context"]["actor"]["external_user_id"] == "demo-user"
    assert result["complete"]  # Complete observation, with explicitly unknown hardware.
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))


def test_repeated_query_nodes_are_forwarded_in_one_invocation(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-lock-tail-list")
    result = run(fixture, "resources-all", nodes=["19", "node-20"], model="P800")["result"]
    selected = [node["id"] for group in result["groups"] for node in group["snapshot"]["resources"]]
    assert selected == ["node-19", "node-20"]
    (call,) = events(fixture)
    argv = call["argv"]
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "--node"] == ["19", "node-20"]
    assert argv.count("--model") == 1


def test_query_flags_do_not_duplicate_or_expand_command_arguments(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-exact")
    result = run(
        fixture,
        "command",
        group=evaluation.intent.CARDS,
        operation="lock",
        nodes=["node-10"],
        devices=["2", "3"],
        duration="2h",
        model="P800",
        full=True,
        with_metrics=True,
    )
    assert result["result"]["verified"]
    argv = events(fixture)[0]["argv"]
    assert argv.count("--model") == argv.count("--node") == 1
    assert "--full" not in argv and "--with-metrics" not in argv
    run(
        fixture, "context", group=evaluation.intent.CARDS, model="P800", nodes=["node-10"], full=True, with_metrics=True
    )
    assert not {"--node", "--model", "--full", "--with-metrics"}.intersection(events(fixture)[1]["argv"])


@pytest.mark.parametrize("version", [18, 19])
def test_legacy_query_protocol_ignores_new_flags(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-find-p800", version)
    result = run(fixture, "resources", group=evaluation.intent.MACHINES, nodes=["unobserved"], model="P900", full=True)
    assert [node["id"] for node in result["result"]["resources"]] == ["node-23"]
    assert not {"--node", "--model", "--full"}.intersection(events(fixture)[0]["argv"])


@pytest.mark.parametrize("scenario", ["intent-context-lock-day", evaluation.COLD_REQUEST])
def test_resume_preserves_v20_and_authentic_history(tmp_path, monkeypatch, scenario):
    _, first = versioned(tmp_path, monkeypatch, scenario)
    if scenario == evaluation.COLD_REQUEST:
        install(first)
        answer = run(first, "key-challenge", authorized=True)["result"]["user_instruction_zh"]
    else:
        run(first, "resources", group=evaluation.intent.MACHINES, model="P800", nodes=["23"])
        answer = "node-23是空闲P800整机。"
    evaluation.base.save_json(first.root / "response.json", {"text": answer})
    destination = tmp_path / "resumed"
    evaluation.resume(first.root, destination)
    assert evaluation.Fixture(destination).case["fixture_version"] == 20
    assert evaluation.tools(destination) == evaluation.tools(first.root)
    assert (destination / "system.txt").read_bytes() == (first.root / "system.txt").read_bytes()
    assert json.loads((destination / "conversation.json").read_text()) == [
        {"role": "user", "content": (first.root / "prompt.txt").read_text()},
        {"role": "assistant", "content": answer},
    ]
    if scenario == evaluation.COLD_REQUEST:
        second = evaluation.Fixture(destination)
        assert run(second, "finish-enrollment", enrollment_dir="private/binding")["result"]["connection_created"]
        assert run(second, "resources", nodes=["node-23"], model="P800")["result"]["resources"]
        assert run(
            second, "command", operation="lock", nodes=["node-23"], model="P800", whole_node=True, duration="2h"
        )["result"]["verified"]
        third = tmp_path / "fresh-session"
        evaluation.resume(destination, third)
        assert evaluation.Fixture(third).case["fixture_version"] == 20
        assert (third / "prompt.txt").read_text() == "这个项目里有啥可用的 LockBot 资源？"
        assert not (third / "conversation.json").exists()


def test_prepare_cli_accepts_explicit_v20(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, "intent-find-p800")
    root = tmp_path / "explicit-prepare"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fixture",
            "prepare",
            str(root),
            "--artifact",
            str(tmp_path / "artifact"),
            "--scenario",
            "intent-find-p800",
            "--fixture-version",
            "20",
        ],
    )
    evaluation.main()
    assert evaluation.Fixture(root).case["fixture_version"] == 20


@pytest.mark.parametrize("version", [19, 20])
def test_matrix_records_and_forwards_selected_cli_protocol(tmp_path, monkeypatch, version):
    forwarded = []
    monkeypatch.setattr(matrix.cli, "prepare", lambda *args, **kwargs: forwarded.append(kwargs["fixture_version"]))
    assert matrix.prepare(tmp_path / "case", "cli", "intent-find-p800", tmp_path, cli_fixture_version=version)
    assert forwarded == [version]
    received = []

    def prepare_case(root, suite, scenario, artifacts, **kwargs):
        received.append(kwargs["cli_fixture_version"])
        return object()

    monkeypatch.setattr(matrix, "prepare", prepare_case)
    monkeypatch.setattr(matrix, "evaluate_phase", lambda root, metadata, *args: {**metadata, "observable_pass": False})
    rows = matrix.run_case(
        tmp_path,
        "cli",
        "intent-context-lock-day",
        1,
        ("synthetic", "no-model", []),
        {},
        "revision",
        cli_fixture_version=version,
    )
    assert received == [version]
    assert all(row["cli_fixture_version"] == version for row in rows)

    def preflight(*args):
        return {"status": "ready"}

    def case(*args, **kwargs):
        assert kwargs["cli_fixture_version"] == version
        return [{"observable_pass": True, "cli_fixture_version": version}]

    monkeypatch.setattr(matrix, "preflight_runner", preflight)
    monkeypatch.setattr(matrix, "run_case", case)
    assert matrix.run_matrix(
        tmp_path,
        [("cli", "intent-find-p800")],
        [("synthetic", "no-model", [])],
        {"current": {"revision": "revision"}},
        1,
        {},
        1,
        cli_fixture_version=version,
    )
    assert json.loads((tmp_path / "run.json").read_text())["cli_fixture_version"] == version
    report = json.loads((tmp_path / "matrix.json").read_text())
    assert report["cli_fixture_version"] == report["results"][0]["cli_fixture_version"] == version


@pytest.mark.parametrize("option", [None, 20])
def test_matrix_cli_default_and_opt_in(tmp_path, monkeypatch, option):
    runner = tmp_path / "runner.json"
    runner.write_text(json.dumps([sys.executable, "synthetic-adapter-not-executed.py"]))
    argv = [
        "matrix",
        "run",
        "--runner",
        "synthetic",
        "no-model",
        str(runner),
        "--case",
        "cli:intent-find-p800",
        "--artifacts",
        str(tmp_path / "input"),
        "--output",
        str(tmp_path / "output"),
    ]
    if option is not None:
        argv += ["--cli-fixture-version", str(option)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(matrix, "freeze", lambda *args: {"revision": "revision"})
    observed = []
    monkeypatch.setattr(matrix, "run_matrix", lambda *args, **kwargs: observed.append(kwargs) or True)
    assert matrix.main() == 0
    assert observed == [{"cli_fixture_version": option or 19}]
