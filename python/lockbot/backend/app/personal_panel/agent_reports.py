"""Caller-scoped read-only reports from explicitly configured local adapters."""

import hashlib
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import BoundedSemaphore
from types import MappingProxyType

from fastapi import Depends, HTTPException, Request, Response

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.traffic import credential_budget
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.dependencies import _bearer_token
from lockbot.backend.app.resource_auth.http_errors import ResourceAuthenticationError

_CACHE_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}

PROTOCOL = "lockbot.personal-report.v1"
PREFIX = "/api/personal-panel/agent/extensions/"
_SECRET = re.compile(r"\b(?:lbr_|lbk_|lba1\.)[A-Za-z0-9_.-]+")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
_SCOPE_FIELDS = ("issuer", "platform", "realm", "subject", "scope", "ref")
_MAX_GROUPS = 16
_MAX_ALLOCATIONS = 128


def _text(value, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or not value.isprintable()
        or _SECRET.search(value)
    ):
        raise ValueError("Invalid report text")
    return value


def _instant(value):
    if value is None:
        return None
    value = _text(value, 64)
    if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
        raise ValueError("Report timestamps require an offset")
    return value


def _project(groups):
    if not isinstance(groups, list) or len(groups) > _MAX_GROUPS:
        raise ValueError("Invalid report groups")
    result = []
    seen = set()
    remaining = _MAX_ALLOCATIONS
    now = datetime.now(timezone.utc)
    for group in groups:
        group_id = hashlib.sha256(_text(group["group_id"], 512).encode()).hexdigest()
        if group_id in seen or group["status"] not in ("ok", "unknown"):
            raise ValueError("Invalid report group")
        seen.add(group_id)
        allocations = group["allocations"]
        if not isinstance(allocations, list) or len(allocations) > remaining:
            raise ValueError("Invalid report allocations")
        remaining -= len(allocations)
        source_at = _instant(group.get("source_at"))
        status = group["status"]
        if source_at:
            age = (now - datetime.fromisoformat(source_at.replace("Z", "+00:00"))).total_seconds()
            if not -30 <= age <= 300:
                status = "unknown"
        entries = []
        for row in allocations if status == "ok" else []:
            if row["kind"] not in ("hold", "reservation"):
                raise ValueError("Invalid allocation kind")
            entries.append(
                {
                    "kind": row["kind"],
                    "node": _text(row["node"], 128),
                    "device": _text(row["device"], 64) if row.get("device") is not None else None,
                    "expires_at": _instant(row.get("expires_at")),
                }
            )
        result.append(
            {
                "id": group_id,
                "label": _text(group["label"], 80),
                "status": status,
                "source_at": source_at,
                "allocations": entries,
            }
        )
    return result


@dataclass(frozen=True)
class Extension:
    id: str
    label: str
    revision: str
    scopes: frozenset[tuple[str, ...]]
    report: Callable[[Mapping], list]

    @classmethod
    def configured(cls, item, adapter):
        id_ = item["id"]
        if not isinstance(id_, str) or not _ID.fullmatch(id_):
            raise ValueError("Invalid Agent report extension ID")
        grants = item["grants"]
        if not isinstance(grants, list) or not 1 <= len(grants) <= 256:
            raise ValueError("Agent report extensions require explicit grants")
        scopes = []
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) != set(_SCOPE_FIELDS):
                raise ValueError("Invalid Agent report grant")
            scopes.append(tuple(_text(grant[field], 512) for field in _SCOPE_FIELDS))
        if not callable(getattr(adapter, "report", None)):
            raise ValueError("Agent report adapter requires report(grant)")
        return cls(id_, _text(item["label"], 80), _text(item["revision"], 64), frozenset(scopes), adapter.report)

    def descriptor(self):
        return {
            "id": self.id,
            "label": self.label,
            "revision": self.revision,
            "mode": "read_only",
            "read_path": PREFIX + self.id,
        }


class AgentReports:
    def __init__(self, provider, extensions):
        self.provider = provider
        self.extensions = {item.id: item for item in extensions}
        if len(self.extensions) != len(extensions) or not 1 <= len(extensions) <= 8:
            raise ValueError("Configure one to eight unique Agent report extensions")
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-report")
        self.capacity = BoundedSemaphore(4)

    def authorized(self, grant):
        scope = tuple(grant[field] for field in _SCOPE_FIELDS)
        return [item for item in self.extensions.values() if scope in item.scopes]

    def discover_binding(self, db, row):
        from .native import ProviderError

        try:
            return self.discover(self.provider._grant(db, row))
        except ProviderError:
            return None

    def discover(self, grant):
        items = [item.descriptor() for item in self.authorized(grant)]
        if items:
            return {"protocol": PROTOCOL, "credential_mode": "same_origin_native", "items": items}
        return None

    def read(self, grant, extension_id):
        item = self.extensions.get(extension_id)
        if item is None or item not in self.authorized(grant):
            raise HTTPException(404, headers=_CACHE_HEADERS)
        groups = []
        status = "unknown"
        if self.capacity.acquire(blocking=False):
            try:
                future = self.executor.submit(item.report, MappingProxyType(dict(grant)))
            except RuntimeError:
                self.capacity.release()
            else:
                future.add_done_callback(lambda _future: self.capacity.release())
                try:
                    groups = _project(future.result(timeout=5))
                    status = "ok" if all(group["status"] == "ok" for group in groups) else "unknown"
                except Exception:
                    groups = []
        return {"protocol": PROTOCOL, "id": item.id, "revision": item.revision, "status": status, "groups": groups}

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def mount_agent_reports(app, origin, provider, extensions):
    from .native import ProviderError

    reports = AgentReports(provider, extensions)
    app.state.agent_personal_reports = reports
    app.state.agent_personal_report_origin = origin

    @app.get(PREFIX + "{extension_id}", include_in_schema=False)
    def read_extension(extension_id: str, request: Request, response: Response, db=Depends(get_db)):
        if request.headers.get("origin") or request.url.query or str(request.base_url).rstrip("/") != origin:
            raise HTTPException(404, headers=_CACHE_HEADERS)
        token = _bearer_token(request)
        row = service.authenticate_for_any_bot(db, token=token)
        if row is None:
            raise ResourceAuthenticationError("Invalid credential", "credential_invalid")
        allowed, retry = credential_budget.check(row.id, "read")
        if not allowed:
            error = errors.rate_limited(retry)
            error.headers = {**(error.headers or {}), **_CACHE_HEADERS}
            raise error
        bot = db.get(Bot, row.bound_bot_id)
        if bot is None or bot.status != "running" or not bot.resource_auth_enabled:
            raise HTTPException(404, headers=_CACHE_HEADERS)
        request.state.traffic_bot_id = bot.id
        request.state.traffic_credential = {
            "id": row.public_id,
            "kind": "agent",
            "user": row.external_user_id,
            "platform": row.platform,
        }
        db.rollback()
        try:
            grant = provider.authenticate(token)
        except ProviderError:
            raise ResourceAuthenticationError("Invalid credential", "credential_invalid") from None
        result = reports.read(grant, extension_id)
        db.rollback()
        try:
            current = provider.authenticate(token)
            row = service.authenticate_for_any_bot(db, token=token)
            if current != grant or row is None:
                raise ProviderError("credential_changed")
        except ProviderError:
            raise ResourceAuthenticationError("Invalid credential", "credential_invalid") from None
        bot = db.get(Bot, row.bound_bot_id)
        if bot is None or bot.status != "running" or not bot.resource_auth_enabled:
            raise HTTPException(404, headers=_CACHE_HEADERS)
        response.headers.update(_CACHE_HEADERS)
        return result
