"""Render low-utilization notices and deliver them after allocator operations."""

import logging
import re
from datetime import datetime

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.core.i18n import t

logger = logging.getLogger(__name__)


class IdleChat:
    def __init__(self, manager, recovery, clock):
        self.manager = manager
        self.recovery = recovery
        self.clock = clock

    def confirm(self, bot, holder, group_id, argument):
        if self.recovery is None:
            return t("idle.confirm_unavailable", config=bot.config, resource=argument)
        parts = argument.split()
        groups = bot.config.get_val("_AUTHORIZED_GROUP_IDS", [])
        if list(groups) == [group_id] and (
            len(parts) == 1 or len(parts) == 2 and re.fullmatch(r"dev[0-9]{1,6}", parts[1])
        ):
            card = int(parts[1][3:]) if len(parts) == 2 else None
            bot_id = bot.config.get_val("BOT_ID")
            notice = self.recovery.confirm(bot, bot_id, holder, None, channel="chat", resource=(bot_id, parts[0], card))
            if notice is not None:
                review_time = datetime.fromtimestamp(notice.review_at).astimezone().strftime("%H:%M:%S %Z")
                return t(
                    "idle.confirmed", config=bot.config, resource=_resource_label(notice.resource), time=review_time
                )
        return t("idle.confirm_unavailable", config=bot.config, resource=argument)

    def send(self, notice, kind):
        bot_id, node, card = notice.resource
        try:
            with self.manager.admit(bot_id) as lease:
                if lease.generation != notice.generation:
                    return False
                bot = lease.bot
                groups = bot.config.get_val("_AUTHORIZED_GROUP_IDS", [])
                if len(groups) != 1:
                    return False
                resource = _resource_label(notice.resource)
                if kind == "warning":
                    if (
                        bot.op_holding_version(
                            bot.published_snapshot().state, node, card, notice.holder, at=self.clock()
                        )
                        != notice.version
                    ):
                        return False
                    body = t("idle.warning", config=bot.config, resource=resource, command=f"keep {resource}")
                    mentions = [notice.holder]
                else:
                    body = t("idle.reclaimed", config=bot.config, resource=resource, holder=notice.holder)
                    mentions = []
                outcome, _ = bot.adapter.send_notification(
                    bot.adapter.build_reply(body + "\n\n", mentions, group_id=groups)
                )
                return outcome == "accepted"
        except (AdmissionFull, BotNotRunning, InstanceRetiring):
            return False
        except Exception:
            logger.warning("Idle recovery notification delivery uncertain")
            return False


def _resource_label(resource):
    _, node, card = resource
    return node if card is None else f"{node} dev{card}"
