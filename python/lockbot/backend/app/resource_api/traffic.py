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
    RESOURCE_API_TIER_ACTIVE_SECONDS,
    RESOURCE_API_TIER_BOOKING_SECONDS,
    RESOURCE_API_TIER_DISCOVERY_SECONDS,
    RESOURCE_API_WRITE_BUDGET_PER_MINUTE,
)

#: Fixed one-minute window. The budgets are expressed per minute.
_WINDOW_SECONDS = 60.0

#: next_event kinds that mean the caller holds a lock or is at the head of a queue (active).
_ACTIVE_KINDS = frozenset({"own_lock_expiry", "own_early_warning", "claim_window_end", "node_frees"})


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


class _ResourceTierGate:
    """Minimum interval between ``/resources`` polls for one credential, by server-derived tier.

    The tier is taken from the caller's own ``next_event_kind`` on each successful poll: an active
    holder/head polls fastest, a waiting booker slower, a bystander slowest. The gate decision for
    a poll uses the tier recorded on the *previous* poll, so an over-eager caller is shed before
    any work with a precise Retry-After. The first poll is always allowed.
    """

    def __init__(self, active: float, booking: float, discovery: float):
        self.active = active
        self.booking = booking
        self.discovery = discovery
        self._lock = threading.Lock()
        self._last: dict[int, tuple[float, float]] = {}  # credential_id -> (monotonic, floor)

    def floor_for(self, next_event_kind: str | None) -> float:
        if next_event_kind in _ACTIVE_KINDS:
            return self.active
        if next_event_kind == "queue_estimate":
            return self.booking
        return self.discovery

    def check(self, credential_id: int, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            record = self._last.get(credential_id)
        if record is None:
            return True, 0
        last, floor = record
        elapsed = now - last
        if elapsed < floor:
            return False, max(1, int(floor - elapsed) + 1)
        return True, 0

    def record(self, credential_id: int, next_event_kind: str | None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._last[credential_id] = (now, self.floor_for(next_event_kind))

    def invalidate(self, credential_id: int) -> None:
        """Allow one post-mutation verification; the next successful poll restores its tier."""
        with self._lock:
            self._last.pop(credential_id, None)

    def reset(self) -> None:
        with self._lock:
            self._last.clear()


resource_tier_gate = _ResourceTierGate(
    active=RESOURCE_API_TIER_ACTIVE_SECONDS,
    booking=RESOURCE_API_TIER_BOOKING_SECONDS,
    discovery=RESOURCE_API_TIER_DISCOVERY_SECONDS,
)
