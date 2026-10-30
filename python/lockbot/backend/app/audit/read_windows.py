"""Durable, fixed-window summaries for dedicated note-credential access only."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, case, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.database import Base


class AuditReadWindow(Base):
    __tablename__ = "audit_read_windows"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    boundary: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    first_at: Mapped[datetime] = mapped_column(DateTime)
    last_at: Mapped[datetime] = mapped_column(DateTime)
    request_count: Mapped[int] = mapped_column(Integer)
    returned_count: Mapped[int] = mapped_column(Integer)
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("audit_logs.id"), nullable=True, unique=True)


def record_read(db, user, grant, *, action, bot, bot_id, surface, reason, result, count, ip, now):
    """Each successful caller must commit this transaction before disclosing data.

    A native conflict-safe insert takes SQLite's write lock before reading a bucket.
    Counts are SQL integer increments; no process cache or database JSON functions.
    """
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    now = now.replace(tzinfo=None)

    def iso(value):
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    start = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    boundary = {
        "principal": grant.public_id,
        "generation": grant.generation,
        "scope": grant.scope,
        "issuer_id": user.id,
        "issuer_token_version": grant.issuer_token_version,
        "issuer_current_version": user.effective_token_version,
        "issuer_role": user.role,
        "issuer_password_change_required": user.must_change_password,
        "granted_bots": json.loads(grant.bots),
        "bot_id": bot_id,
        "bot_identity": (
            {"uuid": bot.uuid, "revision": bot.identity_revision, "owner": bot.user_id, "deleted": bot.is_deleted}
            if bot is not None
            else None
        ),
        "surface": surface,
        "action": action,
        "result": result,
        "reason": reason,
        "ip": ip,
    }
    encoded = json.dumps(boundary, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256((start.isoformat() + "\n" + encoded).encode()).hexdigest()
    db.execute(
        insert(AuditReadWindow)
        .values(
            key=key,
            boundary=encoded,
            window_start=start,
            first_at=now,
            last_at=now,
            request_count=0,
            returned_count=0,
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )
    db.execute(
        update(AuditReadWindow)
        .where(AuditReadWindow.key == key)
        .values(
            request_count=AuditReadWindow.request_count + 1,
            returned_count=AuditReadWindow.returned_count + count,
            first_at=case((AuditReadWindow.first_at > now, now), else_=AuditReadWindow.first_at),
            last_at=case((AuditReadWindow.last_at < now, now), else_=AuditReadWindow.last_at),
        )
    )
    window = db.execute(
        select(AuditReadWindow).where(AuditReadWindow.key == key).execution_options(populate_existing=True)
    ).scalar_one()
    log = db.get(AuditLog, window.audit_id) if window.audit_id is not None else None
    if window.audit_id is not None and log is None:
        raise RuntimeError("Disclosure audit summary is missing")
    if log is not None:
        evidence = json.loads(log.detail)
        first = datetime.fromisoformat(evidence["first_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        last = datetime.fromisoformat(evidence["last_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        if (
            (
                log.operator_id,
                log.operator_role,
                log.action,
                log.target_type,
                log.target_id,
                log.ip_address,
                log.result,
                log.created_at,
            )
            != (user.id, user.role, action, "bot", bot_id, ip, result, start)
            or evidence.get("window_start") != iso(start)
            or evidence.get("window_end") != iso(start + timedelta(minutes=5))
            or evidence.get("window_seconds") != 300
            or evidence["first_at"] != iso(first)
            or evidence["last_at"] != iso(last)
            or not (start <= first <= last < start + timedelta(minutes=5))
            or window.first_at != min(first, now)
            or window.last_at != max(last, now)
            or window.boundary != encoded
            or evidence.get("aggregation_version") != 1
            or any(evidence.get(key) != value for key, value in boundary.items())
            or evidence.get("request_count") != window.request_count - 1
            or evidence.get("returned_count") != window.returned_count - count
        ):
            raise RuntimeError("Disclosure audit summary is inconsistent")
    if log is None:
        log = AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role=user.role,
            action=action,
            target_type="bot",
            target_id=bot_id,
            target_name=f"#{bot_id}",
            ip_address=ip,
            result=result,
            created_at=start,
        )
        db.add(log)
        db.flush()
        window.audit_id = log.id

    log.detail = json.dumps(
        {
            **boundary,
            "aggregation_version": 1,
            "window_seconds": 300,
            "window_start": iso(start),
            "window_end": iso(start + timedelta(minutes=5)),
            "first_at": iso(window.first_at),
            "last_at": iso(window.last_at),
            "request_count": window.request_count,
            "returned_count": window.returned_count,
        }
    )
    db.flush()
