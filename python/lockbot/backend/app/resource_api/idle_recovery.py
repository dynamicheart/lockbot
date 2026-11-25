"""Keep one recoverable notice per continuous idle holding."""

import logging
import threading
import time
from dataclasses import dataclass, replace
from secrets import token_urlsafe

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.core.base_bot import HoldingVersionConflict
from lockbot.core.command_context import operation_attempt

logger = logging.getLogger(__name__)

CONFIRMATION_SECONDS = 10 * 60
EXTRA_SECONDS = 10 * 60


@dataclass(frozen=True, slots=True)
class IdleNotice:
    ref: str
    resource: tuple[int, str, int | None]
    holder: str
    channel: str
    version: str
    since: float
    generation: int
    utilization_percent: float
    threshold_percent: float
    delivered_at: float | None = None
    group_delivered: bool = False
    confirmed: bool = False
    active: bool = True

    @property
    def confirm_by(self):
        return self.delivered_at + CONFIRMATION_SECONDS if self.delivered_at is not None else None

    @property
    def review_at(self):
        deadline = self.confirm_by
        return deadline + EXTRA_SECONDS if deadline is not None and self.confirmed else deadline


class IdleRecovery:
    def __init__(self, manager, *, clock=time.time):
        self.manager = manager
        self.clock = clock
        self._lock = threading.Lock()
        self._notices = {}
        self._source_resources = {}

    def reconcile(self, index, evidence):
        """Publish new notices and remove cycles whose supporting observations were lost."""
        with self._lock:
            candidates = {
                resource: row for resource, row in evidence.items() if row.candidate and row.channel in ("chat", "api")
            }
            for resource in self._source_resources.get(index, set()) - candidates.keys():
                self._notices.pop(resource, None)
            self._source_resources[index] = set(candidates)
            created = []
            for resource, row in candidates.items():
                notice = self._notices.get(resource)
                if notice is not None and (notice.holder, notice.since, notice.generation) == (
                    row.holder,
                    row.since,
                    row.generation,
                ):
                    current = replace(
                        notice,
                        version=row.version,
                        channel=row.channel,
                        active=row.active,
                        utilization_percent=row.utilization_percent,
                    )
                    self._notices[resource] = current
                    if (
                        row.active
                        and notice.delivered_at is None
                        and (not notice.active or notice.channel != row.channel)
                    ):
                        created.append(current)
                    continue
                if not row.active:
                    continue
                notice = IdleNotice(
                    token_urlsafe(12),
                    resource,
                    row.holder,
                    row.channel,
                    row.version,
                    row.since,
                    row.generation,
                    row.utilization_percent,
                    row.threshold_percent,
                )
                self._notices[resource] = notice
                created.append(notice)
            return created

    def for_holder(self, holder, bot_ids, *, channel):
        with self._lock:
            return [
                notice
                for notice in self._notices.values()
                if notice.active
                and notice.holder == holder
                and notice.resource[0] in bot_ids
                and (notice.channel == channel or channel == "chat" and notice.group_delivered)
            ]

    def delivered(self, notice, *, group=False):
        """Begin the confirmation window after a transport accepts the reminder."""
        with self._lock:
            current = self._notices.get(notice.resource)
            if current is None or current.ref != notice.ref or not current.active:
                return None
            if current.delivered_at is None or group and not current.group_delivered:
                delivered_at = self.clock() if current.delivered_at is None else current.delivered_at
                current = replace(current, delivered_at=delivered_at, group_delivered=current.group_delivered or group)
                self._notices[notice.resource] = current
            return current

    def confirm(self, bot, bot_id, holder, ref, *, channel, resource=None):
        """Confirm the exact notice from its authenticated holder while the lease is live."""
        if not bot.config.get_val("IDLE_UTILIZATION_CHECK_ENABLED"):
            return None
        notices = self.for_holder(holder, {bot_id}, channel=channel)
        notice = next(
            (
                item
                for item in notices
                if (item.ref == ref or (channel == "chat" and ref is None and resource is not None))
                and resource in (None, item.resource)
            ),
            None,
        )
        if notice is None:
            return None
        _, node, card = notice.resource
        with bot._lock, self._lock:
            current = self._notices.get(notice.resource)
            if current is None or current.ref != notice.ref or not current.active:
                return None
            now = self.clock()
            version = bot.op_holding_version(bot.state.bot_state, node, card, holder, at=now)
            if version != current.version or current.delivered_at is None:
                return None
            if current.confirmed and now < current.review_at:
                return current
            if not current.delivered_at <= now < current.confirm_by:
                return None
            current = replace(current, confirmed=True)
            self._notices[notice.resource] = current
            return current

    def reclaim_ready(self, evidence, max_age):
        """Try exact releases supported by a fresh sample from this observation round."""
        with self._lock:
            possible = [
                (notice, evidence[resource]) for resource, notice in self._notices.items() if resource in evidence
            ]
        return [notice for notice, row in possible if self._reclaim(notice, row, max_age)]

    def _reclaim(self, notice, evidence, max_age):
        if not self._eligible(notice, evidence, max_age):
            return False
        bot_id, node, card = notice.resource

        def permit():
            with self._lock:
                latest = self._notices.get(notice.resource)
                if latest is None or latest.ref != notice.ref or not self._eligible(latest, evidence, max_age):
                    return False
                del self._notices[notice.resource]
                return True

        try:
            with self.manager.admit(bot_id) as lease, operation_attempt(bot_id, "internal", "idle_reclaim"):
                if lease.generation != notice.generation or not lease.bot.config.get_val(
                    "IDLE_UTILIZATION_CHECK_ENABLED"
                ):
                    return False
                return lease.bot.reclaim_idle_holding(node, card, notice.holder, notice.version, permit=permit)
        except (AdmissionFull, BotNotRunning, HoldingVersionConflict, InstanceRetiring):
            return False
        except OSError as error:
            logger.error("Idle recovery state write failed (%s)", type(error).__name__)
            return False

    def _eligible(self, notice, evidence, max_age):
        now = self.clock()
        deadline = notice.review_at
        return (
            deadline is not None
            and notice.active
            and evidence.active
            and evidence.candidate
            and (notice.version, notice.since) == (evidence.version, evidence.since)
            and deadline <= evidence.observed_at <= now <= evidence.observed_at + max_age
        )
