"""Shape a locked state snapshot into the agent-facing resource payload.

Design rules this module exists to enforce:

- **One instant.** Every derived value is computed from the single ``captured_at`` the
  snapshot was taken at, never from a fresh ``now()``. Otherwise ``remaining_seconds`` and
  ``expires_at`` can disagree with each other inside one response.
- **Absolute time is emitted, not just relative.** The reference implementation computes an
  absolute expiry and then discards it, emitting only a relative value, and serves it from
  a cache whose reference instant is the build time rather than the request time. A client
  then has to subtract staleness itself, undocumented. Here ``expires_at`` is absolute,
  staleness is exposed as ``age_seconds``, and the correction is documented.
- **Per-caller scheduling only where the caller has a stake.** ``next_event_at`` is null
  when the caller holds nothing and has no booking, so it cannot leak another holder's
  timing to a bystander.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Hardcoded in ``queue_bot._check_and_notify``. Mirrored here so the snapshot can express
#: the claim window; if that constant ever becomes configurable both must move together.
QUEUE_CLAIM_WINDOW_SECONDS = 5 * 60

LOCK_STATUS_DOC = (
    "`lock_status` is the bot's STORED status: idle | exclusive | shared. It is not "
    "effective availability. A holder whose duration has elapsed but whose release the "
    "expiry sweep has not run yet still appears here, which is why each holder also "
    "carries `remaining_seconds` and `expires_at`: a resource with `lock_status` set but "
    "every holder at `remaining_seconds: 0` is about to become idle."
)

#: AC-5.1. `alias` has four properties that make it unlike every other reachable command,
#: and an agent that assumes otherwise will be wrong in ways that are hard to debug.
ALIAS_DOC = (
    "`alias` is reachable so an agent can declare what it is running, but it behaves "
    "unlike the lock operations in four ways. (1) It is bot-GLOBAL and visible to "
    "everyone, not scoped to you: setting it renames the node for every user. (2) It is "
    "NOT covered by the bot state lock, so it carries none of the consistency guarantees "
    "the lock operations and this snapshot do, and it does not appear in this snapshot at "
    "all -- an alias lives in bot configuration, not in lock state. (3) The value is "
    "capped at 15 characters, newlines are rejected, the target must be a known node key, "
    "and an empty value clears it. (4) Persistence is best-effort: the write can succeed "
    "in memory and fail to reach the database silently, so a successful reply does not "
    "guarantee the alias survives a restart. A bot may also have the feature disabled "
    "entirely, which answers with code `aliases_disabled`."
)


def _instant(epoch_seconds) -> str | None:
    if not epoch_seconds:
        return None
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()


def _holder(user_info: dict, captured_at: datetime) -> dict:
    """One holder, with both relative and absolute timing from the captured instant."""
    start_time = user_info.get("start_time") or 0
    duration = int(user_info.get("duration") or 0)
    elapsed = int(captured_at.timestamp()) - int(start_time) if start_time else 0
    remaining = max(duration - elapsed, 0)
    return {
        "external_user_id": user_info.get("user_id"),
        "source": user_info.get("source") if user_info.get("source") in {"chat", "api"} else "unknown",
        "start_time": _instant(start_time),
        "duration_seconds": duration,
        "remaining_seconds": remaining,
        "expires_at": _instant(int(start_time) + duration) if start_time else None,
    }


def _holders(node: dict, captured_at: datetime, platform: str) -> list[dict]:
    return [dict(_holder(u, captured_at), platform=platform) for u in node.get("current_users", []) or []]


def _booking_entry(user_info: dict, position: int) -> dict:
    return {
        "position": position,
        "external_user_id": user_info.get("user_id"),
        "source": user_info.get("source") if user_info.get("source") in {"chat", "api"} else "unknown",
        "notified": bool(user_info.get("is_notified")),
    }


def build_resources(
    *,
    captured_at: datetime,
    state: dict,
    bot_type: str,
    platform: str,
    caller_id: str,
) -> list[dict]:
    """Build the per-resource list. Resource ``id`` values are exactly what commands accept."""
    resources = []
    for node_key, node in state.items():
        if bot_type == "DEVICE":
            devices = []
            for dev in node or []:
                devices.append(
                    {
                        "id": dev.get("dev_id"),
                        "lock_status": dev.get("status", "idle"),
                        "holders": _holders(dev, captured_at, platform),
                    }
                )
            resources.append(
                {
                    "id": node_key,
                    "lock_status": "exclusive" if any(d["lock_status"] != "idle" for d in devices) else "idle",
                    "holders": [],
                    "devices": devices,
                }
            )
            continue

        entry = {
            "id": node_key,
            "lock_status": node.get("status", "idle"),
            "holders": _holders(node, captured_at, platform),
        }
        booking_list = node.get("booking_list")
        if booking_list is not None:
            entry["booking_list"] = [_booking_entry(u, i) for i, u in enumerate(booking_list)]
            entry["caller_queue_position"] = _caller_position(booking_list, caller_id)
            # Computed server-side from current state; nothing the request supplies is read.
            entry["caller_is_queue_head"] = entry["caller_queue_position"] == 0
            # Private: needed to express the claim window, stripped before serialization.
            own_booking = next((u for u in booking_list if u.get("user_id") == caller_id), None)
            if own_booking and own_booking.get("start_time"):
                entry["_caller_booking_started_at"] = datetime.fromtimestamp(
                    int(own_booking["start_time"]), tz=timezone.utc
                )
        if bot_type == "QUEUE":
            entry["claimable_by_you"] = bool(entry.get("caller_is_queue_head") and entry["lock_status"] == "idle")
            entry["claim_command"] = f"lock {node_key}" if entry["claimable_by_you"] else None
        resources.append(entry)
    return resources


def scrub_private(resources: list[dict]) -> list[dict]:
    """Drop internal bookkeeping keys so they never reach the wire."""
    for resource in resources:
        for key in [k for k in resource if k.startswith("_")]:
            resource.pop(key)
    return resources


def _caller_position(booking_list, caller_id) -> int | None:
    for index, user_info in enumerate(booking_list or []):
        if user_info.get("user_id") == caller_id:
            return index
    return None


def compute_next_event(
    *,
    captured_at: datetime,
    resources: list[dict],
    caller_id: str,
    early_notify: bool,
    time_alert_seconds: int,
) -> tuple[str | None, str | None]:
    """Return ``(next_event_at, next_event_kind)`` for this caller only.

    Cases, which the documentation and the skill must both state:

    - Holding a live lock: the caller's own expiry, or its early-warning point when
      ``EARLY_NOTIFY`` is on and the warning has not fired yet. **Exact.**
    - At the head of a booking list with the node idle: the end of the claim window, after
      which the booking is dropped. **Exact**, and the tightest deadline in the system.
    - At the head with the node still occupied: the current holder's expiry. **Exact** for
      when the node frees, though claiming still requires acting inside the window that
      starts then.
    - Further back in the queue: an **estimate only**. A holder ahead can release early and
      move the caller's turn forward unpredictably, so this is a floor, not a promise.
    - No lock and no booking: ``None``. Nothing is scheduled for this caller, and emitting
      another holder's timing here would leak it.
    """
    candidates: list[tuple[datetime, str]] = []

    for resource in resources:
        holder_groups = [resource["holders"]]
        for device in resource.get("devices", []) or []:
            holder_groups.append(device["holders"])

        own_holdings = [h for group in holder_groups for h in group if h["external_user_id"] == caller_id]
        for holding in own_holdings:
            if not holding["expires_at"]:
                continue
            expiry = datetime.fromisoformat(holding["expires_at"])
            candidates.append((expiry, "own_lock_expiry"))
            if early_notify and time_alert_seconds:
                warning = expiry - timedelta(seconds=int(time_alert_seconds))
                if warning > captured_at:
                    candidates.append((warning, "own_early_warning"))

        position = resource.get("caller_queue_position")
        if position is None:
            continue

        if position == 0:
            booking = (resource.get("booking_list") or [])[0]
            if resource["lock_status"] == "idle" and booking.get("notified"):
                # The claim window is running. This is the deadline that actually bites.
                started = _booking_started_at(resource, caller_id)
                if started is not None:
                    candidates.append((started + timedelta(seconds=QUEUE_CLAIM_WINDOW_SECONDS), "claim_window_end"))
            else:
                earliest = _earliest_holder_expiry(resource)
                if earliest is not None:
                    candidates.append((earliest, "node_frees"))
        else:
            earliest = _earliest_holder_expiry(resource)
            if earliest is not None:
                candidates.append((earliest, "queue_estimate"))

    if not candidates:
        return None, None
    moment, kind = min(candidates, key=lambda pair: pair[0])
    return moment.isoformat(), kind


def _booking_started_at(resource: dict, caller_id: str) -> datetime | None:
    """When the caller's claim window opened, i.e. when it was notified as head."""
    return resource.get("_caller_booking_started_at")


def _earliest_holder_expiry(resource: dict) -> datetime | None:
    moments = [
        datetime.fromisoformat(h["expires_at"]) for h in resource.get("holders", []) or [] if h.get("expires_at")
    ]
    return min(moments) if moments else None
