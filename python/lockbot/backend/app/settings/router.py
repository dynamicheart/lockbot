"""
Settings API — public read + admin write.
"""

import os
import re
import unicodedata
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.dependencies import get_current_user, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.settings.models import SiteSetting

router = APIRouter(tags=["settings"])

# --- Public keys (exposed without auth) ---
PUBLIC_KEYS = {"platform_url", "admin_contact", "platform_infoflow_enabled", "platform_feishu_enabled"}

# --- Env-var fallbacks ---
_DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")

_ENV_FALLBACKS = {
    "platform_url": os.environ.get("PLATFORM_URL", ""),
    "admin_contact": os.environ.get("ADMIN_CONTACT", ""),
    "github_url": os.environ.get("GITHUB_URL", "https://github.com/dynamicheart/lockbot"),
    "news_content": "",
    "agent_promotion_enabled": "false",
    "platform_infoflow_enabled": "true",
    "platform_feishu_enabled": "true",
    "agent_promotion_text_zh": "",
    "agent_promotion_text_en": "",
    "feishu_proxy_mode": "deployment",
    "feishu_proxy_url": "",
}


class SettingOut(BaseModel):
    key: str
    value: str | None


class SettingBatch(BaseModel):
    settings: dict[str, str | None]


def _get_setting(db: Session, key: str) -> str | None:
    row = db.get(SiteSetting, key)
    if row is not None:
        return row.value
    return _ENV_FALLBACKS.get(key)


def get_all_settings(db: Session) -> dict[str, str | None]:
    """Return all settings as a dict, with env-var fallbacks."""
    rows = db.query(SiteSetting).all()
    result = dict(_ENV_FALLBACKS)
    for row in rows:
        result[row.key] = row.value
    return result


# ── Public endpoint (no auth) ────────────────────────────


@router.get("/api/settings", response_model=list[SettingOut])
def public_settings(db: Session = Depends(get_db)):
    """Return public-facing settings (no auth required)."""
    return [SettingOut(key=k, value=_get_setting(db, k)) for k in sorted(PUBLIC_KEYS)]


class PlatformStats(BaseModel):
    """Public platform statistics."""

    total_users: int
    total_bots: int
    running_bots: int
    agent_authorizations: int = 0
    agent_usage_registrations: int = 0


@router.get("/api/public/stats", response_model=PlatformStats)
def get_public_stats(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return platform statistics (requires login)."""
    total_users = db.query(sa_func.count(User.id)).scalar()
    total_bots = db.query(sa_func.count(Bot.id)).filter(Bot.is_deleted.is_(False)).scalar()
    running_bots = db.query(sa_func.count(Bot.id)).filter(Bot.is_deleted.is_(False), Bot.status == "running").scalar()
    from lockbot.backend.app.resource_api.activity import agent_summary

    summary = agent_summary(db, db.query(Bot).filter(Bot.is_deleted.is_(False)).all())
    return PlatformStats(
        agent_authorizations=summary["authorizations"],
        agent_usage_registrations=summary["usage_registrations"],
        total_users=total_users,
        total_bots=total_bots,
        running_bots=running_bots,
    )


# ── Admin endpoints (super_admin only) ──────────────────


@router.get("/api/admin/settings", response_model=list[SettingOut])
def list_all_settings(
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """List all settings including non-public ones."""
    all_keys = sorted(set(PUBLIC_KEYS) | set(_ENV_FALLBACKS.keys()))
    return [SettingOut(key=k, value=_get_setting(db, k)) for k in all_keys]


@router.put("/api/admin/settings")
def batch_update_settings(
    body: SettingBatch,
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Bulk-update settings. Only known keys are accepted."""
    allowed = set(_ENV_FALLBACKS.keys())
    for key, value in body.settings.items():
        if key not in allowed:
            raise HTTPException(status_code=422, detail=f"Unknown setting key: {key}")
        if key in {"agent_promotion_enabled", "platform_infoflow_enabled", "platform_feishu_enabled"} and value not in (
            "true",
            "false",
        ):
            raise HTTPException(status_code=422, detail=f"{key} must be true or false")
        if (
            key in {"agent_promotion_text_zh", "agent_promotion_text_en"}
            and value
            and (
                len(value) > 120
                or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in value)
                or any(char in value for char in "<>[]{}*_`#")
                or re.search(r"(?:https?://|www\.)", value, re.IGNORECASE)
            )
        ):
            raise HTTPException(status_code=422, detail=f"{key} must be plain single-line text (max 120 characters)")
    if {"feishu_proxy_mode", "feishu_proxy_url"} & body.settings.keys():
        from lockbot.core.platforms.feishu_proxy import validate_proxy_url

        mode = body.settings.get("feishu_proxy_mode", _get_setting(db, "feishu_proxy_mode"))
        value = body.settings.get("feishu_proxy_url", _get_setting(db, "feishu_proxy_url")) or ""
        try:
            if mode not in {"deployment", "direct", "custom"}:
                raise ValueError("Invalid Feishu proxy mode")
            if mode == "custom" and not value:
                raise ValueError("A Feishu proxy URL is required")
            validate_proxy_url(value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    updated = []
    for key, value in body.settings.items():
        if key not in allowed:
            raise HTTPException(status_code=422, detail=f"Unknown setting key: {key}")
        row = db.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key, value=value, updated_at=datetime.utcnow())
            db.add(row)
        else:
            row.value = value
            row.updated_at = datetime.utcnow()
        updated.append(key)
    db.commit()

    # Invalidate bot site-settings cache so bots pick up changes immediately
    from lockbot.core.base_bot import BaseLockBot

    BaseLockBot._invalidate_site_cache()

    proxy_status = None
    if {"feishu_proxy_mode", "feishu_proxy_url"} & body.settings.keys():
        from lockbot.backend.app.feishu.proxy_settings import apply_proxy

        try:
            proxy_status = apply_proxy(db)
        except Exception:
            # Configuration is committed; do not replay mutations or pretend a handshake succeeded.
            proxy_status = "restart_required"
    return {"updated": updated, "feishu_proxy_status": proxy_status}


@router.get("/api/admin/feishu/proxy")
def feishu_proxy_status(_admin: User = Depends(require_super_admin)):
    from lockbot.core.platforms.feishu_proxy import proxy_state

    return proxy_state()


@router.post("/api/admin/feishu/proxy/test")
def test_feishu_proxy(_admin: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    from lockbot.backend.app.feishu.models import FeishuApplication
    from lockbot.backend.app.feishu.service import application_client

    app = db.query(FeishuApplication).order_by(FeishuApplication.id).first()
    if app is None:
        return {"http": "no_application", "websocket": "not_tested"}
    client = application_client(app)
    db.rollback()
    try:
        client.bot_identity()  # Fixed official API only, never an administrator-supplied target.
        status = "ok"
    except Exception:
        status = "unavailable"
    return {"http": status, "websocket": "not_tested"}
