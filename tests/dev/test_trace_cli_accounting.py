"""Keep model calls, actual CLI invocations and nested wire requests distinct."""

import json
import runpy
import sys
from pathlib import Path

from test_trace_dataset_ui import INTERACTIONS, VIEWER, function, run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/trace-viewer"))
from publish_snapshot import phase_metrics  # noqa: E402
from synthetic_transcript import tool_steps  # noqa: E402

merge = runpy.run_path(str(ROOT / "tools/trace-viewer/merge_snapshots.py"))


def recorded_events():
    return [
        {
            "tool": "http_request",
            "args": {"method": "GET", "url": "https://fixture.example/manifest"},
            "result": {"status": 200},
        },
        {
            "source": "packaged_cli",
            "tool": "http_request",
            "args": {
                "method": "GET",
                "url": "https://fixture.example/context",
                "headers": {"Authorization": "Bearer synthetic-private"},
            },
            "result": {"status": 200, "body": {"actor": "synthetic-user"}},
        },
        {
            "source": "packaged_cli",
            "tool": "http_request",
            "args": {"method": "POST", "url": "https://fixture.example/commands", "body": {"command": "lock node-a"}},
            "result": {"status": 409, "body": {"error": "conflict"}},
        },
        {
            "tool": "run_lockbot",
            "args": {"script": "/fixture/lockbot_cli.py", "argv": ["resources"]},
            "result": {"exit_code": 2, "error": "conflict"},
            "argv": ["/fixture/lockbot_cli.py", "resources"],
            "stdout": "complete stdout\n",
            "stderr": "complete stderr\n",
            "raw_result": {"private_path": "/fixture/private/connection.json"},
        },
        {
            "tool": "run_lockbot",
            "args": {"script": "unverified.py", "argv": []},
            "result": {"error": "Verified installed packaged CLI required"},
        },
    ]


def test_marked_wire_evidence_is_complete_ordered_and_not_model_calls():
    events = recorded_events()
    projected = tool_steps("\n".join(map(json.dumps, events)), 2, raw=True)
    assert projected["total"] == 5  # All raw records remain represented.
    assert projected["model_tool_calls"] == 3
    assert projected["cli_http_requests"] == 2
    assert [step[0] for step in projected["steps"]] == [event["tool"] for event in events]
    assert projected["steps"][1][2] == "CLI 内部 HTTP"
    assert projected["steps"][2][3] == "失败"
    for event, evidence in zip(events, projected["evidence"], strict=True):
        assert evidence["input"] == event["args"]
        assert evidence["output"] == event["result"]
        for field in ("source", "argv", "stdout", "stderr", "raw_result"):
            if field in event:
                assert evidence[field] == event[field]
    sanitized = tool_steps("\n".join(map(json.dumps, events)), 2)
    assert sanitized["model_tool_calls"] == 3
    assert sanitized["evidence"][1]["source"] == "packaged_cli"
    assert "synthetic-private" not in json.dumps(sanitized)
    assert "fixture.example" not in json.dumps(sanitized)
    assert "complete stderr" not in json.dumps(sanitized)


def test_marker_gated_phase_metrics_count_http_once_and_actual_cli_only(tmp_path):
    directory = tmp_path / "phase"
    directory.mkdir()
    (directory / "trace.jsonl").write_text("\n".join(map(json.dumps, recorded_events())) + "\n")
    # A copied sidecar is not another ten requests when trace already embeds them.
    (directory / "cli-http.jsonl").write_text("{}\n" * 10)
    row = {"phase": 1, "status": "completed", "evidence": "phase", "duration_seconds": 2}
    (actual,) = phase_metrics([row], tmp_path)
    assert actual == {
        "phase": 1,
        "duration_seconds": 2,
        "tool_calls": 3,
        "model_tool_calls": 3,
        "http_requests": 3,
        "model_http_requests": 1,
        "cli_http_requests": 2,
        "cli_calls": 1,
    }
    phase = merge["summary_efficiency"](tmp_path, [row])["phases"][0]
    assert phase["cli_calls"] == 1  # Rejected model request did not invoke CLI.
    assert phase["model_tool_calls"] == 3
    assert phase["model_http_requests"] == 1 and phase["cli_http_requests"] == 2
    assert phase["http_requests"] == 3


def test_orphan_internal_http_is_not_an_invented_cli_invocation(tmp_path):
    internal = recorded_events()[1]
    (tmp_path / "trace.jsonl").write_text(json.dumps(internal) + "\n")
    (actual,) = phase_metrics([{"phase": 1, "status": "timeout", "evidence": "."}], tmp_path)
    assert actual["tool_calls"] == 0
    assert actual["cli_calls"] == 0
    assert actual["http_requests"] == actual["cli_http_requests"] == 1


def test_local_only_new_cli_keeps_diagnostics_and_reports_measured_zero_http(tmp_path):
    event = recorded_events()[3]
    text = json.dumps(event) + "\n"
    (tmp_path / "trace.jsonl").write_text(text)
    (actual,) = phase_metrics([{"phase": 1, "status": "completed", "evidence": "."}], tmp_path)
    assert actual["model_tool_calls"] == actual["cli_calls"] == 1
    assert actual["http_requests"] == actual["cli_http_requests"] == actual["model_http_requests"] == 0
    assert tool_steps(text, 1, raw=True)["evidence"][0]["stderr"] == event["stderr"]
    (tmp_path / "trace.jsonl").write_text(json.dumps(recorded_events()[4]) + "\n")
    (rejected,) = phase_metrics([{"phase": 1, "status": "completed", "evidence": "."}], tmp_path)
    assert rejected["model_tool_calls"] == 1 and rejected["cli_calls"] == 0


