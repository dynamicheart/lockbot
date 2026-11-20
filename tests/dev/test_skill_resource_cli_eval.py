"""The model host executes the frozen CLI; no model calls or external services."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/dev"))
import skill_resource_cli_eval as evaluation  # noqa: E402


def prepare(tmp_path, monkeypatch, scenario="list-resources"):
    monkeypatch.setenv("PLATFORM_URL", evaluation.cli.base.ORIGIN)
    _, _, manifest = skill.skill_artifacts()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    evaluation.cli.base.save_json(artifact / "manifest.json", manifest)
    root = tmp_path / scenario
    evaluation.prepare(root, artifact, scenario)
    return evaluation.Fixture(root)


def run(fixture, *argv):
    return fixture.call("run_lockbot", {"argv": list(argv)})


def test_actual_packaged_resource_list_view_and_devices(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch)
    page = run(fixture, "resource", "list", "--node", "node-10", "--json", "ref,node,hardware")["result"]
    assert page["rows"]
    target = page["rows"][0]["ref"]
    detail = run(fixture, "resource", "view", target, "--json", "node,hardware")
    assert detail.get("result"), detail
    devices = run(fixture, "resource", "devices", target, "--limit", "1", "--json", "id,model")
    assert devices.get("result"), devices
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert all(row["exit_code"] == 0 for row in trace)
    assert all("workspace/skills/lockbot-agent/scripts/lockbot_cli.py" in row["argv"][0] for row in trace)
    requests = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert requests and all(row["method"] == "GET" for row in requests)


def test_status_answer_can_use_one_exact_list_without_redundant_view(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "node-detail")
    fixture.call("read_lockbot_skill", {"offset": 0})
    page = run(fixture, "resource", "list", "--node", "node-10", "--json", "node,hardware,states")["result"]
    assert page["rows"][0]["node"] == "node-10"
    assert evaluation.score(fixture.root, "Observed node-10 status.")["observable_pass"]
    trace = [json.loads(line) for line in (fixture.root / "trace.jsonl").read_text().splitlines()]
    assert len([row for row in trace if row["tool"] == "run_lockbot"]) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["resource", "list", "--node", "node-23", "--json", "node,hardware,states"],
        ["resource", "list", "--node", "node-10", "--json", "node"],
    ],
)
def test_other_node_or_missing_status_facts_cannot_satisfy_status_question(tmp_path, monkeypatch, argv):
    fixture = prepare(tmp_path, monkeypatch, "node-detail")
    fixture.call("read_lockbot_skill", {"offset": 0})
    assert run(fixture, *argv).get("result")
    assert not evaluation.score(fixture.root, "Observed status.")["checks"]["exact_detail_observed"]


def test_archived_v1_status_grading_keeps_its_old_contract(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "node-detail")
    run(fixture, "resource", "list", "--node", "node-10", "--json", "node,hardware,states")
    path = fixture.root / "case.json"
    case = json.loads(path.read_text())
    case["resource_cli_version"] = 1
    evaluation.cli.base.save_json(path, case)
    assert not evaluation.score(fixture.root, "Observed status.")["checks"]["exact_detail_observed"]


@pytest.mark.parametrize("argument", ["--help", "-h"])
def test_current_host_executes_real_top_level_help_without_network(tmp_path, monkeypatch, argument):
    fixture = prepare(tmp_path, monkeypatch)
    output = run(fixture, argument)
    assert output["exit_code"] == 0
    assert output["text"].startswith("usage: lockbot_cli.py")
    assert not (fixture.root / "cli-http.jsonl").exists()
    assert evaluation.cli.base.KEY not in output["text"]


def test_help_cannot_smuggle_an_action_or_change_archived_scope(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch)
    assert run(fixture, "--help", "command").get("error")
    case = fixture.case.copy()
    case["resource_cli_version"] = 1
    evaluation.cli.base.save_json(fixture.root / "case.json", case)
    assert run(evaluation.Fixture(fixture.root), "--help").get("error")
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_pagination_runs_real_cursor_and_completes_coverage(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "all-resources")
    fixture.call("read_lockbot_skill", {"offset": 0})
    cursor, refs = None, set()
    for _ in range(5):
        argv = ["resource", "list", "--limit", "10", "--json", "ref,node"]
        if cursor:
            argv += ["--cursor", cursor]
        result = run(fixture, *argv)
        assert "result" in result, result
        page = result["result"]
        refs.update(row["ref"] for row in page["rows"])
        cursor = page.get("next_cursor")
        if not page["has_more"]:
            break
    assert len(refs) == 28
    assert page["traversal_exhausted"] is True
    assert page["unread_reasons"] == []
    assert evaluation.score(fixture.root, "Observed all resources.")["observable_pass"]


def test_first_page_does_not_satisfy_all_resources(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "all-resources")
    fixture.call("read_lockbot_skill", {"offset": 0})
    page = run(fixture, "resource", "list", "--limit", "5", "--json", "ref,node")["result"]
    assert page["has_more"] is True
    verdict = evaluation.score(fixture.root, "All resources shown.")
    assert not verdict["observable_pass"]
    assert not verdict["checks"]["all_fixture_rows_observed"]


@pytest.mark.parametrize(
    "argv",
    [
        ["command", "--operation", "lock", "--node", "node-10"],
        ["resource", "label", "add", "anything"],
        ["resource", "list", "--directory", "/tmp"],
        ["resource", "list", "--resource-sources", "/tmp/provider.json"],
    ],
)
def test_host_rejects_writes_and_external_local_state(tmp_path, monkeypatch, argv):
    fixture = prepare(tmp_path, monkeypatch)
    assert run(fixture, *argv).get("error")
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_modified_bundle_is_not_executed(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch)
    (fixture.installation / "scripts/lockbot_cli.py").write_text("raise RuntimeError('unverified')")
    assert run(fixture, "resource", "list") == {"error": "verified_package_required"}
    assert not (fixture.root / "cli-http.jsonl").exists()


def test_metrics_unknown_keeps_real_cli_coverage_notice(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch, "metrics")
    fixture.call("read_lockbot_skill", {"offset": 0})
    result = run(fixture, "resource", "list", "--model", "P800", "--with-metrics", "--json", "node,metrics")
    assert "result" in result, result
    assert result["result"]["metrics_read_complete"] is False
    assert result["result"]["metrics_unread_reasons"]
    assert evaluation.score(fixture.root, "Utilization is unknown.")["observable_pass"]


def test_field_discovery_does_not_invent_http_or_pass(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch)
    fields = run(fixture, "resource", "list", "--json")
    assert "json_fields" in fields, fields
    assert not (fixture.root / "cli-http.jsonl").exists()
    assert not evaluation.score(fixture.root, "Available fields.")["observable_pass"]


def test_stdio_mcp_routes_only_read_tools_to_the_frozen_cli(tmp_path, monkeypatch):
    fixture = prepare(tmp_path, monkeypatch)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run_lockbot",
                "arguments": {"argv": ["resource", "list", "--json"]},
            },
        },
    ]
    process = subprocess.run(
        [sys.executable, str(evaluation.__file__), "serve", str(fixture.root)],
        input="".join(json.dumps(row) + "\n" for row in messages),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert {row["name"] for row in responses[0]["result"]["tools"]} == {"read_lockbot_skill", "run_lockbot"}
    fields = json.loads(responses[1]["result"]["content"][0]["text"])
    assert "json_fields" in fields
    assert not responses[1]["result"]["isError"]
    assert not (fixture.root / "cli-http.jsonl").exists()


@pytest.mark.parametrize("scenario", ["node-detail", "device-detail"])
def test_detail_grader_requires_the_requested_node(tmp_path, monkeypatch, scenario):
    fixture = prepare(tmp_path, monkeypatch, scenario)
    fixture.call("read_lockbot_skill", {"offset": 0})
    page = run(fixture, "resource", "list", "--node", "node-10", "--json", "ref,node")["result"]
    assert not evaluation.score(fixture.root, "Observed the list.")["observable_pass"]
    ref = page["rows"][0]["ref"]
    if scenario == "node-detail":
        run(fixture, "resource", "view", ref, "--json", "node,hardware,states")
    else:
        run(fixture, "resource", "devices", ref, "--json", "id,model")
    assert evaluation.score(fixture.root, "Observed node-10 details.")["observable_pass"]
