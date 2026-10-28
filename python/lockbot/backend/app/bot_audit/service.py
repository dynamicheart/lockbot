"""Transactional binding audit; failure rolls back the authorization change."""

import json

from sqlalchemy import delete, select

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.bot_audit.models import BotAuditLog

ACTIONS = ("resource_auth.bind", "resource_auth.unbind")


def write_bot_audit(db, operator, action, *, target_type, target_id, target_name, detail=None):
    db.add(
        BotAuditLog(
            operator_id=operator.id,
            operator_username=operator.username,
            operator_role=operator.role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=json.dumps(detail, ensure_ascii=False) if detail else None,
            result="success",
        )
    )
    db.flush()


def migrate_binding_audit(engine):
    """Move legacy binding rows atomically and idempotently, preserving every stored field."""
    BotAuditLog.__table__.create(engine, checkfirst=True)
    source = AuditLog.__table__
    target = BotAuditLog.__table__
    with engine.begin() as connection:
        rows = connection.execute(select(source).where(source.c.action.in_(ACTIONS))).mappings().all()
        for row in rows:
            old = connection.execute(select(target).where(target.c.legacy_audit_id == row["id"])).mappings().first()
            payload = {k: v for k, v in row.items() if k != "id"}
            if old is None:
                connection.execute(target.insert().values(**payload, legacy_audit_id=row["id"]))
            else:
                assert all(old[k] == v for k, v in payload.items()), "Legacy audit copy differs"
            connection.execute(delete(source).where(source.c.id == row["id"]))
    return len(rows)
