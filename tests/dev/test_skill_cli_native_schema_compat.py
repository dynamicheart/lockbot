"""Native-compatible MCP schemas retain runtime guards and no-observation failures.

The native ducc client omits a tool with root-level oneOf before model dispatch.
Keep the published schema consumable while checking exclusivity at execution.
"""

import json
import subprocess
import sys

import pytest
from test_skill_cli_argv_protocol import artifact, release, variant_artifact


def prepare(tmp_path, scenario="alias-set", *, version=None):
    current = artifact(tmp_path)
    previous = variant_artifact(current, tmp_path / "older") if scenario == "update-inquiry" else None
    root = tmp_path / scenario
    kwargs = {"fixture_version": version} if version is not None else {}
    release.prepare(root, current, scenario, installed_artifact=previous, **kwargs)
    return release.ReleaseFixture(root)


def test_actual_stdio_catalog_exposes_executable_cli_and_runtime_rejects_mixed_modes(tmp_path):
    fixture = prepare(tmp_path)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "run_lockbot",
                "arguments": {"argv": ["--help"]},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "run_lockbot",
                "arguments": {"argv": ["--help"], "action": "command"},
            },
        },
    ]
    process = subprocess.run(
        [sys.executable, str(release.__file__), "serve", str(fixture.root)],
        input="".join(json.dumps(row) + "\n" for row in requests),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [row["id"] for row in responses] == [1, 2, 3, 4]
    catalog = responses[1]["result"]["tools"]
    assert {row["name"] for row in catalog} == {"install_lockbot", "read_lockbot_skill", "run_lockbot"}
    executable = next(row for row in catalog if row["name"] == "run_lockbot")
    description = executable["description"]
    assert "verified installed LockBot CLI" in description
    assert "argv=['--help']" in description and "argv=['COMMAND','--help']" in description
    assert "Inspection, installation and resource changes are separate commands" in description
    assert not any(value in description for value in ("discover --origin", "api/agent-skill", "package_version"))
    schema = executable["inputSchema"]
    assert schema["type"] == "object" and "argv" in schema["properties"]
    assert not {"oneOf", "allOf", "anyOf"}.intersection(schema)
    help_result = json.loads(responses[2]["result"]["content"][0]["text"])
    assert help_result["exit_code"] == 0 and "resource" in help_result["stdout"]
    assert not responses[2]["result"]["isError"]
    mixed = json.loads(responses[3]["result"]["content"][0]["text"])
    assert mixed == {"error": "use_argv_or_legacy_action"}
    assert responses[3]["result"]["isError"]
    assert not (fixture.root / "cli-http.jsonl").exists()
    scored = release.score(fixture.root, "Only help was read.")
    assert scored["status"] == "completed" and not scored["observable_pass"]
    assert scored["http_event_count"] == 0


@pytest.mark.parametrize("scenario", ["alias-set", "queue-book", "update-inquiry"])
def test_no_http_or_tool_files_are_empty_failed_observations_not_fixture_errors(tmp_path, scenario):
    fixture = prepare(tmp_path, scenario)
    assert not (fixture.root / "trace.jsonl").exists()
    assert not (fixture.root / "cli-http.jsonl").exists()
    scored = release.score(fixture.root, "No execution tool was available.")
    assert scored["status"] == "completed"
    assert not scored["observable_pass"]
    assert scored["read_count"] == scored["http_event_count"] == scored["packaged_cli_calls"] == 0


@pytest.mark.parametrize(
    "version,prompt,protocol", [(4, "有新版吗", 1), (5, "Skill有新版吗", 2), (6, "Skill有新版吗", 3)]
)
def test_new_prompt_and_schema_are_versioned_without_rewriting_old_contract(tmp_path, version, prompt, protocol):
    fixture = prepare(tmp_path, "update-inquiry", version=version)
    assert (fixture.root / "prompt.txt").read_text() == prompt
    assert fixture.case["cli_argv_protocol"] == protocol
    schema = next(row for row in fixture.tool_catalog() if row["name"] == "run_lockbot")["inputSchema"]
    assert ("oneOf" in schema) == (protocol == 1)


@pytest.mark.parametrize("version", [3, 4, 5])
def test_prior_release_descriptions_keep_their_original_inventory_contract(tmp_path, version):
    fixture = prepare(tmp_path, version=version)
    legacy = next(row for row in release.cli.legacy_tools(fixture.root) if row["name"] == "run_lockbot")
    expected = legacy["description"]
    if version >= 4:
        expected += " Prefer argv for the real CLI syntax; pass argv OR the legacy action fields."
    expected += (
        " Alias edits require the exact observed node ID; machine operations require whole_node "
        "when the user requests a whole machine."
    )
    actual = next(row for row in fixture.tool_catalog() if row["name"] == "run_lockbot")
    assert actual["description"] == expected
    assert "argv=['--help']" not in actual["description"]


def test_generic_help_can_discover_anonymous_inspection_without_installing(tmp_path):
    fixture = prepare(tmp_path, "update-inquiry")
    fixture.call("read_lockbot_skill", {"offset": 0, "reference": "installation.json"})
    before = release._tree_digest(fixture.workspace / "skills/lockbot-agent")
    for argv in (["--help"], ["discover", "--help"]):
        result = fixture.call("run_lockbot", {"argv": argv})
        assert result["exit_code"] == 0, result
        assert "discover" in result["stdout"]
    assert not (fixture.root / "cli-http.jsonl").exists()
    inspected = fixture.call("run_lockbot", {"argv": ["discover", "--origin", fixture.origin]})
    assert inspected["exit_code"] == 0, inspected
    wire = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert len(wire) == 1
    assert wire[0]["method"] == "GET" and not wire[0]["authorization_present"]
    assert wire[0]["url"] == fixture.origin + "/api/agent-skill"
    assert release._tree_digest(fixture.workspace / "skills/lockbot-agent") == before
    scored = release.score(fixture.root, "Skill有新版，尚未安装。")
    assert scored["observable_pass"] and scored["installation_unchanged"], scored
