"""The command endpoint: one allowlisted mutation language for agents.

Scope decisions, all fixed by the plan and the user:

- ``take`` is excluded because ``QueueBot.take`` **preempts a live holder**, which is the
  same class of action as ``kickout`` and ``kicklock``. Queue support survives without it:
  ``QueueBot.lock`` accepts an idle node when the caller is first in the booking list, so
  ``book`` then ``lock`` is a complete non-preemptive path.
- ``alias`` is included so an agent can declare what it is running, which is the whole
  point of letting it set one.
- ``bind`` / ``unbind`` / ``bindings`` are refused unconditionally. Enrollment and
  revocation are chat-only, because chat is the only place a trustworthy identity exists.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from lockbot.backend.app.bots.manager import (
    AdmissionFull,
    BotNotRunning,
    InstanceRetiring,
    MayHaveExecuted,
    bot_manager,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.config import RESOURCE_API_MAX_HELD_PER_IDENTITY
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.notifications import notification_target, notify_group, should_notify
from lockbot.backend.app.resource_api.observability import record_api_command
from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.dependencies import ResourceActor, require_resource_actor, reverify_actor
from lockbot.core.base_bot import AcquisitionLimitExceeded
from lockbot.core.handler import (
    BINDING_COMMANDS,
    CommandNotAllowed,
    CommandNotSupported,
    canonical_command,
    execute_text_command,
)
from lockbot.core.platforms.registry import normalize_platform_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bots", tags=["Public API"])

#: Canonical command words an agent credential may reach. Personal operations only: nothing
#: here can evict, preempt, or enroll.
AGENT_ALLOWED_COMMANDS = frozenset({"lock", "slock", "unlock", "query", "book", "alias"})

#: Words we can refuse *before* taking an admission slot, because no bot configuration
#: could make them reachable. Anything else is decided inside the dispatcher, which is the
#: only place that knows this bot's cluster keys — a bare node key is a `query`, and
#: deciding that out here would wrongly reject it.
PRE_REJECTED_COMMANDS = frozenset({"take", "kickout", "kicklock"}) | frozenset(BINDING_COMMANDS)


COMMAND_GUIDE = """## Resource command reference

Send JSON to `POST /api/bots/{bot_id}/commands` using the personal Bearer key. Obtain the
real resource IDs, device indexes and `supported_operations` from `GET /resources` first.
The examples below use placeholder resources; replace them with those returned by your bot.

| Action | NODE | DEVICE | QUEUE | QUEUE_DEVICE |
|---|---|---|---|---|
| Exclusive lock | `lock node1 2h` | `lock node1 dev0 2h` | `lock node1 2h` | `lock node1 dev0 2h` |
| Shared lock | `slock node1 30m` | `slock node1 dev0 30m` | Not supported | Not supported |
| Release own lock | `free node1` | `free node1 dev0` | `free node1` | `free node1 dev0` |
| Join queue | Not supported | Not supported | `book node1 2h` | `book node1 dev0 2h` |
| Cancel own booking | Not applicable | Not applicable | `free node1` | `free node1 dev0` |
| Query | `query` | `query` | `query` | `query` |

- `unlock` and `free` are aliases. Use a specific resource to avoid affecting another one
  of the user's Agents. Bare `free` / `unlock` releases **all this user's holdings and
  bookings on this bot**, including those used by their other Agents sharing the key.
- NODE and QUEUE accept comma-separated resources: `lock node1,node2 30m`,
  `free node1,node2`. DEVICE examples: `lock node1 dev0,2 30m`,
  `lock node1 dev0-3 1h` (inclusive range), `free node1 dev0-3`.
  DEVICE and QUEUE_DEVICE use the same selectors. Indexes are zero-based. Omitting `dev...`
  also selects all cards on QUEUE_DEVICE. On DEVICE, omitting `dev...` selects every device on the
  node: `lock node1 2h` locks all cards; `free node1` releases all your cards there.
- Durations use lowercase `m` (minutes), `h` (hours), `d` (days), e.g. `30m`, `2h`,
  `0.5h`, `1d`. No seconds suffix or combined `1h30m`. If omitted, the bot's configured
  default applies. Configured maximum durations and identity caps still apply.
