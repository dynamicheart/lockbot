"""
Audit log query API.

Visibility rules:
  - super_admin : sees ALL records
  - admin       : sees records where operator_id is in the set of
                  {own id} ∪ {ids of users with role="user"}
                  (admins cannot see each other's actions)
  - user        : sees own records (operator_id == self) PLUS anonymous
                  failed-login attempts where operator_username == self
                  (so users can detect brute-force attempts on their account)
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.database import get_db

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditLogOut(BaseModel):
    id: int
    operator_id: int | None
    operator_username: str
    operator_role: str
    action: str
    target_type: str | None
    target_id: int | None
    target_name: str | None
    detail: Any | None  # parsed JSON or raw string
    ip_address: str | None
    result: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogsPage(BaseModel):
    total: int
    items: list[AuditLogOut]


def _parse_detail(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _to_out(log: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=log.id,
        operator_id=log.operator_id,
        operator_username=log.operator_username,
        operator_role=log.operator_role,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        target_name=log.target_name,
        detail=_parse_detail(log.detail),
        ip_address=log.ip_address,
        result=log.result,
        created_at=log.created_at,
    )


@router.get("/logs", response_model=AuditLogsPage)
def list_audit_logs(
    # Filters
    action: str | None = Query(None, description="Filter by action, e.g. 'bot.start'"),
    operator_id: int | None = Query(None),
    operator_username: str | None = Query(None, description="Filter by operator username (partial match)"),
    target_type: str | None = Query(None),
    target_id: int | None = Query(None),
    result: str | None = Query(None, description="'success' or 'failure'"),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    # Pagination
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    # Auth
    operator: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog)

    # --- Visibility scope ---
    if operator.role == "user":
        # Own authenticated actions + anonymous failed-login attempts targeting this account
        q = q.filter(
            or_(
                AuditLog.operator_id == operator.id,
                and_(
                    AuditLog.operator_id.is_(None),
                    AuditLog.operator_username == operator.username,
                    AuditLog.action == "auth.login",
                    AuditLog.result == "failure",
                ),
            )
        )
    elif operator.role != "super_admin":
        # admin: own actions + actions of users they manage (role="user")
        # Also include anonymous records (operator_id IS NULL, e.g. failed logins)
        managed_ids = [u.id for u in db.query(User.id).filter(User.role == "user").all()]
        visible_ids = list({operator.id, *managed_ids})
        q = q.filter(or_(AuditLog.operator_id.in_(visible_ids), AuditLog.operator_id.is_(None)))

    # --- Filters ---
    if action:
        q = q.filter(AuditLog.action == action)
    if operator_id is not None:
        q = q.filter(AuditLog.operator_id == operator_id)
    if operator_username:
        q = q.filter(AuditLog.operator_username.ilike(f"%{operator_username}%"))
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    if target_id is not None:
        q = q.filter(AuditLog.target_id == target_id)
    if result:
        q = q.filter(AuditLog.result == result)
    if start_date:
        q = q.filter(AuditLog.created_at >= start_date)
    if end_date:
        q = q.filter(AuditLog.created_at <= end_date)

    total = q.count()
    items = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * limit).limit(limit).all()

    return AuditLogsPage(total=total, items=[_to_out(i) for i in items])
