"""Bounded frozen lifecycle export. Cursors bind identity, filters and file high-water marks."""

import base64
import hashlib
import hmac
import json
import os
import stat
import time

from fastapi import HTTPException

from lockbot.backend.app.config import JWT_SECRET
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.resource_api.lease_contract import contract_frame
from lockbot.backend.app.resource_api.lease_history import BACKUPS, LOG_NAME, MAX_LINE_BYTES

READ_BUDGET = 1024 * 1024
CURSOR_SECONDS = 900
RESPONSE_ITEMS_BUDGET = 1024 * 1024


def _encode(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(JWT_SECRET.encode(), b"lifecycle-feed-v2:" + body, hashlib.sha256).digest()
    return (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _decode(cursor):
    try:
        body, signature = cursor.encode().split(b".")
        expected = hmac.new(JWT_SECRET.encode(), b"lifecycle-feed-v2:" + body, hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, actual):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        if not isinstance(value, dict) or value.get("v") != 2:
            raise ValueError
        if value["expires"] < time.time():
            raise HTTPException(409, "Lifecycle cursor expired; start a new export")
        return value
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid lifecycle cursor") from None


def _paths(bot_id):
    paths = get_managed_paths()
    return [paths.bot_file(bot_id, LOG_NAME + (f".{index}" if index else "")) for index in range(BACKUPS + 1)]


def _open(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    source = os.fdopen(descriptor, "rb")
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        source.close()
        raise HTTPException(409, "Lifecycle storage is not regular private storage")
    return source, info


def _manifest(bot_id):
    files, seen = [], set()
    for path in _paths(bot_id):
        try:
            source, info = _open(path)
        except FileNotFoundError:
            continue
        with source:
            identity = [info.st_dev, info.st_ino, info.st_size]
            if tuple(identity[:2]) not in seen:
                files.append(identity)
                seen.add(tuple(identity[:2]))
    return files


def _frozen_file(bot_id, identity):
    for path in _paths(bot_id):
        try:
            source, info = _open(path)
        except FileNotFoundError:
            continue
        if [info.st_dev, info.st_ino] == identity[:2] and info.st_size >= identity[2]:
            return source
        source.close()
    raise HTTPException(409, "Frozen lifecycle history was rotated or replaced; start a new export")


def read_feed(bot, *, cursor=None, limit=25, resource="", start=None, end=None, checkpoint=None):
    """The scope selects records; a filtered record explicitly retains bot-wide digest identity."""
    scope = {
        "bot": bot.id,
        "uuid": str(bot.uuid),
        "revision": bot.identity_revision,
        "credential": hashlib.sha256(bot.api_key.encode()).hexdigest(),
        "resource": resource,
        "start": start,
        "end": end,
        "limit": limit,
    }
    if cursor:
        state = _decode(cursor)
        if state.get("scope") != scope:
            raise HTTPException(400, "Lifecycle cursor belongs to a different bot, credential or filter")
    else:
        now = time.time()
        state = {
            "v": 2,
            "expires": now + CURSOR_SECONDS,
            "scope": scope,
            "as_of": now,
            "files": _manifest(bot.id),
            "index": 0,
            "offset": None,
            "newer": None,
            "tip": None,
            "gaps": [],
            "checkpoint": checkpoint,
            "anchor": None,
        }
    items, remaining, response_bytes = [], READ_BUDGET, 0
    while state["index"] < len(state["files"]) and len(items) < limit and remaining > 0:
        identity = state["files"][state["index"]]
        stop = identity[2] if state["offset"] is None else state["offset"]
        if stop == 0:
            state["index"] += 1
            state["offset"] = None
            continue
        begin = max(0, stop - remaining)
        with _frozen_file(bot.id, identity) as source:
            source.seek(begin)
            data = source.read(stop - begin)
        remaining -= len(data)
        if begin:
            newline = data.find(b"\n")
            if newline < 0:
                state["gaps"] = sorted(set(state["gaps"] + ["oversized_record"]))
                state["offset"] = begin
                continue
            begin += newline + 1
            data = data[newline + 1 :]
        lines = data.splitlines(keepends=True)
        position = begin + len(data)
        for line in reversed(lines):
            position -= len(line)
            state["offset"] = position
            if not line.strip():
                continue
            try:
                frame = contract_frame(json.loads(line)) if len(line) <= MAX_LINE_BYTES else None
            except (ValueError, UnicodeError, RecursionError):
                frame = None
            if frame is None or frame["bot_id"] != bot.id:
                state["gaps"] = sorted(set(state["gaps"] + ["legacy_or_invalid_record"]))
                continue
            prior_newer, prior_tip, prior_gaps = state["newer"], state["tip"], list(state["gaps"])
            meta = frame["contract"]
            current = {
                "journal": meta["journal_id"],
                "sequence": meta["commit_sequence"],
                "before": frame["before_digest"],
                "after": frame["after_digest"],
                "time": frame["timestamp"],
            }
            if state["tip"] is None:
                state["tip"] = current
            newer = state["newer"]
            if newer and (
                current["journal"] != newer["journal"]
                or current["sequence"] + 1 != newer["sequence"]
                or current["after"] != newer["before"]
                or current["time"] > newer["time"]
            ):
                state["gaps"] = sorted(set(state["gaps"] + ["broken_commit_chain"]))
            state["newer"] = current
            if start is not None and frame["timestamp"] <= start:
                # (start, end]: after-state of the last commit at/before start is the anchor.
                # Stop here, so this large public state never needs to be encoded in a cursor.
                state["anchor"] = frame
                state["index"] = len(state["files"])
                break
            if frame["boundary"] or not meta["atomic_state"]:
                state["gaps"] = sorted(set(state["gaps"] + [frame["boundary"] or "non_atomic_commit"]))
            if (start is not None and frame["timestamp"] < start) or (end is not None and frame["timestamp"] > end):
                continue
            if resource:
                frame["events"] = [event for event in frame["events"] if event.get("resource") == resource]
                for field in ("allocations", "bookings"):
                    frame["state"][field] = [lease for lease in frame["state"][field] if lease["resource"] == resource]
                if not frame["events"] and not any(frame["state"][field] for field in ("allocations", "bookings")):
                    continue
                frame["state_digest_scope"] = "whole_bot_before_resource_projection"
                refs = {lease["ref"] for field in ("allocations", "bookings") for lease in frame["state"][field]}
                refs.update(event.get("lease_ref") or event.get("booking_ref") for event in frame["events"])
                meta["versions"] = {key: value for key, value in meta["versions"].items() if key in refs}
            frame_bytes = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode())
            if response_bytes + frame_bytes > RESPONSE_ITEMS_BUDGET and items:
                state["offset"] = position + len(line)
                state["newer"], state["tip"], state["gaps"] = prior_newer, prior_tip, prior_gaps
                break
            response_bytes += frame_bytes
            items.append(frame)
            if len(items) == limit:
                break
        else:
            state["offset"] = begin
    finished = state["index"] >= len(state["files"])
    frozen = state["checkpoint"] or {}
    tip, anchor = state["tip"], state["anchor"]
    tip_matches = bool(
        tip
        and frozen.get("consistent")
        and frozen.get("after_digest") == tip["after"]
        and frozen.get("contract", {}).get("journal_id") == tip["journal"]
        and frozen.get("contract", {}).get("commit_sequence") == tip["sequence"]
    )
    anchored = bool(anchor and anchor["state"]["complete"] and anchor["contract"]["atomic_state"])
    verified = bool(
        finished
        and anchored
        and tip_matches
        and not state["gaps"]
        and end is not None
        and end <= frozen.get("timestamp", -1)
    )
    coverage = {
        "complete_history": False,
        "complete_window": verified,
        "status": "verified" if verified else ("unknown" if finished or state["gaps"] else "pending"),
        "boundaries": state["gaps"],
        "start_anchor": anchor,
        "read_budget_bytes": READ_BUDGET,
        "response_items_budget_bytes": RESPONSE_ITEMS_BUDGET,
        "supported_sources": ["chat", "api", "operator", "live_admin", "system_timer"],
        "unsupported_sources": ["offline_admin", "imported_state", "pre_contract_history"],
        "reason": None if verified else "insufficient_continuous_evidence",
    }
    return {
        "audit_schema_version": 2,
        "bot_id": bot.id,
        "visibility": "public",
        "as_of": state["as_of"],
        "items": items,
        "next_cursor": None if finished else _encode(state),
        "checkpoint": state["checkpoint"],
        "coverage": coverage,
    }
