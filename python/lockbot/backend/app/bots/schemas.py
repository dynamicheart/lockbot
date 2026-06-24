"""
Bot Pydantic schemas
"""

import json
from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

# ── config_overrides value bounds ─────────────────────────────────────────────
_CFG_RULES: dict[str, tuple[int, int] | None] = {
    # (min, max); None means special-cased below
    "DEFAULT_DURATION": (60, 604800),  # 1 min – 7 days
    "TIME_ALERT": (30, 3600),  # 30 s – 1 h
    "MAX_LOCK_DURATION": None,  # -1 (unlimited) or 300–604800
}


def _serialize_device_configs(cluster_configs_json: str) -> str:
    """Convert DEVICE cluster_configs from stored dict JSON to ordered array JSON.

    DB stores: '{"2": ["a800"], "0": ["h20"]}'
    API returns: '[{"node_key": "2", "devices": ["a800"]}, {"node_key": "0", "devices": ["h20"]}]'

    This ensures JS frontend receives an array that preserves insertion order,
    avoiding the JS engine's automatic sorting of numeric object keys.
    """
    try:
        cc = json.loads(cluster_configs_json)
        if isinstance(cc, dict):
            return json.dumps([{"node_key": k, "devices": v} for k, v in cc.items()], ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    return cluster_configs_json


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

    whitelist = v.get("DURATION_WHITELIST")
    if whitelist is not None and (not isinstance(whitelist, list) or not all(isinstance(u, str) for u in whitelist)):
        errors.append("DURATION_WHITELIST must be a list of strings")

    node_aliases = v.get("NODE_ALIASES")
    if node_aliases is not None and (
        not isinstance(node_aliases, dict)
        or not all(isinstance(k, str) and isinstance(a, str) and len(a) <= 15 for k, a in node_aliases.items())
    ):
        errors.append("NODE_ALIASES must be a dict of {string: string} with alias length <= 15")

    show_alias = v.get("SHOW_NODE_ALIAS")
    if show_alias is not None and not isinstance(show_alias, bool):
        errors.append("SHOW_NODE_ALIAS must be a boolean")

    max_dur = v.get("MAX_LOCK_DURATION")
    default_dur = v.get("DEFAULT_DURATION")
    if isinstance(max_dur, int) and isinstance(default_dur, int) and max_dur != -1 and default_dur > max_dur:
        errors.append("DEFAULT_DURATION must not exceed MAX_LOCK_DURATION")

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

    @model_validator(mode="after")
    def convert_device_cluster_configs(self):
        """For DEVICE bots, convert stored dict JSON to array JSON for frontend."""
        if self.bot_type == "DEVICE" and self.cluster_configs:
            self.cluster_configs = _serialize_device_configs(self.cluster_configs)
        return self


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
    has_api_key: bool = False
    api_key: str = ""


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


# ── Public API schemas ────────────────────────────────────────────────────────


class OpkickRequest(BaseModel):
    """Request body for POST /api/bots/{id}/opkick"""

    target_user: str
    node_key: str | None = None
    dev: list[int] | None = None
    reason: str = ""


class OpkickResponse(BaseModel):
    ok: bool = True
    freed: list[str]


class LockedUsersResponse(BaseModel):
    bot_id: int
    bot_type: str
    nodes: dict
