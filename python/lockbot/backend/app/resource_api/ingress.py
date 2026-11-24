"""Coarse, bounded admission before personal API parsing and authentication.

Identity budgets remain in the routes. This gate also covers rejected credentials,
which FastAPI dependencies reject before a route-level limiter gets control.
"""

import asyncio
import hashlib
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

from lockbot.backend.app import config

SOURCE_KEY_KIND = "ip"
V2_RESOURCE_LIST_PATH = "/api/agent/v2/resources"
V2_RESOURCE_LIST_SOURCE_LIMIT = 2400
READ_LANE_LIMITS = {
    "cross_group": {"slots": 12, "max_waiting": 24},
    "targeted": {"slots": 8, "max_waiting": 8},
}
READ_QUEUE_WAIT_SECONDS = 0.2
READS_PER_KEY_IN_FLIGHT = 2
_LEGACY_RESOURCE_LIST = re.compile(r"^/api/(?:agent|bots/[^/]+)/resources/?$")

_PATH = re.compile(
    r"^/api/(?:agent/(?:bindings(?:/recover)?|context|commands|resources|usage-declarations(?:/[^/]+)?"
    r"|v2/(?:commands|resources(?:/(?:events|[^/]+/(?:metrics|details)))?|bindings(?:/[^/]+(?:/complete)?)?))"
    r"|bot-views/[^/]+/state|resource-auth/(?:bindings|credential-challenges)(?:/[^/]+)?"
    r"|bots/[^/]+/(?:commands|resources|usage-declarations(?:/[^/]+)?))/?$"
)


