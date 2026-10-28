"""Coarse, bounded admission before personal API parsing and authentication.

Identity budgets remain in the routes. This gate also covers rejected credentials,
which FastAPI dependencies reject before a route-level limiter gets control.
"""

import math
import re
import threading
import time
from collections import OrderedDict

from starlette.responses import JSONResponse

_PATH = re.compile(
    r"^/api/(?:agent/context|resource-auth/bindings(?:/[^/]+)?|bots/[^/]+/(?:commands|resources|usage-declarations(?:/[^/]+)?))/?$"
)


class IngressBudget:
    """Token buckets with a fixed memory cap, keyed only by transport source."""

    def __init__(self, source_limit=600, global_limit=6000, max_sources=4096):
        self.source_limit = source_limit
        self.global_limit = global_limit
        self.max_sources = max_sources
        self.sources = OrderedDict()
        self.total = None
        self.lock = threading.Lock()

    @staticmethod
    def available(bucket, capacity, now):
        return capacity if bucket is None else min(capacity, bucket[0] + (now - bucket[1]) * capacity / 60)

    def check(self, source, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            # An untouched bucket is full after one minute, so it can be forgotten.
            while self.sources and next(iter(self.sources.values()))[1] <= now - 60:
                self.sources.popitem(last=False)
            global_tokens = self.available(self.total, self.global_limit, now)
            bucket = self.sources.get(source)
            source_tokens = self.available(bucket, self.source_limit, now)
            if bucket is None and len(self.sources) >= self.max_sources:
                return False, 60
            if min(global_tokens, source_tokens) < 1:
                wait = max((1 - global_tokens) * 60 / self.global_limit, (1 - source_tokens) * 60 / self.source_limit)
                return False, max(1, math.ceil(wait))
            self.total = (global_tokens - 1, now)
            self.sources[source] = (source_tokens - 1, now)
            self.sources.move_to_end(source)
            return True, 0


class AgentIngressMiddleware:
    def __init__(self, app, budget=None):
        self.app = app
        self.budget = budget or IngressBudget()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _PATH.fullmatch(scope.get("path", "")):
            source = (scope.get("client") or ("unknown", 0))[0]
            allowed, retry = self.budget.check(source)
            if not allowed:
                response = JSONResponse(
                    {"detail": {"code": "ingress_rate_limited", "message": "Too many requests; retry later."}},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
