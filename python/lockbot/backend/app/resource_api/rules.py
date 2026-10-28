"""Current persisted bot rules for discovery; never construct or admit a running bot."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from lockbot.backend.app.resource_api import commands
from lockbot.backend.app.resource_api.snapshot import QUEUE_CLAIM_WINDOW_SECONDS
from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate
from lockbot.core.bot_instance import _BOT_CLASS_MAP
from lockbot.core.config import Config


class PollMinimumSeconds(BaseModel):
    active: float = Field(description="Minimum resource poll interval for a holder or queue head, in seconds.")
    booking: float = Field(description="Minimum resource poll interval for a waiting booker, in seconds.")
    discovery: float = Field(
        description="Minimum resource poll interval for a caller without a holding/booking, in seconds."
    )


class RequestBudget(BaseModel):
    read_per_minute: int
    write_per_minute: int


class BotRules(BaseModel):
    forbid_relock: bool = Field(
        description="Defaults to false for every bot type; only explicit configuration disables renewal."
    )
    max_resources_per_user: int = Field(
        description="Distinct held plus booked resources per user and bot; -1 means unlimited."
    )
    resource_unit: Literal["node", "card"] = Field(description="node for NODE/QUEUE; card for DEVICE/QUEUE_DEVICE.")
    default_duration_seconds: int
    max_lock_duration_seconds: int = Field(
        description="Configured remaining-duration ceiling after acquisition/renewal; -1 means unlimited."
    )
    duration_limit_exempt: bool = Field(description="Whether this caller is exempt from the duration ceiling only.")
    effective_max_lock_duration_seconds: int = Field(
        description="Caller-specific duration ceiling, including exemption; -1 means unlimited."
    )
    queue_claim_window_seconds: int | None
    supported_operations: list[str] = Field(
        description="Personal API operations supported by the bot type; access and alias gates still apply."
    )
    allow_user_alias: bool
    request_budget: RequestBudget
    poll_minimum_seconds: PollMinimumSeconds
    api_max_machines_per_user: int = Field(
        description="Separate existing API cap, counting held/booked machines even for card bots."
    )


def current_bot_rules(bot, user_id: str) -> dict:
    """Use Config defaults and the current DB row, not a cached runtime configuration."""
    config = Config({**json.loads(bot.config_overrides or "{}"), "BOT_TYPE": bot.bot_type})
    maximum = config.get_val("MAX_LOCK_DURATION")
    maximum = maximum if maximum > 0 else -1
    exempt = user_id in (config.get_val("DURATION_WHITELIST") or [])
    # supported_commands is stateless for every bot class (also used by handler seam tests).
    supported = _BOT_CLASS_MAP[bot.bot_type].supported_commands(None)
    return {
        "forbid_relock": config.get_val("FORBID_RELOCK"),
        "max_resources_per_user": config.get_val("MAX_RESOURCES_PER_USER"),
        "resource_unit": "card" if bot.bot_type in {"DEVICE", "QUEUE_DEVICE"} else "node",
        "default_duration_seconds": config.get_val("DEFAULT_DURATION"),
        "max_lock_duration_seconds": maximum,
        "duration_limit_exempt": exempt,
        "effective_max_lock_duration_seconds": -1 if exempt else maximum,
        "queue_claim_window_seconds": QUEUE_CLAIM_WINDOW_SECONDS if bot.bot_type in {"QUEUE", "QUEUE_DEVICE"} else None,
        "supported_operations": sorted(set(supported) & commands.AGENT_ALLOWED_COMMANDS),
        "allow_user_alias": bool(config.get_val("ALLOW_USER_ALIAS")),
        "request_budget": {
            "read_per_minute": credential_budget.read_limit,
            "write_per_minute": credential_budget.write_limit,
        },
        "poll_minimum_seconds": {
            "active": resource_tier_gate.active,
            "booking": resource_tier_gate.booking,
            "discovery": resource_tier_gate.discovery,
        },
        "api_max_machines_per_user": commands.RESOURCE_API_MAX_HELD_PER_IDENTITY,
    }
