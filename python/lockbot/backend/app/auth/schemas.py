"""
User Pydantic schemas
"""

from datetime import datetime

from pydantic import BaseModel


class UserRegister(BaseModel):
    username: str
    email: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    max_running_bots: int
    created_at: datetime
    must_change_password: bool = False

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class AdminCreateUser(BaseModel):
    username: str
    email: str
    role: str = "user"
    max_running_bots: int = 10


class AdminEditUser(BaseModel):
    username: str | None = None
    email: str | None = None
    role: str | None = None
    max_running_bots: int | None = None


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class ForceChangePassword(BaseModel):
    new_password: str
    confirm_password: str


class ChangeEmail(BaseModel):
    new_email: str


class PasswordResetOut(BaseModel):
    id: int
    username: str
    new_password: str
