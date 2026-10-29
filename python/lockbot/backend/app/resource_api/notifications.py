"""API-originated group notifications.

Why these exist: an agent's actions must be visible to the team alongside human ones. A
lock that silently appears with no trace in chat is the kind of thing that erodes trust in
the whole mechanism.

Three constraints shape the implementation:

- **Sent outside the bot state lock.** ``post_webhook`` retries up to three times with a
  five-second timeout, so a send can take ~18s. Holding the state lock across that would
  block every other command and the expiry sweep. The codebase already does it this way in
  ``_check_and_notify``, which builds its message inside the lock and sends after releasing
  it; this follows that precedent.
- **Best-effort.** A send failure is logged and changes nothing about the HTTP response: the
  state mutation already happened, and reporting failure would be a lie.
- **Reads never notify**, so a polling agent cannot flood the group.

Delivery target: the notification is sent with **no explicit conversation id**, so it goes
to the bot's configured webhook target. That matches the existing operator-kick behaviour.
"""

from __future__ import annotations

import logging
import threading
import time

from lockbot.core.handler import canonical_command
from lockbot.core.i18n import t

logger = logging.getLogger(__name__)

#: Canonical words that change state and therefore must be announced. `query` is absent on
#: purpose.
NOTIFIABLE_COMMANDS = frozenset({"lock", "slock", "unlock", "book", "alias"})

#: Minimum gap between group notifications for one bot. A burst inside this window is
#: coalesced into a single follow-up message carrying a count, rather than one message per
#: mutation. Full per-credential budgets are traffic control's job (task23); this is the
#: per-bot floor that stops an obvious flood.
MIN_NOTIFY_INTERVAL_SECONDS = 10.0


class _NotificationBudget:
    """Per-bot floor on notification frequency, with suppressed events coalesced."""

    def __init__(self, min_interval: float = MIN_NOTIFY_INTERVAL_SECONDS):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_sent: dict[int, float] = {}
        self._suppressed: dict[int, int] = {}

    def claim(self, bot_id: int, now: float | None = None) -> tuple[bool, int]:
        """Return ``(may_send, coalesced_count)``.

        ``coalesced_count`` is how many mutations were suppressed since the last send, so
        the message that does go out can account for them.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._last_sent.get(bot_id)
            if last is not None and (now - last) < self._min_interval:
                self._suppressed[bot_id] = self._suppressed.get(bot_id, 0) + 1
                return False, self._suppressed[bot_id]
            self._last_sent[bot_id] = now
            coalesced = self._suppressed.pop(bot_id, 0)
            return True, coalesced

    def reset(self) -> None:
        with self._lock:
            self._last_sent.clear()
            self._suppressed.clear()


notification_budget = _NotificationBudget()


def should_notify(command_word: str) -> bool:
    return canonical_command(command_word) in NOTIFIABLE_COMMANDS


def notify_group(
    bot,
    *,
    bot_id: int,
    command: str,
    actor_id: str,
    command_word: str,
    agent_label=None,
    purpose=None,
    task_category=None,
) -> bool:
    """Announce an API-originated mutation. Returns whether a message was sent.

    Never raises: the caller has already committed a state change and must not have its
    result changed by a delivery problem.
    """
    may_send, _coalesced = notification_budget.claim(bot_id)
    if not may_send:
        logger.debug("Bot %d: notification coalesced (%d suppressed)", bot_id, _coalesced)
        return False

    config = getattr(bot, "config", None)
    key = "notify.api_alias" if canonical_command(command_word) == "alias" else "notify.api_mutation"
    try:
        text = t(key, config=config, actor=actor_id, command=command).rstrip()
        from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES

        # Never publish legacy free-form notes or Agent names, including on free/alias.
        public_task = TASK_CATEGORIES.get(task_category)
        if public_task:
            text += t("notify.api_task_category", config=config, category=public_task)
        text = text.rstrip() + "\n\n"
        # Built here, then sent with no conversation id so it reaches the bot's configured
        # webhook target. The bot's state lock is already released by this point.
        groups = config.get_val("_AUTHORIZED_GROUP_IDS", []) if config else []
        target = groups if groups else None
        # The actor remains in the text. Never @mention synthetic or unrelated IDs.
        message = bot.adapter.build_reply(text, [], group_id=target)
        bot.adapter.send(message)
        return True
    except Exception:
        logger.warning("Bot %d: failed to announce API operation in the group", bot_id, exc_info=True)
        return False
