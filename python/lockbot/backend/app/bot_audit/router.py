"""Owner-scoped, paginated authorization history."""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.router import AuditLogsPage, _to_out
from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_audit.models import BotAuditLog
from lockbot.backend.app.bots.router import _get_user_bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_api.privacy import audit_private_read, can_read_private_tasks

router = APIRouter(tags=["bots"])


@router.get("/api/bots/{bot_id}/audit-logs", response_model=AuditLogsPage)
def bot_audit_logs(
    bot_id: int,
    response: Response,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    _get_user_bot(bot_id, user, db)
    query = db.query(BotAuditLog).filter(BotAuditLog.target_id == bot_id, BotAuditLog.target_type == "bot")
    rows = (
        query.order_by(BotAuditLog.created_at.desc(), BotAuditLog.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    private = can_read_private_tasks(db, user)
    items = [_to_out(row) for row in rows]
    if private and items:
        audit_private_read(db, user, bot_id=bot_id, surface="management.authorization_audit")
    if not private:
        for item in items:
            detail = item.detail
            item.detail = (
                {key: detail[key] for key in ("source", "platform", "external_user_id") if key in detail}
                if isinstance(detail, dict)
                else None
            )
    return AuditLogsPage(total=query.count(), items=items)
