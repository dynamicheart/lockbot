"""Per-credential traffic budget for the agent-facing API.

The per-IP slowapi limiter and the per-bot admission cap do not bound a single *credential*:
behind a shared egress IP many agents share one limiter bucket, and the admission cap counts
per bot, not per caller. This adds a lightweight in-process sliding-window budget keyed on the
durable binding (the credential identity), with separate allowances for reads (polling
``/resources``) and writes (mutating ``/commands``).

In-process is correct here: lockbot runs as a single process (see
``state_ownership``), so one shared counter sees every request. Memory is bounded — at most
``limit`` timestamps per active credential per kind, pruned on each check.
"""

from __future__ import annotations

import threading
import time

from lockbot.backend.app.config import (
    RESOURCE_API_READ_BUDGET_PER_MINUTE,
    RESOURCE_API_WRITE_BUDGET_PER_MINUTE,
)

#: Fixed one-minute window. The budgets are expressed per minute.
_WINDOW_SECONDS = 60.0


class _CredentialBudget:
    """Sliding-window request budget keyed on ``(credential_id, kind)``.

    ``kind`` is ``"read"`` or ``"write"``. ``check`` returns ``(allowed, retry_after)``; when
    refused, ``retry_after`` is the whole seconds until the oldest in-window request ages out.
    """

    def __init__(self, read_limit: int, write_limit: int, window: float = _WINDOW_SECONDS):
        self.read_limit = read_limit
        self.write_limit = write_limit
        self._window = window
        self._lock = threading.Lock()
        self._hits: dict[tuple[int, str], list[float]] = {}

    def _limit_for(self, kind: str) -> int:
        return self.write_limit if kind == "write" else self.read_limit

    def check(self, credential_id: int, kind: str, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        limit = self._limit_for(kind)
        key = (credential_id, kind)
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self._window]
            if len(hits) >= limit:
                retry_after = max(1, int(self._window - (now - hits[0])) + 1)
                self._hits[key] = hits
                return False, retry_after
            hits.append(now)
            self._hits[key] = hits
            return True, 0

    def reset(self) -> None:
        """Clear all counters (tests). Does not change the configured limits."""
        with self._lock:
            self._hits.clear()


credential_budget = _CredentialBudget(
    read_limit=RESOURCE_API_READ_BUDGET_PER_MINUTE,
    write_limit=RESOURCE_API_WRITE_BUDGET_PER_MINUTE,
)
