"""Synthetic V2 exports: trust boundaries, exact grant intervals and no secret output."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lease_history/report_v2.py"
_spec = importlib.util.spec_from_file_location("v2_report", SCRIPT)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)
REF = "a" * 64
JOURNAL = "b" * 32


def fixture(projected=False):
    selected = report.scope("https://lockbot.example.invalid", 7, 1000, 1060, "node:dev0" if projected else "")
    empty = {"allocations": [], "bookings": [], "complete": True}
    lease = {
        "ref": REF,
        "resource": "node:dev0",
        "actor": {"platform": "feishu", "external_user_id": "alice"},
        "unit": "device",
        "mode": "exclusive",
        "start": 1010,
        "deadline": 1050,
        "origin": "api",
    }
    states = [
        copy.deepcopy(empty),
        {**empty, "allocations": [lease]},
        {**empty, "allocations": [{**lease, "deadline": 1080}]},
        copy.deepcopy(empty),
    ]
    if projected:
        for state in states:
            state["allocations"] = [
                *state["allocations"],
                {**lease, "ref": "c" * 64, "resource": "other", "start": 990},
            ]
    frames = []
    for index, (stamp, state) in enumerate(zip([1000, 1010, 1020, 1040], states, strict=True), 1):
        fid = f"{index:032x}"
        kind = {2: "acquired", 3: "renewed", 4: "released"}.get(index)
        actor = [
            {"kind": "system"},
            {"kind": "platform_user", "platform": "feishu", "external_user_id": "alice"},
            {"kind": "operator", "bot_id": 7},
            {"kind": "system"},
        ][index - 1]
        events = (
            []
            if kind is None
            else [
                {
                    "kind": kind,
                    "lease_ref": REF,
                    "resource": "node:dev0",
                    "event_id": hashlib.sha256(f"{fid}:0".encode()).hexdigest(),
                    "version": index - 1,
                    "occurred_at": stamp,
                    "recorded_at": stamp,
                    "outcome": "committed",
                }
            ]
        )
        versions = {REF: index - 1} if index > 1 else {}
        if projected:
            versions["c" * 64] = 1
        frame = {
            "schema": "lockbot.resource-lifecycle",
            "schema_version": 1,
            "bot_id": 7,
            "session": "d" * 32,
            "sequence": index,
            "timestamp": stamp,
            "state": copy.deepcopy(state),
            "before_digest": frames[-1]["after_digest"] if frames else report._state.digest(empty),
            "after_digest": report._state.digest(state),
            "boundary": "session_start" if index == 1 else None,
            "source": "api",
            "action": "lock",
            "events": events,
            "contract": {
                "version": 2,
                "journal_id": JOURNAL,
                "commit_sequence": index,
                "frame_id": fid,
                "atomic_state": True,
                "correlation_id": "e" * 32,
                "initiator": actor,
                "versions": versions,
            },
        }
        frames.append(frame)
    checkpoint = {
        "consistent": True,
        "integrity_boundary": None,
        "timestamp": 1060,
        "after_digest": frames[-1]["after_digest"],
        "contract": {"version": 2, "journal_id": JOURNAL, "commit_sequence": 4, "pending_frames": 0},
    }
    if projected:
        for frame in frames:
            frame["state"]["allocations"] = [v for v in frame["state"]["allocations"] if v["resource"] == "node:dev0"]
            frame["state_digest_scope"] = "whole_bot_before_resource_projection"
            frame["contract"]["versions"].pop("c" * 64, None)
    page = {
        "audit_schema_version": 2,
        "visibility": "public",
        "bot_id": 7,
        "as_of": 1061,
        "checkpoint": checkpoint,
        "items": list(reversed(frames[1:])),
        "next_cursor": None,
        "coverage": {
            "complete_history": False,
            "complete_window": True,
            "status": "verified",
            "boundaries": [],
            "start_anchor": frames[0],
        },
    }
    return {"export_schema_version": 2, "scope": selected, "pages": [page], "collection_status": "exhausted"}


@pytest.mark.parametrize("projected", [False, True])
def test_exact_grants_distinguish_holder_and_initiator_without_projected_digest_rehash(projected):
    result = report.analyze(fixture(projected))
    assert result["coverage_complete"]
    assert result["subject_grant_seconds"] == {"device_exclusive_subject_grant_seconds": 30}
    assert result["subject_totals"][0]["external_user_id"] == "alice"
    assert result["committed_events"] == {"acquired": 1, "renewed": 1, "released": 1}
    assert {row["initiator"]["kind"] for row in result["initiators"]} == {"platform_user", "operator", "system"}
    assert not result["complete_history"]


def test_exact_frame_replay_deduplicates_events_not_separate_requests():
    export = fixture()
    export["pages"][0]["items"].append(copy.deepcopy(export["pages"][0]["items"][0]))
    result = report.analyze(export)
    assert result["event_count"] == 3 and result["duplicate_frames_ignored"] == 1
    assert result["subject_grant_seconds"]["device_exclusive_subject_grant_seconds"] == 30


@pytest.mark.parametrize(
    "case", ["anchor", "checkpoint", "partial", "boundary", "legacy", "short_checkpoint", "version"]
)
def test_unknown_evidence_never_yields_zero_or_complete_grant_totals(case):
    export = fixture()
    page = export["pages"][0]
    if case == "anchor":
        page["coverage"]["start_anchor"] = None
    if case == "checkpoint":
        page["checkpoint"] = None
    if case == "partial":
        export["collection_status"] = "partial"
    if case == "boundary":
        page["items"][0]["boundary"] = "session_start"
    if case == "legacy":
        page["items"][0]["contract"]["atomic_state"] = False
    if case == "short_checkpoint":
        page["checkpoint"]["timestamp"] = 1059
    if case == "version":
        page["items"][0]["events"][0]["version"] = 2
        page["items"][0]["contract"]["versions"][REF] = 2
    result = report.analyze(export)
    assert not result["coverage_complete"] and result["subject_grant_seconds"] is None
    assert result["subject_totals"] == []


@pytest.mark.parametrize(
    "case", ["bot", "journal", "operator", "digest", "event_id", "frame_conflict", "resource", "clock"]
)
def test_mixed_or_forged_scope_rejected_without_raw_projection(case):
    export = fixture(case == "resource")
    frame = export["pages"][0]["items"][0]
    if case == "bot":
        frame["bot_id"] = 8
    if case == "journal":
        frame["contract"]["journal_id"] = "f" * 32
    if case == "operator":
        frame["contract"]["initiator"] = {"kind": "operator", "bot_id": 8}
    if case == "digest":
        frame["after_digest"] = "f" * 64
    if case == "event_id":
        frame["events"][0]["event_id"] = "f" * 64
    if case == "frame_conflict":
        duplicate = copy.deepcopy(frame)
        duplicate["timestamp"] += 1
        export["pages"][0]["items"].append(duplicate)
    if case == "resource":
        frame["events"][0]["resource"] = "foreign"
    if case == "clock":
        frame["timestamp"] = 1019
        frame["events"][0]["recorded_at"] = 1019
        frame["events"][0]["occurred_at"] = 1019
    with pytest.raises(report.InvalidExport):
        report.analyze(export)


def test_frozen_pagination_includes_empty_pages_and_reuses_exact_filters():
    export = fixture()
    final = export["pages"][0]
    first = {**copy.deepcopy(final), "items": [], "next_cursor": "opaque_cursor", "coverage": {"status": "pending"}}
    calls, pauses = [], []

    def fetch(selected, key, cursor):
        calls.append((copy.deepcopy(selected), key, cursor))
        return first if cursor is None else final

    collected = report.collect(export["scope"], "synthetic-key", fetch=fetch, pause=pauses.append)
    assert report.analyze(collected)["coverage_complete"]
    assert [call[2] for call in calls] == [None, "opaque_cursor"] and pauses == [2.1]
    assert all(call[0] == export["scope"] for call in calls)


@pytest.mark.parametrize("case", ["as_of", "checkpoint", "repeated", "page_budget", "denied"])
def test_collection_failure_or_scope_drift_stays_partial_and_does_not_retry(case):
    export = fixture()
    original = export["pages"][0]
    first = {**copy.deepcopy(original), "next_cursor": "opaque_cursor"}
    count = []

    def fetch(*_args):
        count.append(1)
        if len(count) == 1:
            return first
        value = copy.deepcopy(original)
        if case == "as_of":
            value["as_of"] += 1
        if case == "checkpoint":
            value["checkpoint"]["timestamp"] += 1
        if case == "repeated":
            value["next_cursor"] = "opaque_cursor"
        if case == "denied":
            raise report.InvalidExport("fixed safe error")
        return value

    collected = report.collect(
        export["scope"], "synthetic-key", max_pages=1 if case == "page_budget" else 3, fetch=fetch, pause=lambda _: None
    )
    assert collected["collection_status"] == "partial" and len(count) <= 2
    assert report.analyze(collected)["subject_grant_seconds"] is None


def test_key_file_modes_links_and_fixed_errors(tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-op-token-12345")
    key.chmod(0o600)
    assert report.private_key(key) == "synthetic-op-token-12345"
    key.chmod(0o644)
    with pytest.raises(report.InvalidExport):
        report.private_key(key)
    key.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(report.InvalidExport):
        report.private_key(link)
    hard = tmp_path / "hard"
    os.link(key, hard)
    with pytest.raises(report.InvalidExport):
        report.private_key(key)


def test_cli_is_stdlib_only_and_export_is_exclusive_private_file(tmp_path):
    exported = tmp_path / "fixture.json"
    exported.write_text(json.dumps(fixture()))
    saved = tmp_path / "saved.json"
    args = [
        sys.executable,
        "-I",
        str(SCRIPT),
        "--input",
        str(exported),
        "--save-export",
        str(saved),
        "--bot-id",
        "7",
        "--since",
        "1970-01-01T00:16:40Z",
        "--until",
        "1970-01-01T00:17:40Z",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["subject_grant_seconds"]["device_exclusive_subject_grant_seconds"] == 30
    assert saved.stat().st_mode & 0o777 == 0o600
    again = subprocess.run(args, capture_output=True, text=True, timeout=5)
    assert again.returncode != 0 and "fixture.json" not in again.stderr
    assert report.analyze(json.loads(saved.read_text())) == report.analyze(fixture())


def test_transport_uses_only_explicit_origin_and_does_not_forward_redirects(monkeypatch):
    export = fixture()
    captures = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def geturl(self):
            return captures[-1].full_url

        def read(self, _limit):
            return json.dumps(export["pages"][0]).encode()

    class Opener:
        def open(self, request, timeout):
            captures.append(request)
            assert timeout == 15
            return Response()

    monkeypatch.setattr(report.urllib.request, "build_opener", lambda handler: Opener())
    report.fetch_page(export["scope"], "DO_NOT_PRINT", "opaque_cursor")
    request = captures[0]
    assert request.full_url.startswith("https://lockbot.example.invalid/api/bots/7/op/lifecycle-audit?")
    assert "cursor=opaque_cursor" in request.full_url
    assert request.get_header("Authorization") == "Bearer DO_NOT_PRINT"
    assert report.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None

    def denied(*_args):
        raise urllib.error.HTTPError("https://private.invalid", 403, "DO_NOT_PRINT", {}, None)

    monkeypatch.setattr(report.urllib.request, "build_opener", denied)
    with pytest.raises(report.InvalidExport) as error:
        report.fetch_page(export["scope"], "DO_NOT_PRINT", None)
    assert "DO_NOT_PRINT" not in str(error.value) and error.value.__cause__ is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@example.invalid",
        "file:///tmp",
        "https://example.invalid/path",
        "https://example.invalid?token=canary",
        "https://example.invalid/#private",
        "https://example.invalid:999999999999999999999999999999999",
    ],
)
def test_untrusted_origin_forms_rejected_before_opening_a_key_or_network(origin):
    with pytest.raises(report.InvalidExport):
        report.scope(origin, 7, 1000, 1060)


def test_input_extra_notes_are_never_projected_to_report():
    exported = fixture()
    exported["pages"][0]["purpose"] = "NOT_A_PUBLIC_NOTE"
    exported["pages"][0]["items"][0]["agent_label"] = "NOT_A_PUBLIC_LABEL"
    result = json.dumps(report.analyze(exported))
    assert "NOT_A_PUBLIC" not in result


def test_late_renewal_does_not_backfill_expired_grant_interval():
    exported = fixture()
    page = exported["pages"][0]
    # Acquisition expires before renewal commits; after that commit the new deadline is effective.
    acquired, renewal, release = reversed(page["items"])
    acquired["state"]["allocations"][0]["deadline"] = 1015
    acquired["after_digest"] = report._state.digest(acquired["state"])
    renewal["before_digest"] = acquired["after_digest"]
    result = report.analyze(exported)
    assert result["subject_grant_seconds"] == {"device_exclusive_subject_grant_seconds": 25}


def test_saved_public_export_drops_unknown_response_additions():
    exported = fixture()
    page = exported["pages"][0]
    page["purpose"] = "DO_NOT_SAVE"
    page["checkpoint"]["api_key"] = "DO_NOT_SAVE"
    page["items"][0]["agent_label"] = "DO_NOT_SAVE"
    page["items"][0]["events"][0]["private_note"] = "DO_NOT_SAVE"
    page["items"][0]["contract"]["Authorization"] = "DO_NOT_SAVE"
    report.analyze(exported)
    safe = report.public_export(exported)
    assert "DO_NOT_SAVE" not in json.dumps(safe)
    assert report.analyze(safe)["subject_grant_seconds"] == {"device_exclusive_subject_grant_seconds": 30}


@pytest.mark.parametrize("claim", [None, True, 1])
def test_export_projection_cannot_upgrade_unknown_coverage(claim):
    exported = fixture()
    exported["pages"][0]["coverage"]["complete_history"] = claim
    assert report.analyze(exported)["subject_grant_seconds"] is None
    assert report.analyze(report.public_export(exported))["subject_grant_seconds"] is None


def test_changed_process_session_is_unknown_even_if_boundary_flag_is_missing():
    exported = fixture()
    exported["pages"][0]["items"][0]["session"] = "f" * 32
    assert report.analyze(exported)["subject_grant_seconds"] is None


def test_event_kind_cannot_relabel_booking_as_acquisition():
    exported = fixture()
    event = exported["pages"][0]["items"][0]["events"][0]
    event["booking_ref"] = event.pop("lease_ref")
    with pytest.raises(report.InvalidExport):
        report.analyze(exported)


@pytest.mark.parametrize("body", [[], {"scope": None}, {"scope": {"origin": False}}])
def test_malformed_envelopes_have_fixed_cli_errors_without_tracebacks(tmp_path, body):
    path = tmp_path / "untrusted.json"
    path.write_text(json.dumps(body))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--input",
            str(path),
            "--bot-id",
            "7",
            "--since",
            "1970-01-01T00:16:40Z",
            "--until",
            "1970-01-01T00:17:40Z",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2 and "Traceback" not in result.stderr and not result.stdout


def test_malformed_checkpoint_contract_fails_closed():
    exported = fixture()
    exported["pages"][0]["checkpoint"]["contract"] = []
    with pytest.raises(report.InvalidExport):
        report.analyze(exported)
