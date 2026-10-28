"""The resources endpoint: the authoritative read an agent verifies against.

There is **no push delivery of any kind** — no callback, stream, or subscription. Polling
this route is how a client learns about state changes, which is why the payload carries
everything needed to schedule the next poll instead of guessing:

- absolute ``expires_at`` per holder, alongside relative ``remaining_seconds``
- ``age_seconds``, non-zero only when a cached copy was served, so a client can correct a
  stale relative value explicitly rather than silently trusting it
- a per-caller ``next_event_at`` with a ``next_event_kind`` saying how exact it is
- the caller's own queue position and a server-computed head-of-queue flag
- the bot's health, so "nothing has expired" is distinguishable from "nothing will ever
  expire because the bot is broken"

A short-TTL cache sits in front of the locked deep copy, so a burst of rapid polls
collapses into one critical section rather than one per request.
"""

from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.manager import (
    AdmissionFull,
    BotNotRunning,
    InstanceRetiring,
    MayHaveExecuted,
    bot_manager,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.commands import AGENT_ALLOWED_COMMANDS
from lockbot.backend.app.resource_api.snapshot import (
    ALIAS_DOC,
    LOCK_STATUS_DOC,
    QUEUE_CLAIM_WINDOW_SECONDS,
    build_resources,
    compute_next_event,
    scrub_private,
)
from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate
from lockbot.backend.app.resource_api.usage import active_declarations, attach_declarations, declarations_for_state
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.dependencies import ResourceActor, require_resource_actor, reverify_actor
from lockbot.core.platforms.registry import normalize_platform_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bots", tags=["Public API"])

#: Chosen together with the minimum poll interval. A compliant agent polling at its tier's
#: interval mostly hits the cache, so sustained polling costs roughly one locked deep copy
#: per TTL per bot rather than one per request.
SNAPSHOT_CACHE_TTL_SECONDS = 5.0


class _SnapshotCache:
    """Per-bot, per-generation cached raw state with single-flight refresh.

    Keyed on ``(bot_id, generation)`` so a delayed refresh from a retired instance can never
    populate a live entry. On a miss, one caller (the *leader*) computes the snapshot while
    concurrent callers wait once for its result, so a burst of truly concurrent polls takes the
    bot state lock once rather than once per request. A follower that is not served by the
    leader falls back to computing its own copy (bounded: a single wait, never a retry loop).

    An ``invalidate`` that lands while a leader is computing bumps a per-key version so the
    leader will not publish a now-stale entry (the drain-before-retire ordering already prevents
    an in-lease ``put`` from racing retirement; this is a cheap second guard)."""

    def __init__(self, ttl: float = SNAPSHOT_CACHE_TTL_SECONDS):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, int], tuple[datetime, dict]] = {}
        self._inflight: dict[tuple[int, int], threading.Event] = {}
        self._versions: dict[tuple[int, int], int] = {}

    def _fresh(self, entry, now):
        if entry is None:
            return None
        captured_at, state = entry
        if (now - captured_at).total_seconds() > self._ttl:
            return None
        return captured_at, state

    def get(self, bot_id: int, generation: int, now: datetime):
        with self._lock:
            return self._fresh(self._entries.get((bot_id, generation)), now)

    def put(self, bot_id: int, generation: int, captured_at: datetime, state: dict) -> None:
        with self._lock:
            self._entries[(bot_id, generation)] = (captured_at, state)

    def get_or_compute(self, bot_id: int, generation: int, now: datetime, compute):
        """Return ``(captured_at, state, from_cache)``. ``compute`` returns ``(captured_at, state)``
        and is called at most once per refresh by the leader; followers reuse the leader's result."""
        key = (bot_id, generation)
        with self._lock:
            fresh = self._fresh(self._entries.get(key), now)
            if fresh is not None:
                return fresh[0], fresh[1], True
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                version = self._versions.get(key, 0)
                leader = True
            else:
                leader = False

        if leader:
            try:
                captured_at, state = compute()
            except BaseException:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()
                raise
            with self._lock:
                # Do not publish if the generation was invalidated while we computed.
                if self._versions.get(key, 0) == version:
                    self._entries[key] = (captured_at, state)
                self._inflight.pop(key, None)
            event.set()
            return captured_at, state, False

        # Follower: wait once for the leader, then reuse its result if fresh; otherwise compute
        # our own copy. Bounded — a single wait, no retry loop.
        event.wait(self._ttl)
        with self._lock:
            fresh = self._fresh(self._entries.get(key), now)
        if fresh is not None:
            return fresh[0], fresh[1], True
        captured_at, state = compute()
        return captured_at, state, False

    def invalidate(self, bot_id: int) -> None:
        with self._lock:
            for key in [
                k for k in self._entries.keys() | self._inflight.keys() | self._versions.keys() if k[0] == bot_id
            ]:
                self._entries.pop(key, None)
                self._versions[key] = self._versions.get(key, 0) + 1

    def invalidate_generation(self, bot_id: int, generation: int) -> None:
        """Evict only one retired generation, so a delayed retirement hook cannot purge a
        replacement generation's cached snapshot, and bump its version so an in-flight leader
        cannot repopulate it."""
        key = (bot_id, generation)
        with self._lock:
            self._entries.pop(key, None)
            self._versions[key] = self._versions.get(key, 0) + 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._inflight.clear()
            self._versions.clear()


