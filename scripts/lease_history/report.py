#!/usr/bin/env python3
"""Verify allocator snapshot chains and count committed grant-seconds, never utilization."""

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def instant(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
        return date.astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError("Use a valid timezone-aware timestamp within the UTC range") from None


def number(value):
    return type(value) in (int, float) and 0 <= value <= 253402300799 and math.isfinite(value)


def valid_state(state):
    if (
        not isinstance(state, dict)
        or set(state) != {"allocations", "bookings", "complete"}
        or type(state["complete"]) is not bool
    ):
        return False
    seen = set()
    for field in ("allocations", "bookings"):
        if not isinstance(state[field], list) or len(state[field]) > 2048:
            return False
        for item in state[field]:
            if not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("ref", ""))):
                return False
            actor = item.get("actor")
            if (
                not isinstance(actor, dict)
                or set(actor) != {"platform", "external_user_id"}
                or not all(isinstance(actor[k], str) and 0 < len(actor[k]) <= 256 for k in actor)
                or not isinstance(item.get("resource"), str)
                or not 0 < len(item["resource"]) <= 160
                or not isinstance(item.get("unit"), str)
                or item["unit"] not in {"node", "device"}
                or not isinstance(item.get("mode"), str)
                or item["mode"] not in {"shared", "exclusive"}
                or not number(item.get("start"))
                or (field == "allocations" and not number(item.get("deadline")))
                or (field == "bookings" and item.get("deadline") is not None and not number(item["deadline"]))
            ):
                return False
            if item["ref"] in seen or (item.get("deadline") is not None and item["deadline"] < item["start"]):
                return False
            seen.add(item["ref"])
    return True