- For QUEUE / QUEUE_DEVICE: `book node1 2h` reserves a queue position, **not ownership**. Read `/resources`
  until the node is idle and you are first, then `lock node1` within
  `queue_claim_window_seconds`; without an explicit duration, the booked duration applies.
  `free node1` cancels your booking or releases your lock. Do not continue using a node
  just because you previously booked it. Do not finish after booking: keep polling until
  `claimable_by_you` is true, POST its `claim_command`, and verify a live holding before work.
  QUEUE_DEVICE queue and claim fields are nested in `resources[].devices[]`; follow that
  card’s exact `claim_command`, e.g. `lock node1 dev0`. The `book` response includes `next_step`.
  Poll with backoff; no busy loops.
- `alias node1 <note>` changes the shared resource alias only when enabled (at most 15
  characters). It is not an Agent task note; use usage declarations for task purposes.
- `take`, `kickout`, `kicklock` and chat binding commands are **not available to personal
  Agent keys**. Native chat/OP permissions do not imply permission through this API.
- HTTP 200 only means the command was dispatched. Inspect `reply` and read `/resources`
  to verify ownership or queue position. Repeating `lock` extends the lease; do not retry
  it blindly after a timeout. Prefer structured GET `/resources` over parsing query replies.

### Example: two Agents sharing one user's key

Both use the same `bot_id` and saved key. Each acquires a different resource:

```json
{"command":"lock node1 2h","agent_label":"dev-agent","purpose":"Develop feature X"}
```

```json
{"command":"lock node2 1h","agent_label":"test-agent","purpose":"Run regression tests"}
```

Successful `lock` / `slock` / `book` with either optional field automatically registers
that resource's Agent name and purpose in the same API call. No second request is needed.
A failed acquisition does not change the current task. Other commands (including `free`)
never publish these private fields in group notifications. To change a task later without changing
the lease, optionally call `PUT /api/bots/{bot_id}/usage-declarations/node1`:

```json
{"purpose":"Develop feature X","agent_label":"dev-agent","ttl_seconds":3600}
```

Other users receive only preset public task categories. Private notes and Agent names are scoped
to the same verified actor and super administrators.
Registrations are scoped to user authorization + resource, not individual Agent processes.
On DEVICE, the current registration covers the node resource; different cards on the same
node do **not** have independent task registrations. QUEUE_DEVICE saves tasks independently
per card; use `devices[].resource_id` for manual usage updates (e.g. `node1:dev0`).

### Actual HTTP call

Use the trusted server address and bot ID supplied by the user. Load the key privately
from the user's credential store; never put it in URLs, logs, shared files or shell history.
For example, after assigning `base_url`, `bot_id` and `key` securely in the Agent process:

```python
import requests
session = requests.Session()
session.headers["Authorization"] = "Bearer " + key
response = session.post(
    f"{base_url}/api/bots/{bot_id}/commands",
    json={"command": "lock node1 2h", "agent_label": "dev-agent", "purpose": "Develop feature X"},
    timeout=15,
)
response.raise_for_status()
# Honour Retry-After / next_event_at for subsequent checks; a POST reply is not proof.
state = session.get(f"{base_url}/api/bots/{bot_id}/resources", timeout=15)
state.raise_for_status()
# Match state.json()["actor"] against the resource's holders, then use the resource.
```
"""

COMMAND_EXAMPLES = {
    "node_lock": {
        "summary": "NODE / QUEUE: exclusive lock",
        "value": {"command": "lock node1 2h", "agent_label": "dev-agent", "purpose": "Develop feature X"},
    },
    "device_lock": {"summary": "DEVICE: lock one card (index 0)", "value": {"command": "lock node1 dev0 2h"}},
    "device_range": {"summary": "DEVICE: lock inclusive card range", "value": {"command": "lock node1 dev0-3 1h"}},
    "shared_lock": {"summary": "NODE / DEVICE only: shared lock", "value": {"command": "slock node1 30m"}},
    "queue_book": {
        "summary": "QUEUE only: book, then wait for idle and first position",
        "value": {"command": "book node1 2h"},
    },
    "queue_claim": {"summary": "QUEUE only: claim at head using booked duration", "value": {"command": "lock node1"}},
    "release_or_cancel": {
        "summary": "Release own lock or cancel own QUEUE booking",
        "value": {"command": "free node1"},
    },
    "device_release": {"summary": "DEVICE: release only card 0", "value": {"command": "free node1 dev0"}},
    "queue_device_book": {
        "summary": "QUEUE_DEVICE: queue for one card, then claim explicitly",
        "value": {"command": "book node1 dev0 30m", "agent_label": "test-agent", "purpose": "Regression"},
    },
    "query": {"summary": "Text query; GET /resources is structured", "value": {"command": "query"}},
}


class CommandRequest(BaseModel):
    """``extra="forbid"`` so an attempt to smuggle an identity field is a 422."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: str = Field(
        min_length=1,
        max_length=512,
        description="Resource command text. See the operation command reference and named request examples.",
    )
    task_category: str | None = Field(
        default=None, max_length=48, description="Public preset task category; see /api/agent/context."
    )

    @field_validator("task_category")
    @classmethod
    def category(cls, value):
        from lockbot.backend.app.resource_api.usage import validate_task_category

        return validate_task_category(value)

    agent_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[^\r\n\x00-\x1f\x7f]+$",
        description="Optional self-declared Agent name for display, never an identity or quota boundary.",
    )

    purpose: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        pattern=r"^[^\r\n\x00-\x1f\x7f]+$",
        description=(
            "Private task note, visible only to the same verified actor and super administrators. "
            "Successful lock/slock/book also registers resource usage; "
            "other commands do not publish it in group notifications."
        ),
    )


