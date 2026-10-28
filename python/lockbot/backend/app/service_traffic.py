"""Bounded aggregate HTTP telemetry; no raw URL, query, credential or body retention."""

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response

from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User

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
    if path == "/api/admin/service-traffic":
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
    def __init__(self, clock=time.time):
        self.clock = clock
        self.started_at = clock()
        self.lock = threading.Lock()
        self.total = Counters()
        self.sources = {source: Counters() for source in SOURCES}
        self.minutes = {}
        self.seconds = {}
        self.routes = {}

    def record(self, *, source, route, status, duration_ms, received=0, sent=0, failed=False):
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

    def snapshot(self):
        now = self.clock()
        minute, second = int(now // 60), int(now)
        with self.lock:
            recent = sum(value for key, value in self.seconds.items() if second - 59 <= key <= second)
            return {
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
                self.stats.record(
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
async def service_traffic(request: Request, response: Response, user: User = Depends(require_super_admin)):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.service_traffic.snapshot()
