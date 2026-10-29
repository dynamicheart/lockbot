"""Bounded aggregate HTTP telemetry; no raw URL, query, credential or body retention."""

import logging
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.dependencies import get_current_user, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.database import get_db

logger = logging.getLogger(__name__)
SOURCES = (
    "agent",
    "operator",
    "enrollment",
    "webhook",
    "resource_view",
    "management",
    "static",
    "health",
    "monitoring",
    "other",
)
LATENCY_BOUNDS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, math.inf)
_RESOURCE = re.compile(r"^/api/bots/[^/]+/(commands|resources|usage-declarations)(/|$)")
_OPERATOR = re.compile(r"^/api/bots/[^/]+/(op/|(?:opkick|kicklock)/?$|locked-users/?$)")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


def source_for(path):
    if path == "/api/admin/service-traffic" or path.endswith("/service-traffic") or path.endswith("/agent-history"):
        return "monitoring"
    if path == "/api/health":
        return "health"
    if path.startswith("/api/bot-views/"):
        return "resource_view"
    if path.startswith("/api/bots/webhook/"):
        return "webhook"
    if path.startswith("/api/resource-auth/"):
        return "enrollment"
    if path.startswith("/api/op-skill") or _OPERATOR.match(path):
        return "operator"
    if path.startswith(("/api/agent/", "/api/agent-skill")) or _RESOURCE.match(path):
        return "agent"
    if path.startswith("/api/"):
        return "management"
    if path == "/" or path.startswith(("/assets/", "/favicon")):
        return "static"
    return "other"


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@dataclass
class Counters:
    requests: int = 0
    client_errors: int = 0
    server_errors: int = 0
    exceptions: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    elapsed_ms: float = 0
    max_ms: float = 0
    statuses: dict = field(default_factory=lambda: {key: 0 for key in ("1xx", "2xx", "3xx", "4xx", "5xx", "other")})
    latency: list = field(default_factory=lambda: [0] * len(LATENCY_BOUNDS))

    def add(self, status, duration_ms, received, sent, failed):
        self.requests += 1
        self.client_errors += int(400 <= status < 500)
        self.server_errors += int(status >= 500 or failed)
        self.exceptions += int(failed)
        self.request_bytes += max(0, received)
        self.response_bytes += max(0, sent)
        self.elapsed_ms += duration_ms
        self.max_ms = max(self.max_ms, duration_ms)
        key = f"{status // 100}xx"
        self.statuses[key if key in self.statuses else "other"] += 1
        for index, bound in enumerate(LATENCY_BOUNDS):
            if duration_ms <= bound:
                self.latency[index] += 1
                break

    def payload(self):
        cumulative = 0
        p95 = 0
        for count, bound in zip(self.latency, LATENCY_BOUNDS, strict=True):
            cumulative += count
            if self.requests and cumulative >= math.ceil(self.requests * 0.95):
                p95 = min(bound, self.max_ms)
                break
        return {
            "requests": self.requests,
            "client_errors": self.client_errors,
            "server_errors": self.server_errors,
            "exceptions": self.exceptions,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "avg_ms": round(self.elapsed_ms / self.requests, 2) if self.requests else 0,
            "p95_ms": round(p95, 2),
            "error_rate": round(self.server_errors / self.requests, 4) if self.requests else 0,
            "statuses": dict(self.statuses),
        }


