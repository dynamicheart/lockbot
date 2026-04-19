"""
Settings API — public read + admin write.
"""

import json
import os
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
PUBLIC_KEYS = {"platform_url", "admin_contact"}

# --- Env-var fallbacks ---
_DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")

_ENV_FALLBACKS = {
    "platform_url": os.environ.get("PLATFORM_URL", ""),
    "admin_contact": os.environ.get("ADMIN_CONTACT", ""),
    "github_url": os.environ.get("GITHUB_URL", "https://github.com/dynamicheart/lockbot"),
    "news_content": "",
    "enabled_platforms": '["Infoflow"]',  # JSON array; default: only Infoflow
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


@router.get("/api/public/stats", response_model=PlatformStats)
def get_public_stats(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return platform statistics (requires login)."""
    total_users = db.query(sa_func.count(User.id)).scalar()
    total_bots = db.query(sa_func.count(Bot.id)).filter(Bot.is_deleted.is_(False)).scalar()
    running_bots = db.query(sa_func.count(Bot.id)).filter(Bot.is_deleted.is_(False), Bot.status == "running").scalar()
    return PlatformStats(
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
    updated = []
    for key, value in body.settings.items():
        if key not in allowed:
            raise HTTPException(status_code=422, detail=f"Unknown setting key: {key}")
        if key == "enabled_platforms" and value is not None:
            try:
                platforms = json.loads(value)
                if not isinstance(platforms, list) or len(platforms) == 0:
                    raise HTTPException(status_code=422, detail="At least one platform must be enabled")
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=422, detail="Invalid JSON for enabled_platforms") from e
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
    try:
        from lockbot.core.base_bot import BaseBot

        BaseBot._invalidate_site_cache()
    except Exception:
        pass

    return {"updated": updated}


# ── Platforms endpoint ────────────────────────────────────


class PlatformsOut(BaseModel):
    platforms: list[str]


@router.get("/api/platforms", response_model=PlatformsOut)
def get_platforms(
    all: bool = False,
    db: Session = Depends(get_db),
):
    """Return available IM platforms.

    - all=False (default, no auth): enabled platforms ∩ PLATFORM_REGISTRY (for BotForm)
    - all=True (super_admin only): full PLATFORM_REGISTRY keys (for admin settings checkbox)
    """
    from lockbot.core.platforms import PLATFORM_REGISTRY

    if all:
        return PlatformsOut(platforms=sorted(PLATFORM_REGISTRY.keys()))

    raw = _get_setting(db, "enabled_platforms") or '["Infoflow"]'
    try:
        enabled: list[str] = json.loads(raw)
    except Exception:
        enabled = ["Infoflow"]

    return PlatformsOut(platforms=[p for p in enabled if p in PLATFORM_REGISTRY])
