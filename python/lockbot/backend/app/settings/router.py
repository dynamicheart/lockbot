"""
Settings API — public read + admin write.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.database import get_db
from lockbot.backend.app.settings.models import SiteSetting

router = APIRouter(tags=["settings"])

# --- Public keys (exposed without auth) ---
PUBLIC_KEYS = {"platform_url", "admin_contact"}

# --- Env-var fallbacks ---
import os

_DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")

_ENV_FALLBACKS = {
    "platform_url": os.environ.get("PLATFORM_URL", ""),
    "admin_contact": os.environ.get("ADMIN_CONTACT", ""),
    "github_url": os.environ.get("GITHUB_URL", "https://github.com/dynamicheart/lockbot"),
    "news_content": "",
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
        row = db.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key, value=value, updated_at=datetime.utcnow())
            db.add(row)
        else:
            row.value = value
            row.updated_at = datetime.utcnow()
        updated.append(key)
    db.commit()
    return {"updated": updated}
