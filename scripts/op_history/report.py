#!/usr/bin/env python3
"""Bounded, local Agent API request analysis. Python standard library only."""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
PUBLIC_CATEGORIES = frozenset(
    {
        "operator_development",
        "operator_testing",
        "communication_testing",
        "unit_testing",
        "functional_testing",
        "performance_testing",
        "model_development",
        "model_load_testing",
        "code_review",
        "debugging",
        "other",
    }
)
OUTCOMES = frozenset({"dispatched", "applied", "rejected", "may_have_executed"})
COMMANDS = frozenset({"lock", "slock", "book", "unlock", "alias", "query", "help"})


class CollectionError(Exception):
    """A fixed, non-secret description; never print response bodies or request headers."""


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be text with an explicit timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Use an explicit timezone, for example 2026-01-01T00:00:00Z")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("Timestamp is outside the supported UTC range") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the OP Authorization header to another origin.


def fetch_page(base_url, bot_id, key, cursor, page_size):
    params = {"limit": page_size}
    if cursor:
        params["cursor"] = cursor
    url = f"{base_url.rstrip('/')}/api/bots/{bot_id}/op/history?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CollectionError("History response exceeded the local size limit")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise CollectionError("History rotated during collection; start a new report") from None
        if exc.code == 429:
            raise CollectionError("History reads were rate limited; respect Retry-After before a new run") from None
        raise CollectionError(f"History request failed with HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise CollectionError("History request or JSON response failed") from exc
    if (
        not isinstance(data, dict)
        or data.get("bot_id") != bot_id
        or not isinstance(data.get("items"), list)
        or len(data["items"]) > page_size
        or not isinstance(data.get("coverage"), dict)
        or data["coverage"].get("source") != "agent_api_operations"
    ):
        raise CollectionError("Unexpected bot scope or history response schema")
    cursor = data.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
        raise CollectionError("Invalid history cursor in response")
    return data


def collect(base_url, bot_id, key, *, max_pages=20, page_size=100, interval=2.1, fetch=fetch_page):
    rows, cursor, scanned, seen = [], None, 0, set()
    coverage = {"source": "agent_api_operations", "complete_history": False}
    for page in range(max_pages):
        if page:
            time.sleep(interval)
        try:
            data = fetch(base_url, bot_id, key, cursor, page_size)
        except CollectionError as exc:
            return rows, {**coverage, "collection_status": "partial", "reason": str(exc), "pages": page}
        rows.extend(row for row in data["items"] if isinstance(row, dict))
        amount = data.get("scanned_bytes", 0)
        scanned += amount if type(amount) is int and amount >= 0 else 0
        coverage = {
            "source": "agent_api_operations",
            "complete_history": False,
            "retention": data.get("retention", {}),
            "pages": page + 1,
            "scanned_bytes": scanned,
        }
        cursor = data.get("next_cursor")
        if cursor is None:
            return rows, {**coverage, "collection_status": "available_files_exhausted"}
        if cursor in seen:
            return rows, {**coverage, "collection_status": "partial", "reason": "Repeated history cursor"}
        seen.add(cursor)
    return rows, {**coverage, "collection_status": "partial", "reason": "Local page budget reached"}


def analyze(rows, since, until, *, coverage=None):
    if since >= until:
        raise ValueError("--since must precede --until")
    actors, seen = {}, set()
    skipped, duplicates = 0, 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        try:
            timestamp = instant(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if not since <= timestamp < until:
            continue
        event_id = row.get("id")
        if isinstance(event_id, str):
            if event_id in seen:
                duplicates += 1
                continue
            seen.add(event_id)
        actor = row.get("actor")
        if not isinstance(actor, dict) or not all(
            isinstance(actor.get(field), str) and actor[field] for field in ("platform", "external_user_id")
        ):
            skipped += 1
            continue
        identity = (actor["platform"], actor["external_user_id"])
        group = actors.setdefault(
            identity, {"records": 0, "operations": Counter(), "outcomes": Counter(), "categories": Counter()}
        )
        group["records"] += 1
        operation = row.get("operation")
        command = row.get("command")
        label = command if operation == "command" and isinstance(command, str) and command in COMMANDS else operation
        if not isinstance(label, str) or label not in COMMANDS | {"usage.update", "usage.delete"}:
            label = "unknown"
        group["operations"][label] += 1
        outcome = row.get("outcome")
        group["outcomes"][outcome if isinstance(outcome, str) and outcome in OUTCOMES else "unknown"] += 1
        category = row.get("task_category") or row.get("previous_task_category")
        group["categories"][
            category if isinstance(category, str) and category in PUBLIC_CATEGORIES else "unspecified"
        ] += 1
    return {
        "schema_version": 1,
        "report": "Agent API operation requests and observed outcomes",
        "window": {"since_inclusive": since.isoformat(), "until_exclusive": until.isoformat()},
        "coverage": coverage or {"source": "local_fixture", "complete_history": False},
        "actors": [{"platform": key[0], "external_user_id": key[1], **group} for key, group in sorted(actors.items())],
        "skipped_invalid_records": skipped,
        "duplicate_event_rows_ignored": duplicates,
        "successful_acquisitions": None,
        "allocated_card_hours": None,
        "measured_utilization": None,
        "notes": [
            "dispatched means the request was dispatched, not that a lock was acquired",
            "book counts booking requests, not current bookings or successful acquisitions",
            "separate retries remain separate requests; only duplicate event IDs are removed",
            "Agents sharing a verified actor are grouped together, never by Agent label",
            "chat, OP releases, automatic expiry and complete holding lifecycles are absent",
            "rotated, missing or unrecorded events can make the selected window incomplete",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Local JSONL fixture or authorized export")
    source.add_argument("--base-url", help="Explicitly trusted Lockbot origin")
    parser.add_argument("--bot-id", type=int)
    parser.add_argument("--key-env", default="LOCKBOT_OP_KEY")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        since, until = instant(args.since), instant(args.until)
        if since >= until or not 1 <= args.max_pages <= 100 or not 1 <= args.page_size <= 100:
            raise ValueError("Invalid time window or page budget")
        if args.input:
            if args.input.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("Local input exceeds 20 MiB; select a smaller export")
            rows = []
            for line in args.input.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append(None)
            coverage = {"source": "local_export", "collection_status": "local_file", "complete_history": False}
        else:
            parsed = urllib.parse.urlsplit(args.base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Use a trusted HTTP(S) base URL without credentials, query or fragment")
            key = os.environ.get(args.key_env, "")
            if not args.bot_id or args.bot_id < 1 or not key:
                raise ValueError("Provide a positive --bot-id and an OP key in the selected environment variable")
            rows, coverage = collect(
                args.base_url, args.bot_id, key, max_pages=args.max_pages, page_size=args.page_size
            )
        print(json.dumps(analyze(rows, since, until, coverage=coverage), indent=2, ensure_ascii=False))
        return 2 if coverage.get("collection_status") == "partial" else 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
