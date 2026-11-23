#!/usr/bin/env python3
"""Preview or upload fresh exporter inventory from an approved node mapping."""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCOPE = "node_inventory:write"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def request_json(opener, url, key, method="GET", body=None):
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method,
    )
    with opener.open(request, timeout=10) as response:
        return json.load(response)


def observation(entry, now):
    saved = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
    if saved["source_id"] != entry["source_id"] or saved["coverage"] != "visible":
        raise ValueError("Inventory source or coverage mismatch")
    stamp = datetime.fromisoformat(saved["observed_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or not now - timedelta(minutes=15) <= stamp <= now + timedelta(minutes=2):
        raise ValueError("Inventory is stale or its clock is ahead")
    devices = saved["devices"]
    if not devices or any(not card.get("model") for card in devices):
        raise ValueError("No verified accelerator models")
    counts = Counter((card["model"], card.get("memory_total_bytes")) for card in devices)
    return {
        "observed_at": stamp.isoformat(),
        "scope": saved["scope"],
        "accelerators": [
            {"model": model, "count": count, "memory_bytes_per_card": memory}
            for (model, memory), count in counts.items()
        ],
        "memory_bytes": saved.get("memory_total_bytes"),
        "cpu_arch": (saved.get("cpu") or {}).get("arch"),
    }


def upload(args):
    origin = urlsplit(args.origin)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or origin.username is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or args.bot <= 0
    ):
        raise ValueError("Invalid service origin or bot ID")
    if args.key_file.stat().st_mode & 0o077:
        raise ValueError("Configuration Key file must have private permissions")
    key = args.key_file.read_text(encoding="utf-8").strip()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    if not key or not isinstance(mapping, dict) or not mapping:
        raise ValueError("Missing credential or node mapping")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = args.origin.rstrip("/") + f"/api/bots/{args.bot}/configuration-access"
    configuration = request_json(opener, url, key)
    if SCOPE not in configuration["scopes"] or not set(mapping) <= set(configuration["node_ids"]):
        raise ValueError("Credential or node mapping does not match this bot")

    now = datetime.now(timezone.utc)
    observations, missing = {}, []
    for node, entry in mapping.items():
        try:
            observations[node] = observation(entry, now)
        except (OSError, KeyError, TypeError, ValueError):
            missing.append(node)
    if not observations:
        print(json.dumps({"updated": 0, "skipped": missing}))
        return 1

    body = {"scope": SCOPE, "configuration": {"version": 1, "nodes": observations}}
    preview = request_json(opener, url + "/preview", key, "POST", body)
    touched = [
        node for node in observations if preview["after"]["nodes"].get(node) != preview["before"]["nodes"].get(node)
    ]
    if args.apply and touched:
        request_json(opener, url, key, "PUT", {**body, "receipt": preview["receipt"]})
    result = {"mode": "apply" if args.apply else "preview", "changed_nodes": touched, "skipped": missing}
    if not args.apply:
        result["before"] = {node: preview["before"]["nodes"].get(node) for node in touched}
        result["after"] = {node: preview["after"]["nodes"][node] for node in touched}
    print(json.dumps(result))
    return int(bool(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--bot", required=True, type=int)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Submit after previewing an approved mapping")
    try:
        return upload(parser.parse_args())
    except HTTPError as error:
        print(f"LockBot returned HTTP {error.code}", file=sys.stderr)
    except (URLError, OSError, ValueError, KeyError, TypeError):
        print("Unable to upload inventory. Check the deployment, node mapping, and Key.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
