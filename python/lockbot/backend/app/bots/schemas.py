"""
Bot Pydantic schemas
"""

from datetime import datetime

from pydantic import BaseModel


class BotCreate(BaseModel):
    name: str
    bot_type: str  # NODE / DEVICE / QUEUE
    platform: str = "Infoflow"
    group_id: str | None = None
    webhook_url: str
    aes_key: str = ""
    token: str = ""
    cluster_configs: dict | list
    config_overrides: dict | None = None


class BotUpdate(BaseModel):
    name: str | None = None
    group_id: str | None = None
    webhook_url: str | None = None
    aes_key: str | None = None
    token: str | None = None
    cluster_configs: dict | list | None = None
    config_overrides: dict | None = None


class BotOut(BaseModel):
    id: int
    user_id: int
    name: str
    bot_type: str
    platform: str
    group_id: str | None
    last_user_id: str | None
    status: str
    last_request_at: datetime | None
    cluster_configs: str  # JSON string
    config_overrides: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BotDetail(BotOut):
    """Detail view with sensitive fields (both masked and raw for show/copy)."""

    owner: str = ""
    owner_role: str = ""
    webhook_url_raw: str = ""
    aes_key_raw: str = ""
    token_raw: str = ""
    webhook_url_masked: str = ""
    aes_key_masked: str = ""
    token_masked: str = ""


class BotStatusOut(BaseModel):
    """Response for lifecycle operations (start/stop/restart)."""

    id: int
    status: str
    pid: int | None = None
    consecutive_failures: int = 0
    message: str = ""


class BotLogOut(BaseModel):
    """Response for log entries."""

    id: int
    bot_id: int
    category: str = "system"
    level: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}
