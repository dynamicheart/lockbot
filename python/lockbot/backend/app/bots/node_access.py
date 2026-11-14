"""Versioned, offline validation of per-bot node connection metadata."""

import ipaddress
import json
import re
from datetime import datetime

from fastapi import HTTPException, Request

VERSION = 1
_MAX_IMPORT_BYTES = 256 * 1024
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


def node_ids(cluster_config):
    """Use the canonical node keys presented by the allocator."""
    if isinstance(cluster_config, dict):
        return list(cluster_config)
    if isinstance(cluster_config, list):
        return list(dict.fromkeys(node.lower() for node in cluster_config if isinstance(node, str)))
    return []


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii() or "%" in value:
        raise ValueError("Invalid SSH host")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.removesuffix(".").split(".")
        if all(_HOST_LABEL.fullmatch(label) for label in labels) and not all(label.isdigit() for label in labels):
            return value.lower()
        raise ValueError("Invalid SSH host") from None


def parse_access_mapping(raw, allowed_nodes=None):
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(data, dict)
        or data.keys() != {"version", "nodes"}
        or type(data["version"]) is not int
        or data["version"] != VERSION
        or not isinstance(data["nodes"], dict)
    ):
        raise ValueError("Expected a version 1 node access mapping")

    nodes = {}
    for node, entry in data["nodes"].items():
        if not node or len(node) > 256 or (allowed_nodes is not None and node not in allowed_nodes):
            raise ValueError(f"Unknown node ID: {node[:256]!r}")
        if not isinstance(entry, dict) or not {"host"} <= entry.keys() <= {"host", "port"}:
            raise ValueError(f"Node {node!r} must contain only host and optional port")
        port = entry.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"Invalid SSH port for node {node!r}")
        try:
            host = _host(entry["host"])
        except ValueError:
            raise ValueError(f"Invalid SSH host for node {node!r}") from None
        nodes[node] = {"host": host, "port": port}
    return nodes


async def read_access_mapping(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_IMPORT_BYTES:
            raise HTTPException(413, "Node access mapping is too large")
        body.extend(chunk)
    try:
        return parse_access_mapping(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid node access JSON") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def saved_node_access(bot):
    """Only read validated metadata for keys still present in the bot inventory."""
    if not bot.node_access_config:
        return {}
    try:
        allowed_nodes = set(node_ids(json.loads(bot.cluster_configs)))
        saved = parse_access_mapping(bot.node_access_config)
    except (ValueError, TypeError, RecursionError):
        return {}
    return {node: access for node, access in saved.items() if node in allowed_nodes}


def serialize_node_access(nodes):
    return json.dumps({"version": VERSION, "nodes": nodes}, ensure_ascii=False) if nodes else None


def _active_holding(resource, subject, now):
    for holder in resource["holders"]:
        if (
            holder["external_user_id"] == subject
            and holder["expires_at"]
            and datetime.fromisoformat(holder["expires_at"]) > now
        ):
            return True
    return False


def attach_node_access(resources, nodes, subject, now):
    for node in resources:
        access = nodes.get(node["id"])
        if access is None:
            continue
        for unit in node.get("devices", [node]):
            if _active_holding(unit, subject, now):
                unit["node_access"] = access
