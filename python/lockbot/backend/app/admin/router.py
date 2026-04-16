"""
Admin API routes.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import (
    can_assign_role,
    can_create_user_with_role,
    can_manage_user,
    require_admin,
    require_super_admin,
)
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.auth.router import _generate_password, _hash_password
from lockbot.backend.app.auth.schemas import (
    AdminCreateUser,
    AdminEditUser,
    PasswordResetOut,
    UserOut,
    UserOutWithStats,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/users", response_model=PasswordResetOut, status_code=201)
def admin_create_user(
    request: Request,
    body: AdminCreateUser,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin creates a user. Super_admin can create admin/user; admin can only create user."""
    # Use unified permission check
    allowed, status_code, error_msg = can_create_user_with_role(operator, body.role)
    if not allowed:
        raise HTTPException(status_code=status_code, detail=error_msg)

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
    db.flush()
    write_audit_log(
        db,
        operator,
        "user.create",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        detail={"role": body.role},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(user)
    return PasswordResetOut(id=user.id, username=user.username, new_password=raw_password)


@router.get("/users", response_model=list[UserOutWithStats])
def list_users(
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List users visible to the operator.
    Super_admin sees all; admin sees only users (not admins/super_admins).
    Includes bot_count and running_count for each user."""
    if operator.role == "super_admin":
        users = db.query(User).all()
    else:
        # admin: see regular users + self
        users = db.query(User).filter((User.role == "user") | (User.id == operator.id)).all()

    # Attach bot stats for each user
    result = []
    for u in users:
        data = UserOut.model_validate(u)
        bot_count = db.query(Bot).filter(Bot.user_id == u.id, Bot.is_deleted.is_(False)).count()
        running_count = (
            db.query(Bot)
            .filter(
                Bot.user_id == u.id,
                Bot.status == "running",
                Bot.is_deleted.is_(False),
            )
            .count()
        )
        result.append({**data.model_dump(), "bot_count": bot_count, "running_count": running_count})
    return result


@router.put("/users/{user_id}", response_model=UserOut)
def admin_edit_user(
    user_id: int,
    body: AdminEditUser,
    request: Request,
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

    changes: dict = {}
    if body.username is not None and body.username != target.username:
        dup = db.query(User).filter(User.username == body.username).first()
        if dup:
            raise HTTPException(status_code=409, detail="Username already taken")
        changes["username"] = {"old": target.username, "new": body.username}
        target.username = body.username

    if body.email is not None and body.email != target.email:
        dup = db.query(User).filter(User.email == body.email).first()
        if dup:
            raise HTTPException(status_code=409, detail="Email already taken")
        changes["email"] = {"old": target.email, "new": body.email}
        target.email = body.email

    if body.role is not None:
        # Use unified permission check
        allowed, status_code, error_msg = can_assign_role(operator, target, body.role)
        if not allowed:
            raise HTTPException(status_code=status_code, detail=error_msg)
        # Force logout when role changes
        if target.role != body.role:
            changes["role"] = {"old": target.role, "new": body.role}
            target.token_version = target.effective_token_version + 1
        target.role = body.role

    if body.max_running_bots is not None:
        changes["max_running_bots"] = {"old": target.max_running_bots, "new": body.max_running_bots}
        target.max_running_bots = body.max_running_bots

    write_audit_log(
        db,
        operator,
        "user.edit",
        target_type="user",
        target_id=target.id,
        target_name=target.username,
        detail=changes,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(target)
    return target


@router.put("/users/{user_id}/max-bots")
def set_max_bots(
    user_id: int,
    body: dict,
    request: Request,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(operator, user.role):
        raise HTTPException(status_code=403, detail="Cannot manage this user")
    old_val = user.max_running_bots
    user.max_running_bots = body["max_running_bots"]
    write_audit_log(
        db,
        operator,
        "user.set_max_bots",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        detail={"old": old_val, "new": user.max_running_bots},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(user)
    return {"id": user.id, "max_running_bots": user.max_running_bots}


@router.post("/users/{user_id}/reset-password", response_model=PasswordResetOut)
def admin_reset_password(
    user_id: int,
    request: Request,
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
    # Force logout - invalidate all existing tokens
    target.token_version = target.effective_token_version + 1
    write_audit_log(
        db,
        operator,
        "user.reset_password",
        target_type="user",
        target_id=target.id,
        target_name=target.username,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(target)
    return PasswordResetOut(id=target.id, username=target.username, new_password=raw_password)


@router.post("/users/{user_id}/force-logout")
def force_logout_user(
    user_id: int,
    request: Request,
    operator: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Force a user to logout by invalidating all their tokens."""
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(operator, target.role):
        raise HTTPException(status_code=403, detail="Cannot manage this user")
    target.token_version = target.effective_token_version + 1
    write_audit_log(
        db,
        operator,
        "user.force_logout",
        target_type="user",
        target_id=target.id,
        target_name=target.username,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return {"id": target.id, "username": target.username, "message": "User logged out successfully"}


@router.get("/bots")
def list_all_bots(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(Bot, User.username).join(User, Bot.user_id == User.id).filter(Bot.is_deleted.is_(False)).all()
    return [
        {c.name: getattr(bot, c.name) for c in bot.__table__.columns} | {"owner": username} for bot, username in rows
    ]


@router.get("/stats")
def platform_stats(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total_users = db.query(User).count()
    bots = db.query(Bot).filter(Bot.is_deleted.is_(False)).all()
    return {
        "totalUsers": total_users,
        "totalBots": len(bots),
        "running": sum(1 for b in bots if b.status == "running"),
        "errors": sum(1 for b in bots if b.status == "error"),
    }


@router.get("/backup")
def download_backup(
    request: Request,
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
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

    write_audit_log(db, _admin, "admin.backup", ip=request.client.host if request.client else None)
    db.commit()

    filename = f"lockbot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/x-sqlite3",
        background=BackgroundTask(os.unlink, tmp_path),
    )


_DEFAULT_DATA_DIR = os.environ.get("DATA_DIR", "/data")


@router.get("/bot-states")
def download_all_bot_states(
    request: Request,
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Download all bot state files as a zip archive (super_admin only)."""
    bots = db.query(Bot).filter(Bot.is_deleted.is_(False)).all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for bot in bots:
            # Determine data dir from config_overrides or default
            data_dir = _DEFAULT_DATA_DIR
            try:
                overrides = json.loads(bot.config_overrides or "{}")
                data_dir = overrides.get("DATA_DIR") or _DEFAULT_DATA_DIR
            except (json.JSONDecodeError, AttributeError):
                pass
            state_file = os.path.join(data_dir, "bots", str(bot.id), "bot_state.json")
            folder_name = f"bot_{bot.id}_{bot.name}"
            if os.path.exists(state_file):
                zf.write(state_file, f"{folder_name}/bot_state.json")
            else:
                # Create an empty placeholder so the admin knows the bot has no state
                zf.writestr(f"{folder_name}/bot_state.json", "{}")

    buf.seek(0)
    content = buf.read()

    write_audit_log(db, _admin, "admin.bot_states", ip=request.client.host if request.client else None)
    db.commit()

    filename = f"lockbot_states_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
