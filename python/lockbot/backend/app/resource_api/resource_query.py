"""Versioned, bounded read pages under an existing one-user/one-bot credential.

The SDK remains a self-contained standard-library package. The service reuses
its pure display projections, never its Client transport or mutation methods.
The current cold path still copies and scans a complete detached bot state;
this protocol bounds the wire response, not source-side work or cross-bot fanout.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from lockbot.backend.app import config
from lockbot.backend.app.agent_skill.scripts import lockbot_cli as projection

MAX_RESPONSE_BYTES = 65536
MAX_CURSOR_BYTES = 2048
CURSOR_SECONDS = 300
NODE_FIELDS = tuple(field for field in projection.SUMMARY_FIELDS if field not in {"ref", "metrics"})
DEVICE_FIELDS = tuple(field for field in projection.DEVICE_FIELDS if field != "metrics")
_CURSOR_DOMAIN = b"lockbot:resource-query-cursor:v1"
_FIELD_COMPANIONS = {
    "hardware": {"hardware_has_more", "hardware_incomplete"},
    "alias": {"alias_truncated"},
    "matched_device_ids": {"devices_has_more", "device_inventory_complete", "unidentified_devices"},
    "idle_device_ids": {"idle_devices_has_more", "device_inventory_complete", "unidentified_devices"},
    "holdings": {"holdings_has_more", "holding_units"},
    "bookings": {"bookings_has_more", "booking_units"},
}


def capability_descriptor():
    return {
        "version": 1,
        "path": "/api/agent/resources/query",
        "views": ["nodes", "devices"],
        "filters": ["node_id", "model", "mine"],
        "node_fields": list(NODE_FIELDS),
        "device_fields": list(DEVICE_FIELDS),
        "max_limit": 256,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "max_cursor_bytes": MAX_CURSOR_BYTES,
        "cursor_format": "row_mac_v1",
    }


def query_openapi_extra():
    """Document the manually strict query without weakening runtime parsing."""
    fields = (
        "Comma-separated allowlisted display fields. Node fields: "
        + ", ".join(NODE_FIELDS)
        + ". Device fields: "
        + ", ".join(DEVICE_FIELDS)
        + ". Identity, freshness, coverage and uncertainty markers remain mandatory."
    )
    definitions = [
        (
            "view",
            {"type": "string", "enum": ["nodes", "devices"], "default": "nodes"},
            "Nodes or exact-node device records; devices requires node_id.",
        ),
        (
            "node_id",
            {"type": "string", "maxLength": 256},
            "Exact canonical node ID, case-sensitive; never an alias or address-suffix hint.",
        ),
        (
            "model",
            {"type": "string", "maxLength": 80},
            "Observed hardware model, trimmed and case-folded. Missing hardware remains unknown.",
        ),
        (
            "mine",
            {"type": "boolean", "default": False},
            "Separate this verified actor's holdings and bookings; missing or stale ownership remains unknown.",
        ),
        ("fields", {"type": "string"}, fields),
        (
            "limit",
            {"type": "integer", "minimum": 1, "maximum": 256, "default": 20},
            "Maximum emitted rows, also limited by the 64 KiB encoded response including cursor tokens.",
        ),
        (
            "cursor",
            {"type": "string", "maxLength": MAX_CURSOR_BYTES},
            "Opaque signed rq1 live keyset cursor. Reauthenticate each page; changed query/scope/config/generation or "
            "expiry requires restart. Pages do not form an atomic snapshot.",
        ),
    ]
    return {
        "parameters": [
            {"name": name, "in": "query", "required": False, "schema": schema, "description": description}
            for name, schema, description in definitions
        ]
    }


def _error(code, status=400):
    raise HTTPException(status, {"code": code, "message": "Invalid or changed resource query; restart the read."})


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def query_digest(query):
    return hashlib.sha256(_encoded(query)).hexdigest()


def _secret_shaped(value):
    return projection.redacted_values(value) != value or bool(projection._RESOURCE_CREDENTIAL_IN_TEXT.search(value))


def parse_query(params):
    """Validate known keys before budget/admission or inventory construction."""
    pairs = list(params.multi_items())
    allowed = {"view", "node_id", "model", "mine", "fields", "limit", "cursor"}
    if len({key for key, _ in pairs}) != len(pairs) or any(key not in allowed for key, _ in pairs):
        _error("resource_query_invalid")
    values = dict(pairs)
    if sum(len(key.encode()) + len(value.encode()) for key, value in pairs) > 8192:
        _error("resource_query_invalid")
    view = values.get("view", "nodes")
    if view not in {"nodes", "devices"}:
        _error("resource_query_invalid")
    node = values.get("node_id")
    if node is not None and (
        not node or len(node) > 256 or len(node.encode()) > 1024 or not node.isprintable() or _secret_shaped(node)
    ):
        _error("resource_query_invalid")
    if view == "devices" and node is None:
        _error("resource_query_invalid")
    model = values.get("model")
    if model is not None:
        if not model or len(model) > 80 or not model.isprintable() or _secret_shaped(model):
            _error("resource_query_invalid")
        model = model.strip().casefold()
        if not model or len(model) > 80:
            _error("resource_query_invalid")
    mine = values.get("mine", "false")
    if mine not in {"true", "false"}:
        _error("resource_query_invalid")
    limit = values.get("limit", "20")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", limit) or not 1 <= int(limit) <= 256:
        _error("resource_query_invalid")
    available = NODE_FIELDS if view == "nodes" else DEVICE_FIELDS
    fields = values["fields"].split(",") if "fields" in values else list(available)
    if not fields or len(set(fields)) != len(fields) or any(field not in available for field in fields):
        _error("resource_query_invalid")
    cursor = values.get("cursor")
    if cursor is not None and (not cursor or len(cursor.encode()) > MAX_CURSOR_BYTES):
        _error("resource_query_cursor_invalid")
    return {
        "view": view,
        "node_id": node,
        "model": model,
        "mine": mine == "true",
        "fields": sorted(fields),
        "limit": int(limit),
    }, cursor


def _key():
    return hmac.digest(config.JWT_SECRET.encode(), _CURSOR_DOMAIN, "sha256")


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value):
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64(raw) != value:
        raise ValueError
    return raw


def _scope_digest(payload, actor, bot_row, generation):
    # Only public identity/generation metadata enters this opaque binding. No
    # Agent key, credential digest, private locator or local file path is used.
    value = [
        payload["context"]["server"]["instance_id"],
        payload["actor"],
        bot_row.uuid,
        bot_row.identity_revision,
        actor.binding_public_id,
        actor.credential_generation,
        generation,
    ]
    return _b64(hmac.digest(_key(), b"scope\0" + _encoded(value), "sha256"))


def configuration_digest(payload):
    """Bind membership/model/alias changes without restarting on every lock."""
    rows = []
    for node in payload["resources"]:
        rows.append(
            [
                node.get("id"),
                node.get("alias"),
                node.get("accelerators"),
                [[card.get("id"), card.get("dev_model")] for card in node.get("devices", [])],
            ]
        )
    rows.sort(key=lambda row: str(row[0]).encode("utf-8"))
    return hashlib.sha256(_encoded([payload["bot_type"], rows])).hexdigest()


def _cursor_frame(expires):
    # Scope/config/query are recomputed after fresh authentication and bound in
    # the row MAC instead of repeating their digests in every nested cursor.
    return "rqf1." + _b64(_encoded([1, expires]))


def _row_mark(scope, configuration, digest, frame, key):
    return _b64(hmac.digest(_key(), b"row\0" + _encoded([scope, configuration, digest, frame, key]), "sha256"))


def _resume_cursor(frame, key, mark):
    token = "rq1." + _b64(_encoded([frame, key, mark]))
    if len(token.encode()) > MAX_CURSOR_BYTES:
        _error("resource_query_item_too_large", 422)
    return token


def _cursor(scope, configuration, digest, key, expires):
    # One shared frame per page and a small authenticated mark per row avoid
    # repeating scope/config/query metadata for every selected card on the wire.
    frame = _cursor_frame(expires)
    return _resume_cursor(frame, key, _row_mark(scope, configuration, digest, frame, key))


def _cursor_after(token, scope, configuration, digest):
    if token is None:
        return None
    try:
        prefix, body = token.split(".")
        if prefix != "rq1" or len(token.encode()) > MAX_CURSOR_BYTES:
            raise ValueError
        frame, key, mark = json.loads(_unb64(body))
        if (
            not isinstance(frame, str)
            or not frame.startswith("rqf1.")
            or not isinstance(key, str)
            or not key
            or len(key) > 256
            or not key.isprintable()
            or not isinstance(mark, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", mark) is None
            or _resume_cursor(frame, key, mark) != token
        ):
            raise ValueError
        value = json.loads(_unb64(frame[5:]))
        if (
            not isinstance(value, list)
            or len(value) != 2
            or value[0] != 1
            or type(value[0]) is not int
            or type(value[1]) is not int
            or _cursor_frame(value[1]) != frame
        ):
            raise ValueError
        if (
            not hmac.compare_digest(mark, _row_mark(scope, configuration, digest, frame, key))
            or value[1] <= time.time()
        ):
            _error("resource_query_restart_required", 409)
        return key
    except (ValueError, TypeError, UnicodeError):
        _error("resource_query_cursor_invalid")


def _own_snapshot(snapshot):
    """Pure own-role projection, matching the standalone inventory semantics."""
    actor = snapshot["context"]["actor"]
    holdings, bookings = [], []
    for node in snapshot["resources"]:
        cards = node.get("devices")
        units = cards if isinstance(cards, list) else [node]
        for unit in units:
            if not isinstance(unit, dict):
                continue
            entry = {
                "node": node["id"],
                "alias": node.get("alias"),
                "device": unit.get("id") if cards is not None else None,
                "allocation": "card" if cards is not None else "whole_node",
                "model": unit.get("dev_model") if cards is not None else None,
                "accelerators": node.get("accelerators") if cards is None else None,
            }
            for field, destination in (("holders", holdings), ("booking_list", bookings)):
                records = unit.get(field)
                if not isinstance(records, list):
                    continue
                own = next(
                    (
                        row
                        for row in records
                        if isinstance(row, dict)
                        and row.get("platform", actor["platform"]) == actor["platform"]
                        and row.get("external_user_id") == actor["external_user_id"]
                    ),
                    None,
                )
                if own is None:
                    continue
                if field == "holders":
                    destination.append(
                        {**entry, "mode": unit.get("lock_status"), "remaining_time": own.get("remaining_time")}
                    )
                else:
                    position = own.get("position")
                    destination.append(
                        {
                            **entry,
                            "mode": None,
                            "queue_rank": position + 1 if type(position) is int and position >= 0 else None,
                            "claimable_by_you": unit.get("claimable_by_you")
                            if type(unit.get("claimable_by_you")) is bool
                            else None,
                        }
                    )
    return {
        **{key: value for key, value in snapshot.items() if key != "resources"},
        "scope": "own",
        "ownership_coverage": projection.ownership_coverage(snapshot, snapshot["context"]),
        "holdings": holdings,
        "bookings": bookings,
    }


def _valid_for_seconds(payload, query):
    """A short display lifetime, never continued possession or write authority."""
    age = projection.metric_number(payload.get("age_seconds"))
    if age is None or age < 0 or payload.get("health") != "running":
        return 0.0
    remaining = max(0.0, 5.0 - age)
    if not query["mine"]:
        return remaining
    actor = payload["context"]["actor"]
    captured = datetime.fromisoformat(payload["captured_at"].replace("Z", "+00:00"))
    for node in payload["resources"]:
        if query["node_id"] is not None and node.get("id") != query["node_id"]:
            continue
        for unit in node.get("devices", [node]):
            for holder in unit.get("holders", []) if isinstance(unit.get("holders"), list) else []:
                if not isinstance(holder, dict) or (
                    holder.get("platform", actor["platform"]) != actor["platform"]
                    or holder.get("external_user_id") != actor["external_user_id"]
                ):
                    continue
                seconds = projection.metric_number(holder.get("remaining_seconds"))
                if seconds is not None:
                    remaining = min(remaining, max(0.0, seconds - age))
                expiry = holder.get("expires_at")
                if isinstance(expiry, str):
                    try:
                        instant = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                        if instant.tzinfo is not None:
                            remaining = min(remaining, max(0.0, (instant - captured).total_seconds() - age))
                    except ValueError:
                        pass
    if payload.get("next_event_kind") in {"own_lock_expiry", "own_early_warning", "claim_window_end"}:
        try:
            event = datetime.fromisoformat(payload["next_event_at"].replace("Z", "+00:00"))
            if event.tzinfo is not None:
                remaining = min(remaining, max(0.0, (event - captured).total_seconds() - age))
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    return remaining


def _project(payload, query):
    snapshot = copy.deepcopy(payload)
    age = snapshot.get("age_seconds")
    snapshot["snapshot_fresh_for_read"] = (
        type(age) in (int, float)
        and math.isfinite(age)
        and 0 <= age <= 5
        and snapshot.get("health") == "running"
        and _valid_for_seconds(payload, query) > 0
    )
    all_resources = snapshot["resources"]
    examined_devices = sum(len(row.get("devices", [])) for row in all_resources)
    if query["node_id"] is not None:
        snapshot["resources"] = [row for row in all_resources if row.get("id") == query["node_id"]]
    coverage = dict.fromkeys(
        (
            "unknown_hardware_nodes",
            "unidentified_nodes",
            "unknown_ownership_units",
            "unknown_model_devices",
            "unidentified_devices",
            "unknown_ownership_devices",
        ),
        0,
    )
    coverage.update(
        total_devices=None,
        matched_devices=None,
        examined_nodes=len(all_resources),
        examined_devices=examined_devices,
        projection_complete=False,
    )
    try:
        if query["view"] == "devices":
            projected = projection.device_summary_rows(
                snapshot, snapshot["context"], node=query["node_id"], model=query["model"], mine=query["mine"]
            )
            rows = projected["rows"]
            for key in (
                "projection_complete",
                "total_devices",
                "matched_devices",
                "unidentified_devices",
                "unknown_model_devices",
                "unknown_ownership_devices",
            ):
                coverage[key] = projected[key]
            coverage["unknown_ownership_units"] = projected["unknown_ownership_devices"]
        else:
            if query["mine"]:
                projection.format_lease_times(snapshot)
                snapshot = _own_snapshot(snapshot)
            rows, notes, complete = projection.resource_summary_projection(
                {"result": snapshot, "_approved_origin": "https://resource-query.invalid"}, model=query["model"]
            )
            for key in ("unknown_hardware_nodes", "unidentified_nodes", "unknown_ownership_units"):
                coverage[key] = notes[key]
            coverage["projection_complete"] = complete and snapshot["snapshot_fresh_for_read"]
            for row in rows:
                row.pop("ref", None)
            identified = [row for row in rows if not _secret_shaped(row["node"])]
            if len(identified) != len(rows):
                coverage["unidentified_nodes"] += len(rows) - len(identified)
                coverage["projection_complete"] = False
                rows = identified
            if query["mine"] and not coverage["projection_complete"]:
                # Historical or incomplete own-role observations must not turn
                # into affirmative current ownership through field selection.
                coverage["unknown_ownership_units"] = max(1, coverage["unknown_ownership_units"])
                rows = [
                    {
                        "node": row["node"],
                        "unit": row["unit"],
                        "snapshot_fresh": False,
                        "observation_incomplete": True,
                        "holding_units": None,
                        "booking_units": None,
                    }
                    for row in rows
                ]
    except projection.ClientError as error:
        missing = error.code == "resource_reference_node_unavailable"
        _error(
            "resource_query_target_missing" if missing else "resource_query_projection_invalid", 404 if missing else 422
        )
    mandatory = (
        {"node", "unit", "snapshot_fresh", "observation_incomplete"}
        if query["view"] == "nodes"
        else {"id", "snapshot_fresh"}
    )
    selected = []
    for row in rows:
        # A clipped array or public label must never look exhaustive merely
        # because its companion marker was omitted by JSON field selection.
        fields = set(query["fields"]) | mandatory
        for field in query["fields"]:
            if field in row:
                fields.update(_FIELD_COMPANIONS.get(field, ()))
        selected.append(
            {key: row[key] for key in fields if key in row}
            | ({"ownership_match_unknown": True} if row.get("ownership_match_unknown") else {})
        )
    rows = selected
    sort_key = (
        (lambda row: row["node"].encode("utf-8"))
        if query["view"] == "nodes"
        else (lambda row: projection.device_summary_sort_key(row["id"]))
    )
    return projection.redacted_values(sorted(rows, key=sort_key)), coverage


def _pack_page(payload, query, *, after, scope, configuration, digest, generation):
    """Pack one detached projection, counting all framing and cursor bytes."""
    rows, coverage = _project(payload, query)
    if after is not None:
        key = after.encode("utf-8") if query["view"] == "nodes" else projection.device_summary_sort_key(after)
        rows = [
            row
            for row in rows
            if (
                row["node"].encode("utf-8")
                if query["view"] == "nodes"
                else projection.device_summary_sort_key(row["id"])
            )
            > key
        ]
    expires = int(time.time()) + CURSOR_SECONDS
    frame = _cursor_frame(expires)
    selected, tokens = [], []
    result = {
        "protocol_version": 1,
        "context": payload["context"],
        "view": query["view"],
        "query": query,
        "query_digest": digest,
        "captured_at": payload["captured_at"],
        "aliases_observed_at": payload["aliases_observed_at"],
        "age_seconds": payload["age_seconds"],
        "valid_for_seconds": _valid_for_seconds(payload, query),
        "health": payload["health"],
        "rows": selected,
        "coverage": coverage,
        "has_more": bool(rows),
        "traversal_exhausted": not rows,
        "next_cursor": None,
        "row_cursors": tokens,
        "cursor_frame": frame,
        "scope_generation": generation,
        "configuration_digest": configuration,
    }

    def encoded():
        more = len(selected) < len(rows)
        last_key = (
            selected[-1]["node"]
            if selected and query["view"] == "nodes"
            else (str(selected[-1]["id"]) if selected else None)
        )
        result.update(
            has_more=more,
            traversal_exhausted=not more,
            next_cursor=_resume_cursor(frame, last_key, tokens[-1]) if more and tokens else None,
        )
        return _encoded(result)

    for row in rows[: query["limit"]]:
        key = row["node"] if query["view"] == "nodes" else str(row["id"])
        selected.append(row)
        # Validate the full continuation bound once per emitted key as well as
        # packing only its small MAC. The SDK can reconstruct a trimmed tail.
        full_cursor = _cursor(scope, configuration, digest, key, expires)
        tokens.append(json.loads(_unb64(full_cursor[4:]))[2])
        if len(encoded()) > MAX_RESPONSE_BYTES:
            selected.pop()
            tokens.pop()
            if not selected:
                _error("resource_query_item_too_large", 422)
            break
    body = encoded()
    if len(body) > MAX_RESPONSE_BYTES:
        _error("resource_query_item_too_large", 422)
    return result


def _aged_payload(payload):
    captured = datetime.fromisoformat(payload["captured_at"].replace("Z", "+00:00"))
    if captured.tzinfo is None:
        _error("resource_query_projection_invalid", 422)
    age = max(payload["age_seconds"], 0.0, (datetime.now(timezone.utc) - captured).total_seconds())
    return {**payload, "age_seconds": round(age, 3)}


def query_response(payload, query, cursor, *, actor, bot_row, generation, headers=None):
    """Project outside the state lock, retaining admission/authority until return."""
    digest = query_digest(query)
    configuration = configuration_digest(payload)
    scope = _scope_digest(payload, actor, bot_row, generation)
    after = _cursor_after(cursor, scope, configuration, digest)
    observed = _aged_payload(payload)
    # Projection/packing can outlive the five-second state freshness window.
    # Rebuild once when it crosses that boundary, including --mine expansion,
    # so an old own row cannot be serialized as a fresh/current ownership claim.
    for _attempt in range(2):
        result = _pack_page(
            observed,
            query,
            after=after,
            scope=scope,
            configuration=configuration,
            digest=digest,
            generation=generation,
        )
        current = _aged_payload(payload)
        if (_valid_for_seconds(observed, query) > 0) != (_valid_for_seconds(current, query) > 0):
            observed = current
            continue
        result["age_seconds"] = current["age_seconds"]
        result["valid_for_seconds"] = _valid_for_seconds(current, query)
        body = _encoded(result)
        if len(body) > MAX_RESPONSE_BYTES:
            # A final age's decimal framing can grow. Repack with that final age,
            # still from the same source observation and without another lease.
            observed = current
            continue
        response_headers = {key.lower(): value for key, value in (headers or {}).items()}
        response_headers["cache-control"] = "no-store"
        vary = {part.strip() for part in response_headers.get("vary", "").split(",") if part.strip()}
        response_headers["vary"] = ", ".join(sorted(vary | {"Authorization"}))
        # No resource_snapshot/prepared full write state is returned.
        return JSONResponse(json.loads(body), headers=response_headers)
    _error("resource_query_projection_unstable", 409)
