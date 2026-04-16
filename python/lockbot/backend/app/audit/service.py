"""
Audit log service — write_audit_log() helper.

Failures are logged as warnings and never propagate to the caller,
so audit recording never breaks the main business flow.
"""

import json
import logging
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from lockbot.backend.app.audit.models import AuditLog

logger = logging.getLogger(__name__)


def _anon_operator(username: str, role: str = "unknown") -> Any:
    """Build a lightweight non-ORM operator for anonymous/failed operations."""
    return SimpleNamespace(id=None, username=username, role=role)


def write_audit_log(
    db: Session,
    operator: Any,
    action: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    target_name: str | None = None,
    detail: dict | str | None = None,
    ip: str | None = None,
    result: str = "success",
) -> None:
    """
    Write one audit log entry.

    :param db:           SQLAlchemy session (already open, caller commits).
    :param operator:     Object with .id, .username, .role  (User ORM or SimpleNamespace).
    :param action:       Dot-namespaced verb, e.g. "auth.login", "bot.start", "user.create".
    :param target_type:  Optional resource type: "user" | "bot".
    :param target_id:    Optional resource primary key.
    :param target_name:  Optional denormalized resource name.
    :param detail:       Optional dict or JSON string with extra context.
    :param ip:           Client IP address (from request.client.host).
    :param result:       "success" (default) or "failure".
    """
    try:
        detail_str: str | None = None
        if isinstance(detail, dict):
            detail_str = json.dumps(detail, ensure_ascii=False)
        elif isinstance(detail, str):
            detail_str = detail

        log = AuditLog(
            operator_id=operator.id,
            operator_username=operator.username,
            operator_role=operator.role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=detail_str,
            ip_address=ip,
            result=result,
        )
        db.add(log)
        db.flush()  # Write within the same transaction as the main operation
    except Exception:
        logger.warning("Failed to write audit log for action=%s operator=%s", action, operator.username, exc_info=True)
