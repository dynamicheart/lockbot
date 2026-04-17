"""
Bot Pydantic schemas
"""

from datetime import datetime

from pydantic import BaseModel, field_validator

# ── config_overrides value bounds ─────────────────────────────────────────────
_CFG_RULES: dict[str, tuple[int, int] | None] = {
    # (min, max); None means special-cased below
    "DEFAULT_DURATION": (60, 604800),  # 1 min – 7 days
    "TIME_ALERT": (30, 3600),  # 30 s – 1 h
    "MAX_LOCK_DURATION": None,  # -1 (unlimited) or 300–604800
}


def _validate_config_overrides(v: dict | None) -> dict | None:
    if not v:
        return v
    errors: list[str] = []
    for key, bounds in _CFG_RULES.items():
        if key not in v:
            continue
        val = v[key]
        if not isinstance(val, int):
            errors.append(f"{key} must be an integer")
            continue
        if key == "MAX_LOCK_DURATION":
            if val != -1 and not (300 <= val <= 604800):
                errors.append("MAX_LOCK_DURATION must be -1 (unlimited) or between 300 and 604800")
        else:
            lo, hi = bounds  # type: ignore[misc]
            if not (lo <= val <= hi):
                errors.append(f"{key} must be between {lo} and {hi}")
    if errors:
        raise ValueError("; ".join(errors))
    return v


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

    @field_validator("config_overrides")
    @classmethod
    def validate_config_overrides(cls, v: dict | None) -> dict | None:
        return _validate_config_overrides(v)


class BotUpdate(BaseModel):
    name: str | None = None
    group_id: str | None = None
    webhook_url: str | None = None
    aes_key: str | None = None
    token: str | None = None
    cluster_configs: dict | list | None = None
    config_overrides: dict | None = None

    @field_validator("config_overrides")
    @classmethod
    def validate_config_overrides(cls, v: dict | None) -> dict | None:
        return _validate_config_overrides(v)


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
