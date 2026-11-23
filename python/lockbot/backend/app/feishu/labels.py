"""Local Feishu name cache. Callers verify the bot and route before using it."""

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, object_session

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import (
    FeishuApplication,
    FeishuDisplayLabel,
    FeishuGroupMembership,
    FeishuGroupRoute,
)

logger = logging.getLogger(__name__)


class DisplayLabels:
    def __init__(self, app):
        self.identity = (app.id, app.app_id, app.tenant_key, app.bot_open_id)
        self.engine = object_session(app).get_bind()

    def scope(self):
        return dict(zip(("application_id", "app_id", "tenant_key", "bot_open_id"), self.identity, strict=True))

    def load(self, db):
        try:
            return {
                kind: dict(
                    db.query(FeishuDisplayLabel.external_id, FeishuDisplayLabel.name)
                    .filter_by(**self.scope(), kind=kind)
                    .order_by(FeishuDisplayLabel.verified_at.desc())
                    .limit(limit)
                    .all()
                )
                for kind, limit in (("user", 1024), ("group", 256))
            }
        except SQLAlchemyError:
            logger.warning("Could not read cached Feishu display names")
            return {}

    def save(self, kind, labels, observed_at):
        try:
            with Session(self.engine) as db:
                app = db.get(FeishuApplication, self.identity[0])
                if app is None or (app.id, app.app_id, app.tenant_key, app.bot_open_id) != self.identity:
                    return
                records = [
                    dict(
                        self.scope(),
                        kind=kind,
                        external_id=identifier,
                        name=name,
                        verified_at=datetime.fromtimestamp(observed_at, timezone.utc).replace(tzinfo=None),
                    )
                    for identifier, name in labels.items()
                ]
                statement = insert(FeishuDisplayLabel).values(records)
                db.execute(
                    statement.on_conflict_do_update(
                        index_elements=list(FeishuDisplayLabel.__table__.primary_key.columns),
                        set_={"name": statement.excluded.name, "verified_at": statement.excluded.verified_at},
                        where=FeishuDisplayLabel.verified_at <= statement.excluded.verified_at,
                    )
                )
                db.commit()
        except SQLAlchemyError:
            logger.warning("Could not save Feishu display names")


def bot_group_names(db, bots):
    ids = [bot.id for bot in bots if str(bot.platform).lower() == "feishu" and bot.group_id]
    if not ids:
        return {}
    return dict(
        db.execute(
            select(Bot.id, FeishuDisplayLabel.name)
            .join(FeishuGroupRoute, and_(FeishuGroupRoute.bot_id == Bot.id, FeishuGroupRoute.chat_id == Bot.group_id))
            .join(FeishuApplication, FeishuApplication.id == FeishuGroupRoute.application_id)
            .join(
                FeishuDisplayLabel,
                and_(
                    FeishuDisplayLabel.application_id == FeishuApplication.id,
                    FeishuDisplayLabel.app_id == FeishuApplication.app_id,
                    FeishuDisplayLabel.tenant_key == FeishuApplication.tenant_key,
                    FeishuDisplayLabel.bot_open_id == FeishuApplication.bot_open_id,
                    FeishuDisplayLabel.kind == "group",
                    FeishuDisplayLabel.external_id == FeishuGroupRoute.chat_id,
                ),
            )
            .where(Bot.id.in_(ids))
            .where(
                ~select(FeishuGroupMembership.application_id)
                .where(
                    FeishuGroupMembership.application_id == FeishuApplication.id,
                    FeishuGroupMembership.chat_id == FeishuGroupRoute.chat_id,
                    FeishuGroupMembership.app_id == FeishuApplication.app_id,
                    FeishuGroupMembership.tenant_key == FeishuApplication.tenant_key,
                    FeishuGroupMembership.status == "removed",
                )
                .exists()
            )
        ).all()
    )
