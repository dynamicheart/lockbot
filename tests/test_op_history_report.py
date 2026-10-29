"""The standalone report counts requests, never guessed resource occupancy."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/op_history/report.py"
spec = importlib.util.spec_from_file_location("op_history_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)
SINCE = report.instant("2026-01-01T00:00:00Z")
UNTIL = report.instant("2026-01-02T00:00:00Z")


def row(event_id, **extra):
    return {
        "id": event_id,
        "timestamp": "2026-01-01T01:00:00Z",
        "actor": {"platform": "infoflow", "external_user_id": "alice"},
        "operation": "command",
        "command": "lock",
        "outcome": "dispatched",
        "task_category": "operator_testing",
        **extra,
    }


def test_groups_same_actor_across_agents_without_inventing_success_or_card_hours():
    rows = [
        row("1", agent_label="private-agent-one", purpose="private-purpose"),
        row("2", agent_label="private-agent-two"),  # A retry is still a second request.
        row("3", command="book"),
        row("4", outcome="rejected", http_status=429),
        row("5", outcome="may_have_executed", http_status=500),
        row("6", operation="usage.delete", command=None, task_category=None, previous_task_category="unit_testing"),
        row("2"),  # Only pagination repeats with the same event ID are deduplicated.
    ]
    result = report.analyze(rows, SINCE, UNTIL)
    assert len(result["actors"]) == 1
    actor = result["actors"][0]
    assert actor["records"] == 6
    assert actor["operations"] == {"lock": 4, "book": 1, "usage.delete": 1}
    assert actor["outcomes"] == {"dispatched": 4, "rejected": 1, "may_have_executed": 1}
    assert actor["categories"]["unit_testing"] == 1
    assert result["duplicate_event_rows_ignored"] == 1
    assert result["successful_acquisitions"] is result["allocated_card_hours"] is result["measured_utilization"] is None
    assert "private-" not in json.dumps(result)


def test_explicit_half_open_window_and_malformed_records():
    result = report.analyze(
        [
            None,
            row("1", timestamp=None),
            row("2", timestamp="2026-01-01"),
            row("3", timestamp="2026-01-02T00:00:00Z"),
            row("4", timestamp="2026-01-01T00:00:00Z"),
        ],
        SINCE,
        UNTIL,
    )
    assert result["actors"][0]["records"] == 1 and result["skipped_invalid_records"] == 3
    with pytest.raises(ValueError):
        report.analyze([], UNTIL, SINCE)


def test_collection_cursor_budget_empty_filtered_page_and_retention_gap(monkeypatch):
    monkeypatch.setattr(report.time, "sleep", lambda seconds: None)
    calls = []

    def fetch(base, bot, key, cursor, limit):
        calls.append(cursor)
        if cursor is None:
            return {"items": [], "next_cursor": "a", "scanned_bytes": 1024, "retention": {"files": 10}}
        if cursor == "a":
            return {"items": [row("1")], "next_cursor": "b", "scanned_bytes": 512}
        raise report.CollectionError("History rotated during collection; start a new report")

    rows, coverage = report.collect("http://example.invalid", 1, "unused", fetch=fetch)
    assert calls == [None, "a", "b"] and len(rows) == 1
    assert coverage["collection_status"] == "partial" and coverage["complete_history"] is False
    assert "rotated" in coverage["reason"]
    rows, coverage = report.collect("http://example.invalid", 1, "unused", fetch=fetch, max_pages=1)
    assert rows == [] and coverage["reason"] == "Local page budget reached"


def test_local_demo_cli_requires_no_lockbot_or_mpi_and_prints_no_private_fields(tmp_path, capsys):
    source = tmp_path / "history.jsonl"
    source.write_text(json.dumps(row("1", purpose="private-purpose")) + "\nnot-json\n")
    assert report.main(["--input", str(source), "--since", SINCE.isoformat(), "--until", UNTIL.isoformat()]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["actors"][0]["records"] == 1 and output["skipped_invalid_records"] == 1
    assert "private-purpose" not in json.dumps(output)


def test_remote_input_rejects_embedded_credentials_and_requires_environment_key(capsys):
    with pytest.raises(SystemExit):
        report.main(
            [
                "--base-url",
                "https://user:secret@example.invalid",
                "--bot-id",
                "1",
                "--since",
                SINCE.isoformat(),
                "--until",
                UNTIL.isoformat(),
            ]
        )
    assert "user:secret" not in capsys.readouterr().err


def test_http_redirect_cannot_forward_op_credential():
    reached = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/sink")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(report.CollectionError, match="HTTP 302"):
                report.fetch_page(f"http://127.0.0.1:{server.server_port}", 1, "test-only-key", None, 25)
        finally:
            server.shutdown()
            thread.join(2)
    assert len(reached) == 1 and "/sink" not in reached


def test_equal_display_ids_on_distinct_platforms_remain_distinct_actors():
    result = report.analyze(
        [row("1"), row("2", actor={"platform": "feishu", "external_user_id": "alice"})], SINCE, UNTIL
    )
    assert len(result["actors"]) == 2
