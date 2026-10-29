"""Safe API-operation notices with bounded trailing delivery.

The first notice retains synchronous behavior. Later events in the ten-second window
are sent by a bounded worker even if no further API call arrives. Notifications describe
operations, never infer successful acquisition from HTTP 200, and never replay commands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import weakref
from dataclasses import dataclass, field

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.notification_queue import NotificationQueue, PublicEvent
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.core.handler import canonical_command, redact_command_text
from lockbot.core.i18n import t

logger = logging.getLogger(__name__)
NOTIFIABLE_COMMANDS = frozenset({"lock", "slock", "unlock", "book", "alias"})
MIN_NOTIFY_INTERVAL_SECONDS = 10.0
_registered_managers = weakref.WeakSet()
_registration_lock = threading.Lock()


def _route_fingerprint(row):
    fields = [getattr(row, name, None) for name in ("platform", "group_id", "webhook_url", "token", "aes_key")]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


@dataclass(frozen=True)
class NotificationTarget:
    bot_id: int
    uuid: str
    revision: int
    generation: int
    route: str
    language: str
    manager: object = field(compare=False, repr=False, default=None)
    factory: object = field(compare=False, repr=False, default=None)
    cancelled: object = field(compare=False, repr=False, default_factory=threading.Event)

    @property
    def key(self):
        return self.uuid, self.revision, self.generation, self.route, id(self.manager)

    def overflow_text(self, count):
        return t("notify.api_overflow", lang=self.language, count=count)


def notification_target(row, lease, manager, factory):
    with _registration_lock:
        if manager not in _registered_managers:
            manager.register_retire_hook(notification_budget.cancel)
            _registered_managers.add(manager)
    return NotificationTarget(
        row.id,
        row.uuid,
        row.identity_revision,
        lease.generation,
        _route_fingerprint(row),
        lease.bot.config.get_val("LANGUAGE", "zh"),
        manager,
        factory,
    )


def _send_text(bot, text):
    try:
        groups = bot.config.get_val("_AUTHORIZED_GROUP_IDS", []) if bot.config else []
        message = bot.adapter.build_reply(text.rstrip() + "\n\n", [], group_id=groups or None)
        # Bound the native encoded payload too: Unicode JSON escaping and per-line
        # platform fragments can be larger than the plain-text batching budget.
        if len(json.dumps(message, ensure_ascii=True).encode()) > 16384:
            logger.warning("API notification payload exceeded the application encoding limit")
            return "failed"
        outcome, _code = bot.adapter.send_notification(message)
        if outcome != "accepted":
            logger.warning("API notification delivery %s; not retried", outcome)
        return outcome
    except Exception:
        # No response body, exception value, raw command or user text in system logs.
        logger.warning("API notification delivery uncertain; not retried")
        return "unknown"


def _deliver(target, text):
    if target.manager is None or target.factory is None or target.cancelled.is_set():
        return "cancelled"
    try:
        with target.manager.admit(target.bot_id) as lease:
            if lease.generation != target.generation:
                return "cancelled"
            with authority_guard(target.bot_id), target.factory() as db:
                row = db.get(Bot, target.bot_id)
                if (
                    target.cancelled.is_set()
                    or row is None
                    or row.is_deleted
                    or row.status != "running"
                    or not row.resource_api_notify_enabled
                    or row.uuid != target.uuid
                    or row.identity_revision != target.revision
                    or _route_fingerprint(row) != target.route
                ):
                    return "cancelled"
            # Lease fences identity/routing replacement. Release the authority guard before
            # network IO so delayed delivery cannot serialize subsequent resource commands.
            # A send already handed to the platform cannot be recalled by a later disable.
            return _send_text(lease.bot, text)
    except AdmissionFull:
        return "deferred"
    except (BotNotRunning, InstanceRetiring):
        return "cancelled"


notification_budget = NotificationQueue(_deliver, interval=MIN_NOTIFY_INTERVAL_SECONDS)


def should_notify(command_word):
    return canonical_command(command_word) in NOTIFIABLE_COMMANDS


def notify_group(
    bot, *, bot_id, command, actor_id, command_word, agent_label=None, purpose=None, task_category=None, target=None
):
    """Return true only for an accepted immediate send; false also includes queued notices.

    The API response stays unchanged. Only already-rendered public text is retained;
    private task notes and Agent names never enter the pending queue.
    """
    word = canonical_command(command_word)
    if not should_notify(word):
        return False
    from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES

    # Alias's free-form value is not an operational resource identifier or a public task.
    public_command = " ".join(command.split(maxsplit=2)[:2]) if word == "alias" else command
    public_command = redact_command_text(public_command)
    key = "notify.api_alias" if word == "alias" else "notify.api_mutation"
    try:
        text = t(key, config=bot.config, actor=actor_id, command=public_command).rstrip()
        category = TASK_CATEGORIES.get(task_category)
        if category:
            text += t("notify.api_task_category", config=bot.config, category=category)
        if target is None:
            # Compatibility for direct adapter callers. API routes always supply a scoped
            # target; an unscoped direct call is never eligible for later network delivery.
            target = NotificationTarget(bot_id, "", 0, 0, str(id(bot)), bot.config.get_val("LANGUAGE", "zh"))
        return notification_budget.offer(target, PublicEvent(text, word), immediate=lambda body: _send_text(bot, body))
    except Exception:
        logger.warning("Bot %d: could not enqueue API notification", bot_id)
        return False
