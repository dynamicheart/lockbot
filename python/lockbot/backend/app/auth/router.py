"""
Auth routes: register / login / current user
"""

import secrets
import string

import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

import lockbot.backend.app.config as _config
from lockbot.backend.app.auth.dependencies import create_access_token, get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.auth.schemas import (
    ChangeEmail,
    ChangePassword,
    ForceChangePassword,
    TokenOut,
    UserLogin,
    UserOut,
    UserRegister,
)
from lockbot.backend.app.database import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return _bcrypt.checkpw(password.encode(), hashed.encode())


def _generate_password(length: int = 12) -> str:
    """Generate a random password with mixed character types."""
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*"),
    ]
    password += [secrets.choice(alphabet) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(password)
    return "".join(password)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: UserRegister, db: Session = Depends(get_db)):
    if not _config.ALLOW_REGISTER:
        raise HTTPException(status_code=403, detail="Registration is disabled. Contact admin.")

    exists = db.query(User).filter(or_(User.username == body.username, User.email == body.email)).first()
    if exists:
        raise HTTPException(status_code=409, detail="Username or email already taken")

    user = User(
        username=body.username,
        email=body.email,
        password_hash=_hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
def login(body: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenOut(
        access_token=create_access_token(user.id, user.effective_token_version, user.must_change_password),
        must_change_password=user.must_change_password,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.put("/change-password", response_model=TokenOut)
def change_password(
    body: ChangePassword,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user.password_hash = _hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    return TokenOut(
        access_token=create_access_token(user.id, user.effective_token_version, False),
        must_change_password=False,
    )


@router.put("/force-change-password", response_model=TokenOut)
def force_change_password(
    body: ForceChangePassword,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.must_change_password:
        raise HTTPException(status_code=400, detail="No password change required")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user.password_hash = _hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    return TokenOut(
        access_token=create_access_token(user.id, user.effective_token_version, False),
        must_change_password=False,
    )


@router.put("/change-email", response_model=UserOut)
def change_email(
    body: ChangeEmail,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    exists = db.query(User).filter(User.email == body.new_email).filter(User.id != user.id).first()
    if exists:
        raise HTTPException(status_code=409, detail="Email already taken")
    user.email = body.new_email
    db.commit()
    db.refresh(user)
    return user