snapshot_cache = _SnapshotCache()

# Evict a generation's cached snapshots when the manager finally retires it (stop/restart/
# delete/failed-eviction). Only the retired generation is evicted, so a delayed hook cannot
# purge a replacement generation's cache.
bot_manager.register_retire_hook(lambda bot_id, generation: snapshot_cache.invalidate_generation(bot_id, generation))


@router.get("/{bot_id}/resources")
@limiter.limit("120/minute")
def get_resources(
    bot_id: int,
    request: Request,
    actor: ResourceActor = Depends(require_resource_actor),
    db: Session = Depends(get_db),
) -> dict:
    """Return the authoritative resource snapshot for this bot, from this caller's view."""
    bot_row = db.get(Bot, bot_id)
    if not bot_row or bot_row.is_deleted:
        raise errors.bot_not_running(bot_id)

    # Per-credential read budget, before admission: a compliant agent polling at its interval
    # stays well under it, but one credential cannot busy-poll without bound.
    allowed, retry_after = credential_budget.check(actor.binding_record_id, "read")
    if not allowed:
        raise errors.rate_limited(retry_after)

    # Per-resource poll tier: enforce the minimum interval for the caller's last-known tier.
    allowed, retry_after = resource_tier_gate.check(actor.binding_record_id)
    if not allowed:
        raise errors.rate_limited(retry_after)

    now = datetime.now(timezone.utc)

    try:
        with (
            bot_manager.admit(bot_id, for_read=True) as lease,
            authority_guard(bot_id, platform=actor.platform, external_user_id=actor.external_user_id),
        ):
            # Re-verify the credential against the fresh binding inside the lease: a
            # restore/delete/revoke may have committed between authentication and here.
            if not reverify_actor(db, request, actor):
                raise errors.credential_invalid()
            generation = lease.generation
            # Single-flight: concurrent misses collapse onto one locked deep copy.
            captured_at, raw_state, from_cache = snapshot_cache.get_or_compute(
                bot_id, generation, now, lease.bot.state_snapshot
            )
            rows = active_declarations(db, bot_row, datetime.utcnow())
            if rows and from_cache:
                # A declaration may refer to a newer hold/booking than the cached state,
                # or a release/reacquire may invalidate it. Join against a fresh instant.
                captured_at, raw_state = lease.bot.state_snapshot()
                from_cache = False
            declarations = declarations_for_state(rows, raw_state, captured_at)
            config = lease.bot.config
            supported = sorted(set(lease.bot.supported_commands()) & AGENT_ALLOWED_COMMANDS)
            early_notify = bool(config.get_val("EARLY_NOTIFY"))
            time_alert = int(config.get_val("TIME_ALERT") or 0)
            # Authoritative health from the admitted generation: a retained failed generation
            # reports error even if its fatal DB status write failed. Otherwise use the DB row.
            health = lease.health or bot_row.status
    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise errors.admission_http_error(exc, bot_id) from None

    platform = normalize_platform_name(bot_row.platform)
    resources = build_resources(
        captured_at=captured_at,
        state=copy.deepcopy(raw_state) if from_cache else raw_state,
        bot_type=bot_row.bot_type,
        platform=platform,
        caller_id=actor.external_user_id,
    )
    attach_declarations(resources, declarations)
    next_event_at, next_event_kind = compute_next_event(
        captured_at=captured_at,
        resources=resources,
        caller_id=actor.external_user_id,
        early_notify=early_notify,
        time_alert_seconds=time_alert,
    )
    scrub_private(resources)

    # Record this poll's server-derived tier so the next poll's interval floor reflects the
    # caller's current stake (holder/head poll fastest, bystanders slowest).
    resource_tier_gate.record(actor.binding_record_id, next_event_kind)

    # age_seconds is measured at response-assembly time (now, re-read here), not request start,
    # so a client correcting a relative remaining_seconds by age_seconds uses the true staleness
    # of the served snapshot. next_event/remaining are all anchored to captured_at.
    age_seconds = max(0.0, round((datetime.now(timezone.utc) - captured_at).total_seconds(), 3))

    from lockbot.core.resource_policy import public_policy

    return {
        "resource_policy": public_policy(raw_state, config, actor.external_user_id, captured_at.timestamp()),
        "bot_id": bot_id,
        "bot_type": bot_row.bot_type,
        "platform": platform,
        # A caller must be able to tell "nothing has expired" from "nothing will ever
        # expire because the bot is broken".
        "health": health,
        "supported_operations": supported,
        "command_examples": _command_examples(bot_row.bot_type, resources, supported),
        "queue_workflow": (
            "book only reserves a queue position, not a lock. Keep polling. When the resource is idle "
            "and claimable_by_you is true, POST its claim_command, then verify your live holding. "
            "Do not finish the task after book or wait for a human/group mention to claim for you."
            if bot_row.bot_type in {"QUEUE", "QUEUE_DEVICE"}
            else None
        ),
        "captured_at": captured_at.isoformat(),
        "age_seconds": age_seconds,
        "actor": actor.as_payload(),
        "next_event_at": next_event_at,
        "next_event_kind": next_event_kind,
        "queue_claim_window_seconds": QUEUE_CLAIM_WINDOW_SECONDS
        if bot_row.bot_type in {"QUEUE", "QUEUE_DEVICE"}
        else None,
        "resources": resources,
        "notes": {
            "usage": "Holder and booking usage is declared, untrusted text; null means no valid declaration.",
            "lock_status": LOCK_STATUS_DOC,
            "alias": ALIAS_DOC,
            "attribution": (
                "This snapshot proves what is true at captured_at. It does not attribute "
                "state to any particular preceding command."
            ),
            "staleness": (
                "age_seconds > 0 means this was served from a short-TTL cache. Correct "
                "remaining_seconds by age_seconds, or prefer the absolute expires_at."
            ),
            "delivery": (
                "There is no push delivery: no callback, stream, or subscription. Polling "
                "this route is how you learn about state changes."
            ),
        },
    }


def _command_examples(bot_type, resources, supported):
    key = resources[0]["id"] if resources else "resource1"
    target = f"{key} dev0" if bot_type in {"DEVICE", "QUEUE_DEVICE"} else key
    examples = {
        "lock": f"lock {target} 2h",
        "slock": f"slock {target} 30m",
        "unlock": f"free {target}",
        "book": f"book {target} 2h",
        "query": "query",
        "alias": f"alias {key} note",
    }
    return {name: examples[name] for name in supported if name in examples}