class IngressBudget:
    """Bounded token buckets for sources and v2 resource-list credentials."""

    def __init__(
        self,
        source_limit=600,
        global_limit=6000,
        max_sources=4096,
        *,
        resource_list_source_limit=V2_RESOURCE_LIST_SOURCE_LIMIT,
    ):
        self.source_limit = source_limit
        self.resource_list_source_limit = resource_list_source_limit
        self.global_limit = global_limit
        self.max_sources = max_sources
        self.sources = OrderedDict()
        self.read_credentials = OrderedDict()
        self.total = None
        self.lock = threading.Lock()

    @staticmethod
    def available(bucket, capacity, now):
        return capacity if bucket is None else min(capacity, bucket[0] + (now - bucket[1]) * capacity / 60)

    def check(self, source, now=None, *, resource_list=False, protect_writes=False, read_key=None):
        # Resource reads from a shared office egress have a separate bucket from
        # commands. The broader v2 list uses a larger pre-auth capacity.
        source_lane = "list" if resource_list else "snapshot_read" if protect_writes else "shared"
        source_key = (source_lane, source)
        source_limit = self.resource_list_source_limit if resource_list else self.source_limit
        now = time.monotonic() if now is None else now
        with self.lock:
            if read_key is not None:
                # A single key gets only a share of the ingress capacity, even
                # when a test or administrator chooses small source/global limits.
                key_limit = min(
                    config.RESOURCE_API_READ_BUDGET_PER_MINUTE,
                    max(1, min(self.global_limit, self.resource_list_source_limit) // 4),
                )
                if key_limit <= 0:
                    return False, 60
                while self.read_credentials and next(iter(self.read_credentials.values()))[1] <= now - 60:
                    self.read_credentials.popitem(last=False)
                key_tokens = self.available(self.read_credentials.get(read_key), key_limit, now)
                if key_tokens < 1:
                    return False, max(1, math.ceil((1 - key_tokens) * 60 / key_limit))

            # An untouched bucket is full after one minute, so it can be forgotten.
            while self.sources and next(iter(self.sources.values()))[1] <= now - 60:
                self.sources.popitem(last=False)
            total_tokens = self.available(self.total, self.global_limit, now)
            reserve = min(120, self.global_limit // 10) if protect_writes else 0
            global_tokens = total_tokens - reserve
            bucket = self.sources.get(source_key)
            source_tokens = self.available(bucket, source_limit, now)
            if bucket is None and len(self.sources) >= self.max_sources:
                return False, 60
            if min(global_tokens, source_tokens) < 1:
                wait = max((1 - global_tokens) * 60 / self.global_limit, (1 - source_tokens) * 60 / source_limit)
                return False, max(1, math.ceil(wait))
            self.total = (total_tokens - 1, now)
            self.sources[source_key] = (source_tokens - 1, now)
            self.sources.move_to_end(source_key)
            if read_key is not None:
                self.read_credentials[read_key] = (key_tokens - 1, now)
                self.read_credentials.move_to_end(read_key)
                if len(self.read_credentials) > self.max_sources:
                    self.read_credentials.popitem(last=False)
            return True, 0


class ResourceReadLane:
    """Bounded ASGI-side queue so waiting reads do not occupy API worker threads."""

    def __init__(self, slots, max_waiting, wait_seconds=READ_QUEUE_WAIT_SECONDS):
        self.slots = asyncio.Semaphore(slots)
        self.max_waiting = max_waiting
        self.wait_seconds = wait_seconds
        self.waiting = 0

    async def acquire(self):
        if not self.slots.locked():
            await self.slots.acquire()
            return True
        if self.waiting >= self.max_waiting:
            return False
        self.waiting += 1
        try:
            await asyncio.wait_for(self.slots.acquire(), self.wait_seconds)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.waiting -= 1


def _read_key(scope):
    authorization = next((value for name, value in scope.get("headers", ()) if name == b"authorization"), b"")
    if len(authorization) <= 100 and authorization.startswith(b"Bearer lbv2_"):
        return hashlib.sha256(authorization).digest()
    return None


def _query_busy():
    return JSONResponse(
        {"detail": {"code": "resource_query_busy"}},
        status_code=503,
        headers={"Cache-Control": "no-store", "Retry-After": "1"},
    )


class AgentIngressMiddleware:
    def __init__(self, app, budget=None):
        self.app = app
        self.budget = budget or IngressBudget()
        self.cross_group = ResourceReadLane(**READ_LANE_LIMITS["cross_group"])
        self.targeted = ResourceReadLane(**READ_LANE_LIMITS["targeted"])
        self.reads_in_flight = Counter()

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        is_http = scope["type"] == "http"
        is_get = is_http and scope.get("method") == "GET"
        is_resource_list = is_get and path.rstrip("/") == V2_RESOURCE_LIST_PATH
        is_legacy_list = is_get and bool(_LEGACY_RESOURCE_LIST.fullmatch(path))
        is_watch = is_get and path.rstrip("/") == V2_RESOURCE_LIST_PATH + "/events"
        read_key = _read_key(scope) if is_resource_list else None
        if read_key is not None and self.reads_in_flight[read_key] >= READS_PER_KEY_IN_FLIGHT:
            await _query_busy()(scope, receive, send)
            return
        if is_http and _PATH.fullmatch(path):
            source = (scope.get("client") or ("unknown", 0))[0]
            allowed, retry = self.budget.check(
                source,
                resource_list=is_resource_list,
                protect_writes=is_resource_list or is_legacy_list or is_watch,
                read_key=read_key,
            )
            if not allowed:
                response = JSONResponse(
                    {"detail": {"code": "ingress_rate_limited", "message": "Too many requests; retry later."}},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
                await response(scope, receive, send)
                return
        if not (is_resource_list or is_legacy_list):
            await self.app(scope, receive, send)
            return
        try:
            filters = parse_qs(scope.get("query_string", b""), max_num_fields=24)
        except (UnicodeError, ValueError):
            filters = {}
        targeted = is_legacy_list or any(key in filters for key in (b"resource_ref", b"parent_ref", b"grant_ref"))
        lane = self.targeted if targeted else self.cross_group
        if read_key is not None:
            self.reads_in_flight[read_key] += 1
        try:
            if not await lane.acquire():
                await _query_busy()(scope, receive, send)
                return
            try:
                await self.app(scope, receive, send)
            finally:
                lane.slots.release()
        finally:
            if read_key is not None:
                self.reads_in_flight[read_key] -= 1
                if not self.reads_in_flight[read_key]:
                    del self.reads_in_flight[read_key]