def test_unmarked_legacy_projection_and_metrics_keep_exact_shape(tmp_path):
    event = {
        "tool": "run_lockbot",
        "args": {"action": "resources"},
        "result": {"exit_code": 0},
        "argv": ["/fixture/lockbot_cli.py", "resources"],
        "stderr": "legacy-diagnostic",
    }
    projected = tool_steps(json.dumps(event), 1, raw=True)
    expected = {
        "role": "tools",
        "steps": [["run_lockbot", "resources", "会话 1", "已返回"]],
        "evidence": [{"input": {"action": "resources"}, "output": {"exit_code": 0}, "argv": event["argv"]}],
        "total": 1,
    }
    assert json.dumps(projected, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    (tmp_path / "trace.jsonl").write_text(json.dumps(event) + "\n")
    (tmp_path / "cli-http.jsonl").write_text("{}\n{}\n")
    assert phase_metrics([{"phase": 1, "status": "completed", "evidence": "."}], tmp_path) == [
        {"phase": 1, "tool_calls": 1, "http_requests": 2}
    ]


def test_viewer_labels_internal_http_without_counting_it_as_model_tool_call():
    code = "\n".join(function(INTERACTIONS, name) for name in ("toolTitle", "toolActivityLabel", "runEfficiency"))
    observed = run(
        code
        + """
const actionNames=new Map();
const steps=[['http_request','HTTP','CLI 内部 HTTP','已返回'],['run_lockbot','resources','会话 1','已返回']];
const evidence=[{source:'packaged_cli'}, {argv:['/fixture/lockbot_cli.py','resources']}];
const trial={sessions:[[{role:'tools',steps,evidence,total:2}]],metrics:{phases:[{
 phase:1,model_tool_calls:1,cli_calls:1,cli_http_requests:1,model_http_requests:0,http_requests:1} ]}};
console.log(JSON.stringify({label:toolActivityLabel(steps,evidence),title:toolTitle(steps[0],evidence[0]),
 efficiency:runEfficiency(trial),legacy:toolActivityLabel([steps[1]],[evidence[1]]),
 diagnostic:toolActivityLabel([...steps,['native_client_diagnostic','stderr','诊断','已记录']],[...evidence,{}]),
 diagnosticOnly:toolActivityLabel([['native_client_diagnostic','wire','诊断','已记录']], [{}]),
 protocolOnly:toolActivityLabel([['native_client_protocol','wire','诊断','已记录'],
 ['native_client_stderr','stderr','诊断','已记录']], [{},{}]),
 legacyWithDiagnostic:toolActivityLabel([steps[1],['native_client_diagnostic','wire','诊断','已记录']], [{},{}])}));
"""
    )
    assert observed == {
        "label": "1 次模型工具调用 · 1 次 CLI 内部 HTTP",
        "title": "CLI 内部 HTTP · http_request",
        "efficiency": "模型工具 1 次 · CLI 1 次 · HTTP 1 次",
        "legacy": "1 次工具调用",
        "diagnostic": "1 次模型工具调用 · 1 次 CLI 内部 HTTP · 1 项运行日志",
        "diagnosticOnly": "运行日志",
        "protocolOnly": "运行日志",
        "legacyWithDiagnostic": "1 次工具调用 · 1 项运行日志",
    }
    viewer = VIEWER.read_text()
    assert "toolActivityLabel(entry.steps,entry.evidence)" in viewer
    assert "['raw_result','CLI 原始结果']" in viewer
    assert "['stdout','标准输出']" in viewer and "['stderr','标准错误']" in viewer


def test_real_packaged_host_wire_stream_projects_without_loss(tmp_path, monkeypatch):
    from test_skill_agent_eval import frozen_package
    from test_skill_onboarding_v7 import install, prepare
    from test_skill_onboarding_v7 import run as invoke

    artifacts = frozen_package.__wrapped__(tmp_path, monkeypatch)
    fixture = prepare(tmp_path, artifacts)
    assert install(fixture)["installed"]
    initialized = invoke(fixture, "init", "--origin", fixture.origin, "--key-file", "private/current.key")
    assert initialized["exit_code"] == 0
    connection = initialized["result"]["connection"]
    assert invoke(fixture, "--connection", connection, "resources")["exit_code"] == 0
    assert "error" in invoke(fixture, "connections", script="/synthetic-user/skills/unverified/scripts/lockbot_cli.py")
    text = (fixture.root / "trace.jsonl").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    projected = tool_steps(text, 1, raw=True)
    (metrics,) = phase_metrics([{"phase": 1, "status": "completed", "evidence": "."}], fixture.root)
    assert metrics["cli_calls"] == 2
    assert metrics["model_tool_calls"] == 5  # Manifest, install, init, resources, rejected script.
    assert metrics["model_http_requests"] == 1
    assert metrics["cli_http_requests"] > metrics["cli_calls"]
    assert metrics["http_requests"] == metrics["cli_http_requests"] + 1
    assert projected["total"] == len(events)
    assert not (fixture.root / "cli-http.jsonl").exists()
    assert events.index(next(event for event in events if event.get("source") == "packaged_cli")) < events.index(
        next(event for event in events if event.get("argv"))
    )
    for event, detail in zip(events, projected["evidence"], strict=True):
        assert detail["input"] == event["args"] and detail["output"] == event["result"]
        for field in ("argv", "stdout", "stderr", "raw_result", "cli_action", "source"):
            if field in event:
                assert detail[field] == event[field]
