"""Persistent identities for native allocation units."""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint, event, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.core.io import normalize_cluster_configs

CARD_BOTS = {"DEVICE", "QUEUE_DEVICE"}
NODE = -1


class ResourceIdentity(Base):
    __tablename__ = "agent_v2_resource_identities"
    __table_args__ = (UniqueConstraint("bot_id", "node", "slot"),)

    ref: Mapped[str] = mapped_column(String(40), primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    node: Mapped[str] = mapped_column(String(256))
    slot: Mapped[int]
    fingerprint: Mapped[str] = mapped_column(String)


def config_units(bot: Bot, *, node: str | None = None, slot: int | None = None) -> dict[tuple[str, int], str]:
    """The identifiers denote configured allocation units, including logical card slots."""
    try:
        configs = json.loads(bot.cluster_configs)
        if node is not None and isinstance(configs, dict):
            configs = {node: configs[node]} if node in configs else {}
        configs = normalize_cluster_configs(configs)
    except (TypeError, ValueError, AttributeError):
        return {}
    if bot.bot_type not in CARD_BOTS:
        if not isinstance(configs, (list, dict)) or slot not in (None, NODE):
            return {}
        return {(str(name), NODE): bot.bot_type for name in configs if node is None or name == node}
    if not isinstance(configs, dict):
        return {}
    return {
        (str(name), index): json.dumps([bot.bot_type, model], ensure_ascii=False, sort_keys=True)
        for name, devices in configs.items()
        if isinstance(devices, list)
        for index, model in enumerate(devices)
        if slot is None or slot == index
    }


@event.listens_for(Session, "before_flush")
def retire_removed_identities(db: Session, _context, _instances) -> None:
    """Retire references in the same transaction as topology or identity changes."""
    for bot in db.dirty:
        if not isinstance(bot, Bot):
            continue
        attrs = inspect(bot).attrs
        reset = any(attrs[name].history.has_changes() for name in ("uuid", "identity_revision", "is_deleted"))
        if not reset and not any(attrs[name].history.has_changes() for name in ("cluster_configs", "bot_type")):
            continue
        units = {} if reset else config_units(bot)
        for row in db.scalars(select(ResourceIdentity).where(ResourceIdentity.bot_id == bot.id)):
            if units.get((row.node, row.slot)) != row.fingerprint:
                db.delete(row)


def identities(db: Session, bot: Bot) -> dict[tuple[str, int], ResourceIdentity]:
    """Backfill old installations on first use, serialized by the bot authority guard."""
    current = config_units(bot)
    known = {
        (row.node, row.slot): row
        for row in db.scalars(select(ResourceIdentity).where(ResourceIdentity.bot_id == bot.id))
    }
    result = {}
    for unit, fingerprint in current.items():
        row = known.get(unit)
        if row is not None and row.fingerprint != fingerprint:
            db.delete(row)
            db.flush()
            row = None
        if row is None:
            row = ResourceIdentity(
                ref="res_" + uuid4().hex, bot_id=bot.id, node=unit[0], slot=unit[1], fingerprint=fingerprint
            )
            db.add(row)
        result[unit] = row
    return result
