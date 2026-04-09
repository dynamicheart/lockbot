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


def create_access_token(user_id: int, must_change_password: bool = False) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "mcp": must_change_password,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
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