def analyze(frames, since, until, checkpoint=None):
    if since >= until:
        raise ValueError("Window start must precede end")
    # Order within a session by sequence, then stitch session runs in observed-time order.
    sessions = defaultdict(dict)
    malformed = 0
    conflicts = set()
    bot_ids = set()
    for frame in frames:
        try:
            if (
                frame.get("schema") != "lockbot.resource-lifecycle"
                or type(frame.get("schema_version")) is not int
                or frame["schema_version"] != 1
                or type(frame.get("bot_id")) is not int
                or frame["bot_id"] <= 0
                or type(frame["sequence"]) is not int
                or frame["sequence"] < 1
                or not number(frame["timestamp"])
                or not valid_state(frame["state"])
                or digest(frame["state"]) != frame["after_digest"]
            ):
                raise ValueError()
            key = (frame["session"], frame["sequence"])
            if key in conflicts:
                continue
            previous = sessions[frame["session"]].get(frame["sequence"])
            if previous and any(
                previous.get(k) != frame.get(k)
                for k in ("timestamp", "before_digest", "after_digest", "boundary", "state")
            ):
                malformed += 1
                conflicts.add(key)
                del sessions[frame["session"]][frame["sequence"]]
                continue
            bot_ids.add(frame.get("bot_id"))
            sessions[frame["session"]][frame["sequence"]] = frame
        except (KeyError, TypeError, ValueError, AttributeError):
            malformed += 1
    runs = [sorted(items.values(), key=lambda f: f["sequence"]) for items in sessions.values() if items]
    runs.sort(key=lambda run: run[0]["timestamp"])
    if checkpoint and checkpoint.get("consistent"):
        for run in runs:
            last = run[-1]
            if (
                checkpoint.get("session") == last["session"]
                and checkpoint.get("sequence") == last["sequence"]
                and checkpoint.get("after_digest") == last["after_digest"]
                and checkpoint.get("timestamp", 0) >= last["timestamp"]
            ):
                run.append(
                    {
                        **last,
                        "sequence": last["sequence"] + 1,
                        "timestamp": checkpoint["timestamp"],
                        "before_digest": last["after_digest"],
                        "boundary": None,
                    }
                )
    segments, gaps, covered = [], [], []
    if len(runs) > 1:
        for earlier, later in zip(runs, runs[1:], strict=False):
            left, right = earlier[-1]["timestamp"], later[0]["timestamp"]
            if min(left, right) < until and max(left, right) > since:
                gaps.append(
                    {
                        "start": max(since, min(left, right)),
                        "end": min(until, max(left, right)),
                        "reason": "session_boundary",
                    }
                )
    for run in runs:
        for previous, current in zip(run, run[1:], strict=False):
            start, end = previous["timestamp"], current["timestamp"]
            left, right = max(start, since), min(end, until)
            if end < start and min(start, end) < until and max(start, end) > since:
                gaps.append({"start": max(since, end), "end": min(until, start), "reason": "clock_reversal"})
            if current.get("boundary") and start == end and since < start < until:
                gaps.append({"start": start, "end": end, "reason": current["boundary"]})
            if right <= left:
                continue
            valid = (
                current["sequence"] == previous["sequence"] + 1
                and not current.get("boundary")
                and current["before_digest"] == previous["after_digest"]
                and previous["state"].get("complete") is True
                and current["state"].get("complete") is True
            )
            if not valid:
                gaps.append(
                    {"start": left, "end": right, "reason": current.get("boundary") or "sequence_or_digest_gap"}
                )
                continue
            covered.append((left, right))
            for allocation in previous["state"]["allocations"]:
                deadline = allocation["deadline"]
                grant_start = max(left, allocation["start"])
                stop = min(right, deadline)
                if stop <= grant_start:
                    continue
                # A renewal takes effect only after its commit: the previous deadline wins
                # for this pair, so a late renewal cannot backfill an expired interval.
                segments.append(
                    {
                        "lease_ref": allocation["ref"],
                        "resource": allocation["resource"],
                        "actor": {key: allocation["actor"][key] for key in ("platform", "external_user_id")},
                        "unit": allocation["unit"],
                        "mode": allocation["mode"],
                        "start": grant_start,
                        "end": stop,
                        "seconds": stop - grant_start,
                    }
                )
    covered.sort()
    horizon = since
    for start, end in covered:
        if start > horizon:
            gaps.append({"start": horizon, "end": start, "reason": "unobserved_window"})
        horizon = max(horizon, end)
    if horizon < until:
        gaps.append({"start": horizon, "end": until, "reason": "unobserved_window"})
    if len(bot_ids) != 1:
        gaps.append({"start": since, "end": until, "reason": "missing_or_mixed_bot_scope"})
    # Never call overlapping uncertain clock/session ranges verified segments.
    clipped = []
    for segment in segments:
        pieces = [(segment["start"], segment["end"])]
        for gap in gaps:
            if gap["end"] <= gap["start"]:
                continue
            next_pieces = []
            for left, right in pieces:
                if gap["end"] <= left or gap["start"] >= right:
                    next_pieces.append((left, right))
                else:
                    if left < gap["start"]:
                        next_pieces.append((left, gap["start"]))
                    if right > gap["end"]:
                        next_pieces.append((gap["end"], right))
            pieces = next_pieces
        clipped.extend({**segment, "start": left, "end": right, "seconds": right - left} for left, right in pieces)
    segments = clipped
    complete = not gaps and not malformed
    totals = defaultdict(float)
    subjects = defaultdict(float)
    for segment in segments:
        key = f"{segment['unit']}_{segment['mode']}_subject_grant_seconds"
        totals[key] += segment["seconds"]
        actor = segment["actor"]
        subjects[(actor["platform"], actor["external_user_id"], key)] += segment["seconds"]
    return {
        "schema_version": 1,
        "bot_id": next(iter(bot_ids)) if len(bot_ids) == 1 else None,
        "report": "Verified committed allocation time (not hardware utilization)",
        "window": {"since": since, "until": until},
        "coverage_complete": complete,
        "verified_allocated_seconds": dict(totals) if complete else None,
        "verified_segments": segments,
        "unknown_gaps": gaps,
        "malformed_records": malformed,
        "subject_totals": [
            {"platform": p, "external_user_id": u, "measure": k, "verified_segment_seconds": v}
            for (p, u, k), v in sorted(subjects.items())
        ],
        "notes": [
            "node-seconds and device-seconds are different units; no hardware card multiplier is guessed",
            "shared allocations count subject-grant seconds, not exclusive physical occupancy",
            "bootstrapped state starts at its observed baseline; earlier history is unknown",
            "renewals, duplicate event rows and failed/retried requests do not create duplicate grant time",
            "public lease references are correlation hashes, never holding proofs or authorization",
            "no messages are sent; publishing a summary requires separate explicit authorization",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Authorized OP lease-history pages as JSONL")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as source:
            body = source.read(20 * 1024 * 1024 + 1)
        if len(body) > 20 * 1024 * 1024:
            raise ValueError("Select an export below 20 MiB")
        frames, checkpoint = [], None
        for line in body.splitlines():
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                frames.append(None)
                continue
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                frames.extend(value["items"])
                if value.get("checkpoint"):
                    checkpoint = value["checkpoint"]
            else:
                frames.append(value)
        print(
            json.dumps(
                analyze(frames, instant(args.since), instant(args.until), checkpoint), indent=2, ensure_ascii=False
            )
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
