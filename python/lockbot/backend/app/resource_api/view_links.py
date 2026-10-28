"""Bot-scoped read-only links, created lazily and reused for a fixed 24-hour lifetime."""

import base64
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import DateTime, ForeignKey, Integer, String, or_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from lockbot.backend.app import config
from lockbot.backend.app.database import Base

LIFETIME_SECONDS = 86400
_SHORT_VIEW_CODE = re.compile(r"[A-Za-z0-9_-]{22}\Z")
_VIEW_CODE = re.compile(r"lbv1\.[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{43}\Z")


class BotViewWindow(Base):
    __tablename__ = "bot_view_windows"
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), primary_key=True)
    bot_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def utcnow():
    return datetime.utcnow()


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat()


def _signature(db, window, *, compact=False):
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id

    domain = b"lockbot:readonly-view:v2" if compact else b"lockbot:readonly-view:v1"
    key = hmac.digest(config.JWT_SECRET.encode(), domain, "sha256")
    message = "|".join(
        [
            ensure_instance_id(db),
            str(window.bot_id),
            window.bot_uuid,
            str(window.identity_revision),
            window.nonce,
            _iso(window.issued_at),
            _iso(window.expires_at),
        ]
    ).encode()
    digest = hmac.digest(key, message, "sha256")
    return base64.urlsafe_b64encode(digest[:16] if compact else digest).decode().rstrip("=")


def _current(window, bot, now):
    return (
        window is not None
        and not bot.is_deleted
        and window.bot_uuid == bot.uuid
        and window.identity_revision == bot.identity_revision
        and window.expires_at > now
    )


def compact_view_path(db, window):
    return f"/r/{window.bot_id}/{_signature(db, window, compact=True)}"


def view_link(db: Session, bot, base_url: str) -> dict:
    """Authorized group, Agent or bot-detail reads may obtain a link; state polling never renews it."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return {}
    if bot.is_deleted or not bot.uuid:
        return {}
    now = utcnow().replace(microsecond=0)
    window = db.get(BotViewWindow, bot.id)
    if not _current(window, bot, now):
        values = {
            "bot_id": bot.id,
            "bot_uuid": bot.uuid,
            "identity_revision": bot.identity_revision,
            "nonce": secrets.token_urlsafe(18),
            "issued_at": now,
            "expires_at": now + timedelta(seconds=LIFETIME_SECONDS),
        }
        db.execute(
            insert(BotViewWindow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[BotViewWindow.bot_id],
                set_={key: value for key, value in values.items() if key != "bot_id"},
                where=or_(
                    BotViewWindow.expires_at <= now,
                    BotViewWindow.bot_uuid != bot.uuid,
                    BotViewWindow.identity_revision != bot.identity_revision,
                ),
            )
        )
        db.commit()
        db.expire_all()
        window = db.get(BotViewWindow, bot.id)
    base = base_url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return {"view_url": f"{base}/#{compact_view_path(db, window)}", "view_expires_at": _iso(window.expires_at)}


def verify_view(db, bot, token):
    """Read-only verification; an expired or wrong-bot token cannot renew itself."""
    if bot is None or bot.is_deleted or not token:
        return None
    compact = _SHORT_VIEW_CODE.fullmatch(token)
    if not compact and not _VIEW_CODE.fullmatch(token):
        return None
    window = db.get(BotViewWindow, bot.id)
    if not _current(window, bot, utcnow()):
        return None
    if compact:
        return window if secrets.compare_digest(_signature(db, window, compact=True), token) else None
    pieces = token.split(".")
    if len(pieces) != 3 or pieces[0] != "lbv1":
        return None
    if not secrets.compare_digest(window.nonce, pieces[1]):
        return None
    return window if secrets.compare_digest(_signature(db, window), pieces[2]) else None


def group_view_link(bot_id, expected_uuid):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.database import SessionLocal
    from lockbot.backend.app.settings.router import get_all_settings

    with SessionLocal() as db:
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted or bot.uuid != expected_uuid:
            return {}
        return view_link(db, bot, (get_all_settings(db).get("platform_url") or "").strip())
