"""Acceptance intent and runnable packaged-CLI checks for cli-release."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "scripts/dev")]
SOURCE = ROOT / "scripts/dev/skill_cli_release_eval.py"
spec = importlib.util.spec_from_file_location("skill_cli_release_eval", SOURCE)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_short_prompts_and_independent_initial_states_are_explicit():
    assert release.VERSION == 3
    assert release.FIXTURE_VERSION == 3
    assert release.SCENARIOS["alias-set"]["prompt"] == "19的别名改成跑测试"
    assert release.SCENARIOS["alias-clear"]["prompt"] == "删掉19的别名"
    assert release.SCENARIOS["queue-book"]["prompt"] == "book 19 一天"
    assert release.SCENARIOS["queue-claim"]["prompt"] == "19可以用了，帮我开始用"
    assert release.SCENARIOS["release-reconcile"]["prompt"] == "19释放了吗"
    assert release.SCENARIOS["update-inquiry"]["prompt"] == "有新版吗"
    assert release.SCENARIOS["queue-book"]["synthetic_actions"] == ("queue_busy_seed",)
    assert release.SCENARIOS["queue-claim"]["synthetic_actions"] == ("queue_claimable_transition",)


def test_score_requires_exact_target_known_write_and_verified_result():
    event = [{"kind": "read"}, {"kind": "write", "operation": "book", "command": "book node-19 1d"}]
    verified = [{"mutating": True, "success_verified": True}]
    assert release.score_events("queue-book", event, final="已预订", cli_results=verified)["observable_pass"]
    for bad_events, bad_results, final in (
        ([*event, {"kind": "write", "operation": "release", "command": "unlock node-19"}], verified, "已预订"),
        ([{"kind": "read"}, {"kind": "write", "operation": "book", "command": "book node-20 1d"}], verified, "已预订"),
        (
            [{"kind": "read"}, {"kind": "write", "operation": "unknown", "command": "something else"}],
            verified,
            "已预订",
        ),
        (event, [{"mutating": True, "success_verified": False}], "已预订"),
        ([{"kind": "read"}], verified, "已预订"),
    ):
        assert not release.score_events("queue-book", bad_events, final=final, cli_results=bad_results)[
            "observable_pass"
        ]


def test_read_only_cases_reject_writes_and_unknown_operations():
    assert release.score_events("update-inquiry", [{"kind": "read"}])["observable_pass"]
    for events in (
        [{"kind": "write", "operation": "book", "command": "book node-19 1d"}],
        [{"kind": "read"}, {"kind": "write", "operation": "unknown", "command": "mystery"}],
    ):
        assert not release.score_events("update-inquiry", events)["observable_pass"]


def test_update_requires_an_explicit_older_artifact(tmp_path):
    try:
        release.prepare(tmp_path / "case", tmp_path, "update-inquiry")
    except ValueError as error:
        assert "--installed-artifacts" in str(error)
    else:
        raise AssertionError("update inquiry must not invent an older installation")


def test_matrix_exposes_cli_release_cases():
    import skill_eval_matrix as matrix

    assert matrix.CASES["cli-release"] == tuple(release.SCENARIOS)


def test_packaged_cli_runs_against_isolated_http_fixture(tmp_path, monkeypatch):
    from lockbot.backend.app.resource_api.skill import _skill_artifacts

    artifact = tmp_path / "artifact"
    artifact.mkdir(mode=0o700)
    md, bundle, manifest = _skill_artifacts(release.cli.base.ORIGIN)
    (artifact / "SKILL.md").write_bytes(md)
    (artifact / "lockbot-agent.zip").write_bytes(bundle)
    (artifact / "manifest.json").write_text(json.dumps(manifest))

    scenarios = (
        ("alias-set", {"action": "alias", "node": "node-19", "alias": "跑测试"}),
        ("alias-clear", {"action": "alias", "node": "node-19", "clear": True}),
        (
            "queue-book",
            {
                "action": "command",
                "operation": "book",
                "nodes": ["node-19"],
                "whole_node": True,
                "duration": "1d",
                "group": "加速卡群",
            },
        ),
        (
            "queue-claim",
            {"action": "command", "operation": "claim", "nodes": ["node-19"], "whole_node": True, "group": "加速卡群"},
        ),
        ("queue-mine", {"action": "resources", "mine": True, "group": "加速卡群"}),
        ("release-reconcile", {"action": "resources", "mine": True, "group": "加速卡群"}),
    )
    for scenario, arguments in scenarios:
        root = tmp_path / scenario
        release.prepare(root, artifact, scenario)
        fixture = release.ReleaseFixture(root)
        import subprocess

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        wire = subprocess.run(
            [sys.executable, str(SOURCE), "serve", str(root)],
            input="".join(json.dumps(row) + "\n" for row in requests),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        responses = [json.loads(row) for row in wire.stdout.splitlines()]
        assert [row["id"] for row in responses] == [1, 2]
        assert responses[1]["result"]["tools"]
        result = fixture.call("run_lockbot", arguments)
        rows = [json.loads(line) for line in (root / "cli-http.jsonl").read_text().splitlines()]
        assert rows
        assert all(row["url"].startswith(fixture.origin + "/") for row in rows)
        assert "error" not in result or scenario in {"queue-mine", "release-reconcile"} and "result" in result
        scored = release.score(root, "已完成" if release.SCENARIOS[scenario]["writes"] else "已核对")
        assert scored["observable_pass"], (scenario, result, scored)
