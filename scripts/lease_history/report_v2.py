#!/usr/bin/env python3
"""Bounded V2 public OP export and subject-grant report; standard library only."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# Reuse only schema-1's pure state/digest/time validators, never its chain analyzer.
_spec = importlib.util.spec_from_file_location("_lease_state_validation", Path(__file__).with_name("report.py"))
_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_state)
MAX_BYTES = 20 * 1024 * 1024
PAGE_BYTES = 3 * 1024 * 1024
KINDS = {
    "acquired",
    "renewed",
    "released",
    "expired",
    "booked",
    "claim_ready",
    "claimed",
    "claim_timeout",
    "booking_removed",
    "mode_changed",
}


class InvalidExport(ValueError):
    """Fixed safe messages only; never include credentials or response bodies."""


def require(condition, message="Invalid V2 evidence"):
    if not condition:
        raise InvalidExport(message)


def hex_id(value, length):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value) is not None


def positive(value):
    return type(value) is int and value > 0


def scope(origin, bot_id, start, end, resource="", page_size=25):
    try:
        parsed = urllib.parse.urlsplit(origin)
        _ = parsed.port
    except ValueError:
        raise InvalidExport("Use an explicit trusted HTTP(S) origin") from None
    require(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and not any(c.isspace() or ord(c) < 32 for c in origin),
        "Use an explicit trusted HTTP(S) origin",
    )
    require(
        positive(bot_id) and _state.number(start) and _state.number(end) and 0 < end - start <= 7 * 86400,
        "Use a positive bot ID and an ordered window of at most seven days",
    )
    require(
        type(page_size) is int and 1 <= page_size <= 100 and isinstance(resource, str) and len(resource) <= 160,
        "Invalid resource filter or page size",
    )
    return {
        "origin": origin.rstrip("/"),
        "bot_id": bot_id,
        "start": start,
        "end": end,
        "resource": resource,
        "page_size": page_size,
    }


def private_key(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid()
                and info.st_size <= 4096,
                "Use an owned mode-0600 regular OP key file",
            )
            key = source.read(4097).decode("ascii").strip()
        require(re.fullmatch(r"[A-Za-z0-9_.-]{16,4096}", key), "Invalid OP key file")
        return key
    except (OSError, UnicodeError):
        raise InvalidExport("Could not read the private OP key file") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_page(selected, key, cursor):
    params = {k: selected[k] for k in ("start", "end", "resource")}
    params["limit"] = selected["page_size"]
    if cursor is not None:
        params["cursor"] = cursor
    url = selected["origin"] + f"/api/bots/{selected['bot_id']}/op/lifecycle-audit?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            require(response.geturl() == url, "Redirected lifecycle response refused")
            body = response.read(PAGE_BYTES + 1)
        require(len(body) <= PAGE_BYTES, "Lifecycle page exceeded the local byte budget")
        return json.loads(body)
    except urllib.error.HTTPError:
        raise InvalidExport("Lifecycle request denied, expired, rotated or rate limited; do not replay") from None
    except (OSError, ValueError, RecursionError):
        raise InvalidExport("Lifecycle request or JSON response failed") from None


def page_scope(page, selected):
    require(
        isinstance(page, dict)
        and page.get("audit_schema_version") == 2
        and type(page["audit_schema_version"]) is int
        and type(page.get("bot_id")) is int
        and page["bot_id"] == selected["bot_id"]
        and page.get("visibility") == "public"
        and _state.number(page.get("as_of"))
        and isinstance(page.get("items"), list)
        and len(page["items"]) <= selected["page_size"]
        and isinstance(page.get("coverage"), dict),
        "Unexpected lifecycle schema or bot scope",
    )
    cursor = page.get("next_cursor")
    require(
        cursor is None
        or isinstance(cursor, str)
        and 0 < len(cursor) <= 16384
        and re.fullmatch(r"[A-Za-z0-9_.-]+", cursor),
        "Invalid lifecycle cursor",
    )


def collect(selected, key, *, max_pages=20, fetch=fetch_page, pause=time.sleep):
    require(type(max_pages) is int and 1 <= max_pages <= 100, "Invalid page budget")
    pages, cursor, seen, size = [], None, set(), 0
    result = {"export_schema_version": 2, "scope": selected, "pages": pages, "collection_status": "partial"}
    for index in range(max_pages):
        if index:
            pause(2.1)  # Below the endpoint's 30 reads/minute budget; never retry failures.
        try:
            page = fetch(selected, key, cursor)
            page_scope(page, selected)
            if pages:
                require(
                    page["as_of"] == pages[0]["as_of"] and page.get("checkpoint") == pages[0].get("checkpoint"),
                    "Frozen export changed across pages",
                )
            size += len(json.dumps(page, ensure_ascii=False).encode())
            require(size <= MAX_BYTES, "Export exceeded the local byte budget")
        except InvalidExport:
            result["reason"] = "collection_failed_or_scope_changed"
            return result
        cursor = page.get("next_cursor")
        if cursor is not None and cursor in seen:
            result["reason"] = "repeated_cursor"
            return result
        pages.append(page)
        if cursor is None:
            result["collection_status"] = "exhausted"
            return result
        if cursor in seen:
            result["reason"] = "repeated_cursor"
            return result
        seen.add(cursor)
    result["reason"] = "page_budget"
    return result


def initiator(value, bot_id):
    require(isinstance(value, dict), "Invalid initiator")
    kind = value.get("kind")
    expected = {
        "system": {"kind"},
        "unknown": {"kind"},
        "platform_user": {"kind", "platform", "external_user_id"},
        "management_user": {"kind", "user_id"},
        "operator": {"kind", "bot_id"},
    }
    require(kind in expected and set(value) == expected[kind], "Invalid initiator")
    if kind == "platform_user":
        require(
            value["platform"] in {"infoflow", "feishu"}
            and isinstance(value["external_user_id"], str)
            and 0 < len(value["external_user_id"]) <= 256,
            "Invalid initiator",
        )
    if kind == "management_user":
        require(positive(value["user_id"]), "Invalid initiator")
    if kind == "operator":
        require(type(value["bot_id"]) is int and value["bot_id"] == bot_id, "Invalid operator scope")
    return value


def validate_frame(frame, selected):
    require(
        isinstance(frame, dict)
        and frame.get("schema") == "lockbot.resource-lifecycle"
        and type(frame.get("schema_version")) is int
        and frame["schema_version"] == 1
        and type(frame.get("bot_id")) is int
        and frame["bot_id"] == selected["bot_id"]
        and hex_id(frame.get("session"), 32)
        and positive(frame.get("sequence"))
        and _state.number(frame.get("timestamp"))
        and _state.valid_state(frame.get("state"))
        and hex_id(frame.get("before_digest"), 64)
        and hex_id(frame.get("after_digest"), 64)
    )
    require(
        frame.get("boundary")
        in {
            None,
            "session_start",
            "state_persist_failed",
            "journal_write_failed",
            "invalid_state",
            "clock_reversal",
            "unconfirmed_mutation",
            "outbox_overflow",
        }
    )
    projected = bool(selected["resource"])
    require(
        frame.get("state_digest_scope") == "whole_bot_before_resource_projection"
        if projected
        else "state_digest_scope" not in frame and _state.digest(frame["state"]) == frame["after_digest"],
        "Invalid digest scope",
    )
    meta = frame.get("contract")
    require(
        isinstance(meta, dict)
        and type(meta.get("version")) is int
        and meta["version"] == 2
        and positive(meta.get("commit_sequence"))
        and hex_id(meta.get("journal_id"), 32)
        and hex_id(meta.get("frame_id"), 32)
        and hex_id(meta.get("correlation_id"), 32)
        and type(meta.get("atomic_state")) is bool
        and isinstance(meta.get("versions"), dict)
        and len(meta["versions"]) <= 4096
    )
    require(all(hex_id(ref, 64) and positive(v) for ref, v in meta["versions"].items()))
    initiator(meta.get("initiator"), selected["bot_id"])
    require(isinstance(frame.get("events"), list) and len(frame["events"]) <= 4096)
    for field in ("allocations", "bookings"):
        for lease in frame["state"][field]:
            require(lease["ref"] in meta["versions"])
            require(not projected or lease["resource"] == selected["resource"], "Resource filter mismatch")
    for index, event in enumerate(frame["events"]):
        require(
            isinstance(event, dict)
            and event.get("kind") in KINDS
            and hex_id(event.get("event_id"), 64)
            and isinstance(event.get("resource"), str)
            and 0 < len(event["resource"]) <= 160
            and event.get("outcome") == "committed"
            and _state.number(event.get("occurred_at"))
            and event["occurred_at"] <= frame["timestamp"]
            and event.get("recorded_at") == frame["timestamp"]
        )
        require(all(hex_id(event[key], 64) for key in ("lease_ref", "booking_ref") if key in event))
        require("late" not in event or type(event["late"]) is bool)
        ref = event.get("lease_ref") or event.get("booking_ref")
        require(hex_id(ref, 64) and positive(event.get("version")) and event["version"] == meta["versions"].get(ref))
        require(not projected or event["resource"] == selected["resource"], "Resource filter mismatch")
        if not projected:
            require(event["event_id"] == hashlib.sha256(f"{meta['frame_id']}:{index}".encode()).hexdigest())
    return frame


def analyze(export):
    require(
        isinstance(export, dict) and export.get("export_schema_version") == 2 and isinstance(export.get("scope"), dict)
    )
    selected = scope(**export["scope"])
    pages = export.get("pages")
    require(isinstance(pages, list) and 1 <= len(pages) <= 100, "No bounded lifecycle pages")
    unique, events, reasons, duplicate_frames = {}, {}, set(), 0
    checkpoint, as_of = pages[0].get("checkpoint"), pages[0].get("as_of")
    require(checkpoint is None or isinstance(checkpoint, dict), "Invalid frozen checkpoint")
    seen_cursors = set()
    for index, page in enumerate(pages):
        page_scope(page, selected)
        cursor = page.get("next_cursor")
        require(cursor is None or cursor not in seen_cursors, "Repeated lifecycle cursor")
        if cursor is not None:
            seen_cursors.add(cursor)
        require(page["as_of"] == as_of and page.get("checkpoint") == checkpoint, "Mixed frozen exports")
        require(index == len(pages) - 1 or page.get("next_cursor") is not None, "Pages after exhausted export")
        for frame in page["items"]:
            validate_frame(frame, selected)
            require(selected["start"] < frame["timestamp"] <= selected["end"], "Frame outside selected window")
            fid = frame["contract"]["frame_id"]
            if fid in unique:
                require(frame == unique[fid], "Conflicting duplicate frame")
                duplicate_frames += 1
            unique[fid] = frame
    final = pages[-1]
    coverage = final["coverage"]
    anchor = coverage.get("start_anchor")
    if coverage.get("complete_history") is not False:
        reasons.add("unsupported_history_claim")
    if export.get("collection_status") != "exhausted" or final.get("next_cursor") is not None:
        reasons.add("partial_collection")
    if (
        coverage.get("complete_window") is not True
        or coverage.get("status") != "verified"
        or coverage.get("boundaries") != []
    ):
        reasons.add("server_coverage_unknown")
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("consistent") is not True
        or checkpoint.get("integrity_boundary") is not None
    ):
        reasons.add("missing_consistent_checkpoint")
    if anchor is None:
        reasons.add("missing_start_anchor")
    else:
        validate_frame(anchor, selected)
        require(anchor["timestamp"] <= selected["start"], "Anchor outside selected window")
    frames = sorted(unique.values(), key=lambda f: f["contract"]["commit_sequence"])
    chain = ([anchor] if anchor else []) + frames
    totals, subjects, event_counts, actors = defaultdict(float), defaultdict(float), Counter(), Counter()
    previous = None
    versions = dict(anchor["contract"]["versions"]) if anchor else {}
    journal_id = chain[0]["contract"]["journal_id"] if chain else None
    for frame in chain:
        meta = frame["contract"]
        require(meta["journal_id"] == journal_id, "Mixed journal identity")
        if (
            not meta["atomic_state"]
            or not frame["state"]["complete"]
            or (frame is not anchor and frame.get("boundary"))
        ):
            reasons.add("non_atomic_or_boundary")
        if checkpoint:
            cp = checkpoint.get("contract", {})
            require(meta["journal_id"] == cp.get("journal_id"), "Mixed journal identity")
        for event in frame["events"] if frame is not anchor else []:
            eid = event["event_id"]
            require(eid not in events, "Duplicate event ID across different frames")
            events[eid] = event
            ref = event.get("lease_ref") or event.get("booking_ref")
            if ref in versions and event["version"] != versions[ref] + 1:
                reasons.add("non_continuous_lease_version")
            versions[ref] = event["version"]
            event_counts[event["kind"]] += 1
            actors[json.dumps(initiator(meta["initiator"], selected["bot_id"]), sort_keys=True)] += 1
        for ref, version in meta["versions"].items():
            if ref in versions and version < versions[ref]:
                reasons.add("regressed_lease_version")
            versions[ref] = max(version, versions.get(ref, 0))
        if previous:
            old = previous["contract"]
            require(
                meta["commit_sequence"] > old["commit_sequence"] and frame["timestamp"] >= previous["timestamp"],
                "Sequence conflict or clock reversal",
            )
            if not selected["resource"] and (
                meta["commit_sequence"] != old["commit_sequence"] + 1
                or frame["before_digest"] != previous["after_digest"]
            ):
                reasons.add("broken_unfiltered_chain")
        previous = frame
    if chain and checkpoint and isinstance(checkpoint.get("contract"), dict):
        cp, last = checkpoint["contract"], chain[-1]
        require(
            type(cp.get("version")) is int
            and cp["version"] == 2
            and positive(cp.get("commit_sequence"))
            and _state.number(checkpoint.get("timestamp"))
            and hex_id(checkpoint.get("after_digest"), 64),
            "Invalid frozen checkpoint",
        )
        require(cp["commit_sequence"] >= last["contract"]["commit_sequence"], "Checkpoint precedes evidence")
        if cp["commit_sequence"] == last["contract"]["commit_sequence"]:
            require(checkpoint["after_digest"] == last["after_digest"], "Checkpoint digest mismatch")
        if (
            checkpoint["timestamp"] < selected["end"]
            or checkpoint["timestamp"] > as_of
            or type(cp.get("pending_frames")) is not int
            or cp["pending_frames"] != 0
        ):
            reasons.add("unsettled_or_short_checkpoint")
    else:
        reasons.add("missing_checkpoint_or_chain")
    if not reasons:
        for index, frame in enumerate(chain):
            right = chain[index + 1]["timestamp"] if index + 1 < len(chain) else selected["end"]
            left, right = max(selected["start"], frame["timestamp"]), min(selected["end"], right)
            for lease in frame["state"]["allocations"]:
                seconds = max(0, min(right, lease["deadline"]) - max(left, lease["start"]))
                measure = f"{lease['unit']}_{lease['mode']}_subject_grant_seconds"
                totals[measure] += seconds
                subjects[(lease["actor"]["platform"], lease["actor"]["external_user_id"], measure)] += seconds
    return {
        "report_schema_version": 2,
        "bot_id": selected["bot_id"],
        "window": {k: selected[k] for k in ("start", "end", "resource")},
        "coverage_complete": not reasons,
        "unknown_reasons": sorted(reasons),
        "complete_history": False,
        "subject_grant_seconds": dict(totals) if not reasons else None,
        "subject_totals": [
            {"platform": p, "external_user_id": u, "measure": m, "seconds": v}
            for (p, u, m), v in sorted(subjects.items())
        ]
        if not reasons
        else [],
        "committed_events": dict(event_counts),
        "initiators": [{"initiator": json.loads(k), "events": v} for k, v in sorted(actors.items())],
        "duplicate_frames_ignored": duplicate_frames,
        "event_count": len(events),
        "notes": [
            "Subject grants are not physical occupancy, hardware utilization or productive work.",
            "Filtered whole-bot digests are not recomputed from projected states; trusted server coverage is required.",
            "The server cursor binds UUID/revision/credential; the consumer pins origin, bot, window and journal.",
            "Local exports are trusted input, not cryptographically signed evidence. No messages are sent.",
        ],
    }


def public_export(export):
    """Persist only fields this consumer validated, discarding response additions."""

    def frame(value):
        if value is None:
            return None
        result = {
            key: value[key]
            for key in (
                "schema",
                "schema_version",
                "bot_id",
                "session",
                "sequence",
                "timestamp",
                "state",
                "before_digest",
                "after_digest",
                "boundary",
            )
        }
        if "state_digest_scope" in value:
            result["state_digest_scope"] = value["state_digest_scope"]
        result["contract"] = {
            key: value["contract"][key]
            for key in (
                "version",
                "commit_sequence",
                "journal_id",
                "frame_id",
                "atomic_state",
                "initiator",
                "correlation_id",
                "versions",
            )
        }
        result["events"] = [
            {
                key: event[key]
                for key in (
                    "kind",
                    "lease_ref",
                    "booking_ref",
                    "resource",
                    "late",
                    "event_id",
                    "version",
                    "occurred_at",
                    "recorded_at",
                    "outcome",
                )
                if key in event
            }
            for event in value["events"]
        ]
        return result

    def checkpoint(value):
        if value is None:
            return None
        meta = value.get("contract", {})
        require(isinstance(meta, dict), "Invalid frozen checkpoint")
        return {
            "consistent": value.get("consistent") is True,
            "integrity_boundary": None if value.get("integrity_boundary") is None else "unknown",
            "timestamp": value.get("timestamp") if _state.number(value.get("timestamp")) else None,
            "after_digest": value.get("after_digest") if hex_id(value.get("after_digest"), 64) else None,
            "contract": {
                "version": 2,
                "journal_id": meta.get("journal_id") if hex_id(meta.get("journal_id"), 32) else None,
                "commit_sequence": meta.get("commit_sequence") if positive(meta.get("commit_sequence")) else None,
                "pending_frames": meta.get("pending_frames") if type(meta.get("pending_frames")) is int else None,
            },
        }

    pages = []
    for page in export["pages"]:
        coverage = page["coverage"]
        pages.append(
            {
                "audit_schema_version": 2,
                "bot_id": page["bot_id"],
                "visibility": "public",
                "as_of": page["as_of"],
                "items": [frame(value) for value in page["items"]],
                "next_cursor": page.get("next_cursor"),
                "checkpoint": checkpoint(page.get("checkpoint")),
                "coverage": {
                    "complete_history": False if coverage.get("complete_history") is False else None,
                    "complete_window": coverage.get("complete_window") is True,
                    "status": coverage.get("status")
                    if coverage.get("status") in {"verified", "unknown", "pending"}
                    else "unknown",
                    "boundaries": [] if coverage.get("boundaries") == [] else ["unknown"],
                    "start_anchor": frame(coverage.get("start_anchor")),
                },
            }
        )
    return {
        "export_schema_version": 2,
        "scope": export["scope"],
        "pages": pages,
        "collection_status": "exhausted" if export.get("collection_status") == "exhausted" else "partial",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--base-url")
    parser.add_argument("--bot-id", type=int, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--save-export", type=Path, help="Create a new mode-0600 export; never overwrite")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--resource", default="")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=25)
    args = parser.parse_args(argv)
    try:
        start, end = _state.instant(args.since), _state.instant(args.until)
        if args.input:
            descriptor = os.open(args.input, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source_file:
                info = os.fstat(source_file.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES, "Use a bounded regular export file")
                body = source_file.read(MAX_BYTES + 1)
            require(len(body) <= MAX_BYTES, "Local export exceeded the byte budget")
            export = json.loads(body)
            require(
                export.get("scope", {}).get("bot_id") == args.bot_id
                and export["scope"].get("start") == start
                and export["scope"].get("end") == end
                and export["scope"].get("resource") == args.resource,
                "Local export scope differs from requested report",
            )
        else:
            require(args.key_file is not None, "Provide a private OP key file")
            selected = scope(args.base_url, args.bot_id, start, end, args.resource, args.page_size)
            export = collect(selected, private_key(args.key_file), max_pages=args.max_pages)
        result = analyze(export)
        if args.save_export:
            body = json.dumps(public_export(export), ensure_ascii=False)
            require(len(body.encode()) <= MAX_BYTES, "Export exceeded the local byte budget")
            descriptor = os.open(
                args.save_export, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            with os.fdopen(descriptor, "w") as destination:
                destination.write(body)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["coverage_complete"] else 2
    except (InvalidExport, ValueError, TypeError, KeyError, OSError, RecursionError):
        parser.error(
            "Invalid, incomplete or inaccessible V2 export/configuration; no credentials or raw response are printed"
        )


if __name__ == "__main__":
    raise SystemExit(main())