class TrafficStats:
    def __init__(self, clock=time.time, *, scoped=True):
        self.clock = clock
        self.started_at = clock()
        self.lock = threading.Lock()
        self.total = Counters()
        self.sources = {source: Counters() for source in SOURCES}
        self.minutes = {}
        self.seconds = {}
        self.routes = {}
        self.scoped = scoped
        self.bots = OrderedDict()
        self.credentials = OrderedDict()
        self.unattributed_requests = 0
        self.operations = Counter()
        self.operation_minutes = {}

    def _ensure_bot(self, bot_id):
        if bot_id not in self.bots:
            if len(self.bots) >= 256:
                evicted_bot, _ = self.bots.popitem(last=False)
                for old_key in [key for key in self.credentials if key[0] == evicted_bot]:
                    del self.credentials[old_key]
            self.bots[bot_id] = TrafficStats(self.clock, scoped=False)
        self.bots.move_to_end(bot_id)
        return self.bots[bot_id]

    def record_operation(self, *, bot_id, source, command, outcome):
        from lockbot.core.command_context import RESOURCE_OPERATIONS

        if (
            type(bot_id) is not int
            or bot_id <= 0
            or source not in {"chat", "agent", "operator"}
            or command not in RESOURCE_OPERATIONS
            or outcome not in {"dispatched", "rejected", "uncertain"}
        ):
            return
        key = (source, command, outcome)
        minute = int(self.clock() // 60)
        with self.lock:
            self._record_operation_locked(key, minute)
            if self.scoped:
                child = self._ensure_bot(bot_id)
                with child.lock:
                    child._record_operation_locked(key, minute)

    def _record_operation_locked(self, key, minute):
        self.operations[key] += 1
        self.operation_minutes = {
            stamp: bucket for stamp, bucket in self.operation_minutes.items() if minute - 59 <= stamp <= minute
        }
        self.operation_minutes.setdefault(minute, Counter())[(key[0], key[2])] += 1

    def _operation_payload(self, now):
        return {
            "total": sum(self.operations.values()),
            "observed_since": _iso(self.started_at),
            "history": [
                {
                    "at": _iso(stamp * 60),
                    "observed": stamp >= int(self.started_at // 60),
                    "sources": {
                        source: sum(
                            self.operation_minutes.get(stamp, {}).get((source, outcome), 0)
                            for outcome in ("dispatched", "rejected", "uncertain")
                        )
                        for source in ("chat", "agent", "operator")
                    },
                }
                for stamp in range(int(now // 60) - 59, int(now // 60) + 1)
            ],
            "rows": [
                {"source": key[0], "command": key[1], "outcome": key[2], "count": count}
                for key, count in sorted(self.operations.items())
            ],
        }

    def record(
        self, *, source, route, status, duration_ms, received=0, sent=0, failed=False, bot_id=None, credential=None
    ):
        now = self.clock()
        minute, second = int(now // 60), int(now)
        duration_ms = max(0, duration_ms)
        with self.lock:
            self.minutes = {key: value for key, value in self.minutes.items() if minute - 59 <= key <= minute}
            self.seconds = {key: value for key, value in self.seconds.items() if second - 59 <= key <= second}
            bucket = self.minutes.setdefault(minute, Counters())
            self.seconds[second] = self.seconds.get(second, 0) + 1
            if route not in self.routes and len(self.routes) >= 100:
                route = "OTHER"
            counters = self.routes.setdefault(route, Counters())
            for target in (self.total, self.sources.get(source, self.sources["other"]), bucket, counters):
                target.add(status, duration_ms, received, sent, failed)
            if self.scoped:
                if isinstance(bot_id, int) and not isinstance(bot_id, bool) and bot_id > 0:
                    self._ensure_bot(bot_id)
                    self.bots[bot_id].record(
                        source=source,
                        route=route,
                        status=status,
                        duration_ms=duration_ms,
                        received=received,
                        sent=sent,
                        failed=failed,
                    )
                    if credential is not None:
                        key = (bot_id, credential["id"])
                        if key not in self.credentials:
                            if len(self.credentials) >= 1024:
                                self.credentials.popitem(last=False)
                            self.credentials[key] = {"identity": dict(credential), "counter": Counters(), "since": now}
                        self.credentials.move_to_end(key)
                        self.credentials[key]["counter"].add(status, duration_ms, received, sent, failed)
                else:
                    self.unattributed_requests += 1

    def snapshot(self, bot_id=None):
        if self.scoped:
            with self.lock:
                options = [{"bot_id": key, "requests": value.total.requests} for key, value in self.bots.items()]
                ranked = [
                    {"bot_id": key[0], **value["identity"], "since": _iso(value["since"]), **value["counter"].payload()}
                    for key, value in self.credentials.items()
                    if bot_id is None or key[0] == bot_id
                ]
                ranked.sort(key=lambda row: row["requests"], reverse=True)
                if bot_id is not None:
                    scoped = self.bots.get(bot_id)
                    result = (scoped or TrafficStats(self.clock, scoped=False)).snapshot()
                    result.update(bot_id=bot_id, credentials=ranked[:50], scoped_attribution=True)
                    result["bot_options"] = options
                    return result
            # The global payload below keeps its existing cumulative counters and history.
        else:
            options, ranked = [], []
        now = self.clock()
        minute, second = int(now // 60), int(now)
        with self.lock:
            recent = sum(value for key, value in self.seconds.items() if second - 59 <= key <= second)
            return {
                "business_operations": self._operation_payload(now),
                "bot_options": options,
                "credentials": ranked[:50],
                "unattributed_requests": self.unattributed_requests,
                "scope_limits": {"bots": 256, "credentials": 1024, "ranking": 50},
                "started_at": _iso(self.started_at),
                "captured_at": _iso(now),
                "uptime_seconds": max(0, int(now - self.started_at)),
                "window_minutes": 60,
                "last_minute": {"requests": recent, "rps": round(recent / 60, 2)},
                "totals": self.total.payload(),
                "sources": [{"source": key, **value.payload()} for key, value in self.sources.items()],
                "routes": [
                    {"route": key, **value.payload()}
                    for key, value in sorted(self.routes.items(), key=lambda pair: pair[1].requests, reverse=True)[:50]
                ],
                "history": [
                    {"at": _iso(stamp * 60), **self.minutes.get(stamp, Counters()).payload()}
                    for stamp in range(minute - 59, minute + 1)
                ],
            }


class ServiceTrafficMiddleware:
    def __init__(self, app, stats):
        self.app, self.stats = app, stats

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status, received, sent, failed = 500, 0, 0, False

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
            return message

        async def counted_send(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, counted_receive, counted_send)
        except BaseException:
            failed = True
            raise
        finally:
            # The router writes a registered path template into scope. Never retain scope.path.
            template = getattr(scope.get("route"), "path", None)
            if not template or not template.startswith("/api/"):
                template = "<static>" if source_for(scope.get("path", "")) == "static" else "<unmatched>"
            method = scope.get("method", "OTHER")
            method = method if method in _METHODS else "OTHER"
            try:
                state = scope.get("state", {})
                actor = state.get("resource_actor")
                bot_id = actor.bot_id if actor is not None else state.get("traffic_bot_id")
                credential = state.get("traffic_credential")
                # A successful bot-scoped management route has completed its existing
                # authorization checks. Failed/unknown routes are never inferred from a URL.
                management = state.get("traffic_management")
                candidate = scope.get("path_params", {}).get("bot_id")
                if bot_id is None and management and status < 400 and "/{bot_id}" in template:
                    try:
                        bot_id = int(candidate)
                        credential = management
                    except (TypeError, ValueError):
                        pass
                if actor is not None:
                    credential = {
                        "id": actor.binding_public_id,
                        "kind": "agent",
                        "user": actor.external_user_id,
                        "platform": actor.platform,
                    }
                elif state.get("traffic_operator") and bot_id:
                    credential = {"id": "op", "kind": "operator", "user": "", "platform": ""}
                self.stats.record(
                    bot_id=bot_id,
                    credential=credential,
                    source=source_for(scope.get("path", "")),
                    route=f"{method} {template}",
                    status=status,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    received=received,
                    sent=sent,
                    failed=failed,
                )
            except Exception:
                logger.warning("Could not update HTTP traffic counters", exc_info=True)


router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/service-traffic")
async def service_traffic(
    request: Request,
    response: Response,
    bot_id: int | None = Query(None, ge=1),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if bot_id is not None:
        from lockbot.backend.app.bots.router import _get_user_bot

        _get_user_bot(bot_id, user, db)
    response.headers["Cache-Control"] = "no-store"
    payload = request.app.state.service_traffic.snapshot(bot_id)
    if payload.get("credentials"):
        from lockbot.backend.app.resource_api.privacy import audit_private_read

        audit_private_read(db, user, bot_id=bot_id, surface="management.global_service_traffic")
    # Resolve display names only when reading the admin dashboard, never on requests.
    from lockbot.backend.app.bots.models import Bot

    ids = [row["bot_id"] for row in payload["bot_options"]]
    names = dict(db.query(Bot.id, Bot.name).filter(Bot.id.in_(ids), Bot.is_deleted.is_(False)).all()) if ids else {}
    payload["bot_options"] = [
        {**row, "name": names[row["bot_id"]]} for row in payload["bot_options"] if row["bot_id"] in names
    ]
    return payload


bot_router = APIRouter(prefix="/api/bots", tags=["bots"])


@bot_router.get("/{bot_id}/service-traffic")
def bot_service_traffic(
    bot_id: int,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.router import _get_user_bot

    _get_user_bot(bot_id, user, db)
    response.headers["Cache-Control"] = "no-store"
    from lockbot.backend.app.resource_api.privacy import audit_private_read, can_read_private_tasks

    payload = request.app.state.service_traffic.snapshot(bot_id)
    payload.pop("bot_options", None)
    if can_read_private_tasks(db, user):
        if payload.get("credentials"):
            audit_private_read(db, user, bot_id=bot_id, surface="management.service_traffic")
    else:
        payload["credentials"] = [
            {key: value for key, value in row.items() if key != "id"} for row in payload.get("credentials", [])
        ]
    return payload


@bot_router.get("/{bot_id}/agent-history")
def agent_history(
    bot_id: int,
    response: Response,
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(25, ge=1, le=100),
    query: str = Query("", max_length=128),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.router import _get_user_bot
    from lockbot.backend.app.resource_api.event_log import read_history

    _get_user_bot(bot_id, user, db)
    response.headers["Cache-Control"] = "no-store"
    from lockbot.backend.app.resource_api.privacy import audit_private_read, can_read_private_tasks

    private = can_read_private_tasks(db, user)
    result = read_history(bot_id, limit=limit, cursor=cursor, query=query, private=private)
    if private and result["items"]:
        audit_private_read(db, user, bot_id=bot_id, surface="management.agent_history")
    result["details_restricted"] = not private
    return result
