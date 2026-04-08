"""
Admin API routes.
"""

import contextlib
import os
import shutil
import tempfile
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from lockbot.backend.app.auth.dependencies import can_manage_user, require_admin, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.auth.router import _generate_password, _hash_password
from lockbot.backend.app.auth.schemas import (
    AdminCreateUser,
    AdminEditUser,
    PasswordResetOut,
    UserOut,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])

ADMIN_VISIBLE_ROLES = ("admin", "user")
SUPER_ADMIN_VISIBLE_ROLES = ("super_admin", "admin", "user")
ADMIN_CREATABLE_ROLES = ("user",)


@router.post("/users", response_model=PasswordResetOut, status_code=201)
def admin_create_user(
    body: AdminCreateUser,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin creates a user. Super_admin can create admin/user; admin can only create user."""
    # Non-super_admin can only create regular users
    if operator.role != "super_admin" and body.role not in ADMIN_CREATABLE_ROLES:
        raise HTTPException(status_code=403, detail="Cannot create user with this role")

    if body.role not in SUPER_ADMIN_VISIBLE_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role, must be one of {SUPER_ADMIN_VISIBLE_ROLES}")

    exists = db.query(User).filter(User.username == body.username).first()
    if exists:
        raise HTTPException(status_code=409, detail="Username already taken")
    dup_email = db.query(User).filter(User.email == body.email).first()
    if dup_email:
        raise HTTPException(status_code=409, detail="Email already taken")

    raw_password = _generate_password()
    user = User(
        username=body.username,
        email=body.email,
        password_hash=_hash_password(raw_password),
        role=body.role,
        max_running_bots=body.max_running_bots,
        must_change_password=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return PasswordResetOut(id=user.id, username=user.username, new_password=raw_password)


@router.get("/users", response_model=list[UserOut])
def list_users(
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List users visible to the operator.
    Super_admin sees all; admin sees only users (not admins/super_admins)."""
    if operator.role == "super_admin":
        return db.query(User).all()

    # admin: see regular users + self
    return db.query(User).filter((User.role == "user") | (User.id == operator.id)).all()


@router.put("/users/{user_id}", response_model=UserOut)
def admin_edit_user(
    user_id: int,
    body: AdminEditUser,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin edits user profile."""
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Cannot edit users at or above your own level
    if not can_manage_user(operator, target.role):
        raise HTTPException(status_code=403, detail="Cannot manage this user")

    if body.username is not None and body.username != target.username:
        dup = db.query(User).filter(User.username == body.username).first()
        if dup:
            raise HTTPException(status_code=409, detail="Username already taken")
        target.username = body.username

    if body.email is not None and body.email != target.email:
        dup = db.query(User).filter(User.email == body.email).first()
        if dup:
            raise HTTPException(status_code=409, detail="Email already taken")
        target.email = body.email

    if body.role is not None:
        if body.role not in SUPER_ADMIN_VISIBLE_ROLES:
            raise HTTPException(status_code=400, detail="Invalid role")
        # Only super_admin can promote to admin
        if body.role in ("admin", "super_admin") and operator.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only super admin can set this role")
        target.role = body.role

    if body.max_running_bots is not None:
        target.max_running_bots = body.max_running_bots

    db.commit()
    db.refresh(target)
    return target


@router.put("/users/{user_id}/max-bots")
def set_max_bots(
    user_id: int,
    body: dict,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(operator, user.role):
        raise HTTPException(status_code=403, detail="Cannot manage this user")
    user.max_running_bots = body["max_running_bots"]
    db.commit()
    db.refresh(user)
    return {"id": user.id, "max_running_bots": user.max_running_bots}


@router.post("/users/{user_id}/reset-password", response_model=PasswordResetOut)
def admin_reset_password(
    user_id: int,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin resets a user's password. Returns the new plaintext password."""
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(operator, target.role):
        raise HTTPException(status_code=403, detail="Cannot manage this user")
    raw_password = _generate_password()
    target.password_hash = _hash_password(raw_password)
    target.must_change_password = True
    db.commit()
    db.refresh(target)
    return PasswordResetOut(id=target.id, username=target.username, new_password=raw_password)


@router.get("/bots")
def list_all_bots(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(Bot, User.username).join(User, Bot.user_id == User.id).all()
    return [
        {c.name: getattr(bot, c.name) for c in bot.__table__.columns} | {"owner": username} for bot, username in rows
    ]


@router.get("/stats")
def platform_stats(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total_users = db.query(User).count()
    bots = db.query(Bot).all()
    return {
        "totalUsers": total_users,
        "totalBots": len(bots),
        "running": sum(1 for b in bots if b.status == "running"),
        "errors": sum(1 for b in bots if b.status == "error"),
    }


@router.get("/backup")
def download_backup(
    _admin: User = Depends(require_super_admin),
):
    """Download full SQLite database backup (super_admin only)."""
    from lockbot.backend.app.config import DATABASE_URL

    db_url = DATABASE_URL
    if not db_url.startswith("sqlite:///"):
        raise HTTPException(status_code=400, detail="Backup only supported for SQLite")

    db_path = db_url[len("sqlite:///") :]
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Database file not found")

    # Copy to temp file to avoid sending a mid-write database
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(db_path, tmp_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    filename = f"lockbot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/x-sqlite3",
        background=BackgroundTask(os.unlink, tmp_path),
    )
