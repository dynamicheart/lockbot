"""
JWT authentication dependencies.

NOTE: This is a local auth implementation (JWT + local user table).
To integrate with company SSO / OAuth, replace get_current_user()
with your SSO token validation logic. All routes use
Depends(get_current_user), so no other changes are needed.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET
from lockbot.backend.app.database import get_db

_security = HTTPBearer(auto_error=False)

_ROLE_LEVELS = {"super_admin": 0, "admin": 1, "user": 2}


def create_access_token(user_id: int, token_version: int = 0, must_change_password: bool = False) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "ver": token_version,
        "mcp": must_change_password,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
        token_version = payload.get("ver", 0)  # Default to 0 for old tokens without version
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Check token version - invalidate token if version mismatch
    if user.effective_token_version != token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired, please login again")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require admin or super_admin."""
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Require super_admin only."""
    if user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin required")
    return user


def can_manage_user(operator: User, target_role: str) -> bool:
    """Check if operator can manage a user with the given role."""
    op_level = _ROLE_LEVELS.get(operator.role, 3)
    tgt_level = _ROLE_LEVELS.get(target_role, 3)
    return op_level < tgt_level


def can_assign_role(operator: User, target: User, new_role: str) -> tuple[bool, int, str]:
    """
    Check if operator can change target's role to new_role.

    Returns (allowed, http_status_code, error_message).
    This consolidates all role assignment permission checks in one place.
    """
    # Validate role
    valid_roles = ("super_admin", "admin", "user")
    if new_role not in valid_roles:
        return False, 400, f"Invalid role, must be one of {valid_roles}"

    # Cannot manage users at or above your own level
    if not can_manage_user(operator, target.role):
        return False, 403, "Cannot manage this user"

    # Only super_admin can assign admin or super_admin role
    if new_role in ("admin", "super_admin") and operator.role != "super_admin":
        return False, 403, "Only super admin can assign this role"

    return True, 200, ""


def can_create_user_with_role(operator: User, role: str) -> tuple[bool, int, str]:
    """
    Check if operator can create a user with the given role.

    Returns (allowed, http_status_code, error_message).
    """
    # Validate role
    valid_roles = ("admin", "user")
    if role not in valid_roles:
        return False, 400, f"Invalid role, must be one of {valid_roles}"

    # Only super_admin can create admin
    if role == "admin" and operator.role != "super_admin":
        return False, 403, "Only super admin can create user with this role"

    return True, 200, ""