class CommandActor(BaseModel):
    platform: str
    external_user_id: str


class CommandResponse(BaseModel):
    skill_update: dict = Field(default_factory=dict)
    next_step: str | None = None
    bot_id: int
    bot_type: str
    platform: str
    actor: CommandActor
    command: str
    reply: str = Field(
        description=(
            "The bot's reply as text. This reports what the bot said, not a structured "
            "outcome: read GET /resources to verify what is actually true."
        )
    )


@router.post(
    "/{bot_id}/commands",
    response_model=CommandResponse,
    response_model_exclude_none=True,
    summary="Execute a personal resource command",
    description=COMMAND_GUIDE,
)
@limiter.limit("60/minute")
def execute_command_via_api(
    bot_id: int,
    body: Annotated[CommandRequest, Body(openapi_examples=COMMAND_EXAMPLES)],
    request: Request,
    actor: ResourceActor = Depends(require_resource_actor),
    db: Session = Depends(get_db),
) -> CommandResponse:
    """Execute one allowlisted personal command as the verified IM identity."""
    bot_row = db.get(Bot, bot_id)
    if not bot_row or bot_row.is_deleted:
        raise errors.bot_not_running(bot_id)

    text = body.command.strip()
    word = _leading_word(text)
    from lockbot.backend.app.resource_api.event_log import begin_event, describe_targets, observe_own_resources

    begin_event(
        request,
        bot=bot_row,
        operation="command",
        command=text,
        agent_label=body.agent_label,
        purpose=body.purpose,
        task_category=body.task_category,
    )
    request.state.resource_activity_kind = "read" if canonical_command(word) == "query" else "write"

    # Refuse before admission where we can: an administrative or preemptive word should not
    # consume a slot, and the caller gets the same answer whether or not the bot is up.
    if word in PRE_REJECTED_COMMANDS:
        raise errors.command_not_allowed(word)

    # Per-credential write budget, before admission: one credential cannot flood this bot's
    # single state lock regardless of its source IP or the per-bot admission cap.
    allowed, retry_after = credential_budget.check(actor.binding_record_id, "write")
    if not allowed:
        raise errors.rate_limited(retry_after)

    try:
        with (
            bot_manager.admit(bot_id) as lease,
            authority_guard(bot_id, platform=actor.platform, external_user_id=actor.external_user_id),
        ):
            bot = lease.bot

            # Re-verify the credential against the FRESH binding now that the lease is held:
            # a restore/delete/revoke may have committed between authentication (a dependency,
            # before admission) and here. Without this, a just-revoked actor could still execute.
            if not reverify_actor(db, request, actor):
                raise errors.credential_invalid()

            if canonical_command(word) == "alias" and not bot.config.get_val("ALLOW_USER_ALIAS"):
                # Distinguishable from command_not_allowed on purpose (AC-5.1): the caller
                # is permitted, the deployment has the feature off.
                raise errors.aliases_disabled()

            from lockbot.backend.app.resource_api.usage import (
                prepare_command_usage,
                prune_actor_usage,
                register_command_usage,
            )

            before_usage = prepare_command_usage(bot, actor, canonical_command(word), text)
            request.state.agent_event["requested_resources"] = describe_targets(bot, actor, word, text)
            request.state.agent_event["all_owned_resources"] = text.lower() in {"free", "unlock"}
            try:
                request.state.agent_event["stage"] = "dispatching"
                raw_reply = execute_text_command(
                    actor.external_user_id,
                    text,
                    bot,
                    allowed_commands=AGENT_ALLOWED_COMMANDS,
                    acquisition_limit=RESOURCE_API_MAX_HELD_PER_IDENTITY,
                    source="api",
                )
            except AcquisitionLimitExceeded:
                request.state.agent_event["stage"] = "rejected"
                raise errors.identity_cap_reached(RESOURCE_API_MAX_HELD_PER_IDENTITY) from None
            except CommandNotAllowed:
                request.state.agent_event["stage"] = "rejected"
                raise errors.command_not_allowed(word) from None
            except CommandNotSupported:
                request.state.agent_event["stage"] = "rejected"
                raise errors.command_not_supported(word, bot_row.bot_type) from None

            from lockbot.backend.app.resource_api.resources import snapshot_cache

            snapshot_cache.invalidate(bot_id)
            if canonical_command(word) in {"lock", "slock", "book", "unlock", "alias"}:
                resource_tier_gate.invalidate(actor.binding_record_id)
            reply_text = bot.adapter.extract_reply_text(raw_reply)
            request.state.agent_event.update(
                stage="dispatched", reply=reply_text[:2048], reply_truncated=len(reply_text) > 2048
            )
            request.state.agent_event["observed_state"] = observe_own_resources(
                bot,
                actor,
                request.state.agent_event["requested_resources"],
                all_owned=request.state.agent_event["all_owned_resources"],
            )

            if not bot_manager.lease_is_current(lease):
                # Defensive post-check. It detects replacement but cannot undo a mutation
                # that already persisted, so the honest answer is "may have executed".
                logger.warning("Bot %d was replaced under an admitted command", bot_id)
                raise errors.may_have_executed(bot_id)

            try:
                register_command_usage(
                    db,
                    bot,
                    actor,
                    before_usage,
                    agent_label=body.agent_label,
                    purpose=body.purpose,
                    task_category=body.task_category,
                )
                if canonical_command(word) in {"lock", "slock", "book", "unlock", "alias"}:
                    prune_actor_usage(db, bot, actor)
            except Exception:
                db.rollback()
                logger.exception("Bot %s: usage registration failed after command admission", bot_id)
                raise errors.may_have_executed(bot_id) from None

            # The bot method has released the state lock by now, so this send cannot
            # extend a critical section. Still inside the lease, so a drain waits for it.
            if should_notify(word) and bot_row.resource_api_notify_enabled:
                notify_group(
                    bot,
                    bot_id=bot_id,
                    command=text,
                    actor_id=actor.external_user_id,
                    command_word=word,
                    agent_label=body.agent_label,
                    purpose=body.purpose,
                    task_category=body.task_category,
                    target=notification_target(bot_row, lease, bot_manager, sessionmaker(bind=db.get_bind())),
                )

    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise errors.admission_http_error(exc, bot_id) from None

    record_api_command(actor=actor, bot_id=bot_id, command=text)

    from lockbot.backend.app.resource_api.skill import client_skill_notice

    return CommandResponse(
        skill_update=client_skill_notice(request),
        next_step=(
            "book is only a booking, not ownership. Keep polling GET /resources; when claimable_by_you "
            "is true, send claim_command, then verify your holding before using the resource. "
            "If booking was rejected, follow reply and re-check state."
            if canonical_command(word) == "book"
            else None
        ),
        bot_id=bot_id,
        bot_type=bot_row.bot_type,
        platform=normalize_platform_name(bot_row.platform),
        actor=CommandActor(platform=actor.platform, external_user_id=actor.external_user_id),
        command=text,
        reply=reply_text,
    )


def _leading_word(text: str) -> str:
    """The command word as typed, lowercased. Empty text means `query`, as in chat."""
    match = re.match(r"^(\w+)", text, re.I)
    return match.group(1).lower() if match else "query"
