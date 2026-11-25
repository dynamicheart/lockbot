"""Current persisted bot rules for discovery; never construct or admit a running bot."""

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from lockbot.backend.app.resource_api import commands
from lockbot.backend.app.resource_api.idle_recovery import (
    CONFIRMATION_SECONDS,
    EXTRA_SECONDS,
    IDLE_SECONDS,
    TRIGGER_BOT_FULL,
    TRIGGER_RESOURCE_QUEUE,
    idle_check_overnight_cutoff,
)
from lockbot.backend.app.resource_api.snapshot import QUEUE_CLAIM_WINDOW_SECONDS
from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate
from lockbot.core.bot_instance import _BOT_CLASS_MAP
from lockbot.core.config import Config
from lockbot.core.overnight import LeaseLimit


class OvernightRule(BaseModel):
    enabled: bool = Field(description="Whether this overnight rule is configured to apply.")
    timezone: str = Field(description="IANA time zone used to interpret the start and end times.")
    start: str = Field(description="Local start time in HH:MM format, inclusive.")
    end: str = Field(description="Local end time in HH:MM format, exclusive. May fall on the following day.")
    cutoff_unix_seconds: int | None = Field(
        description="End of the current active window as Unix seconds. Null when the window is inactive."
    )


class LowUtilizationRule(BaseModel):
    enabled: bool = Field(description="Whether the bot allows checks on registered exclusive resources.")
    sampling_required: bool = Field(description="Whether fresh, valid accelerator samples are required for a check.")
    trigger_any: list[Literal["resource_has_waiting_user", "bot_has_no_available_resources"]] = Field(
        description=(
            "At least one condition must hold. resource_has_waiting_user means another user is queued for the "
            "resource. bot_has_no_available_resources means the same bot has no idle resource with an empty queue."
        )
    )
    observation_seconds: int = Field(description="Minimum continuous low-utilization observation in seconds.")
    confirm_window_seconds: int = Field(
        description="Seconds from delivery of a notice in which its holder can confirm with keep."
    )
    keep_extension_seconds: int = Field(description="Seconds added to the notice review deadline by one keep.")
    agent_group_notifications: bool = Field(
        description="Whether low-utilization reminders for Agent-acquired locks are also delivered in group chat."
    )
    overnight_pause: OvernightRule = Field(description="Local time window during which idle recovery checks pause.")


def low_utilization_rule(config, now):
    cutoff = idle_check_overnight_cutoff(config, now)
    return {
        "enabled": config.get_val("IDLE_UTILIZATION_CHECK_ENABLED"),
        "sampling_required": True,
        "trigger_any": [TRIGGER_RESOURCE_QUEUE, TRIGGER_BOT_FULL],
        "observation_seconds": IDLE_SECONDS,
        "confirm_window_seconds": CONFIRMATION_SECONDS,
        "keep_extension_seconds": EXTRA_SECONDS,
        "agent_group_notifications": config.get_val("GROUP_REMIND_AGENT_IDLE"),
        "overnight_pause": {
            "enabled": config.get_val("OVERNIGHT_PAUSE_IDLE_CHECK"),
            "timezone": config.get_val("OVERNIGHT_TIMEZONE"),
            "start": config.get_val("OVERNIGHT_START"),
            "end": config.get_val("OVERNIGHT_END"),
            "cutoff_unix_seconds": cutoff if cutoff is not None and now < cutoff else None,
        },
    }


class IdleThresholds(BaseModel):
    accelerator_utilization_percent: list[float] = Field(
        description="Configured thresholds for recovery targets. The highest device sample must stay below threshold."
    )


class BindingLowUtilizationRule(LowUtilizationRule):
    status: Literal["known"] = Field(default="known", description="The bot policy is readable from this service.")
    thresholds: IdleThresholds | None = Field(
        description="Thresholds of configured recovery targets. Null when no recovery threshold is available."
    )
    action_on_timeout: Literal["release", "none"] | None = Field(
        description=(
            "release requires fresh low-utilization evidence and a trigger after the deadline. "
            "none means recovery is disabled. Null means recovery coverage is unknown."
        )
    )
    max_keeps: int = Field(description="Maximum extensions per notice. Repeated keep calls do not extend it again.")


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
    overnight: OvernightRule
    low_utilization: LowUtilizationRule
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
    now = int(time.time())
    limit = LeaseLimit.for_config(config, now, -1 if exempt else maximum)
    # supported_commands is stateless for every bot class (also used by handler seam tests).
    supported = _BOT_CLASS_MAP[bot.bot_type].supported_commands(None)
    return {
        "forbid_relock": config.get_val("FORBID_RELOCK"),
        "max_resources_per_user": config.get_val("MAX_RESOURCES_PER_USER"),
        "resource_unit": "card" if bot.bot_type in {"DEVICE", "QUEUE_DEVICE"} else "node",
        "overnight": {
            "enabled": config.get_val("OVERNIGHT_ENABLED"),
            "timezone": config.get_val("OVERNIGHT_TIMEZONE"),
            "start": config.get_val("OVERNIGHT_START"),
            "end": config.get_val("OVERNIGHT_END"),
            "cutoff_unix_seconds": limit.cutoff,
        },
        "low_utilization": low_utilization_rule(config, now),
        "default_duration_seconds": config.get_val("DEFAULT_DURATION"),
        "max_lock_duration_seconds": maximum,
        "duration_limit_exempt": exempt,
        "effective_max_lock_duration_seconds": limit.remaining_limit,
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


class BindingLockRules(BaseModel):
    unit: Literal["node", "card"] = Field(description="Allocation and quota unit for this bot.")
    default_duration_seconds: int = Field(description="Lease duration in seconds when a new request omits duration.")
    max_lock_duration_seconds: int = Field(
        description="Configured remaining lock duration ceiling in seconds. -1 means unlimited."
    )
    effective_max_lock_duration_seconds: int = Field(
        description="Caller ceiling in seconds after exemptions and the active overnight window. -1 is unlimited."
    )
    max_book_duration_seconds: int | None = Field(
        description="Current booking duration ceiling in seconds. -1 is unlimited. Null means booking is unsupported."
    )
    duration_limit_exempt: bool = Field(
        description="Whether the caller is exempt from the duration ceiling. Other allocation rules still apply."
    )
    max_resources_per_user: int = Field(
        description="Combined held and queued resource cap per user, counting distinct units. -1 means unlimited."
    )
    claim_window_seconds: int | None = Field(
        description="Seconds to claim a notified, ready booking. Null when the bot does not support booking."
    )
    renew_allowed: bool = Field(description="Whether this bot permits same-mode renewal of an existing eligible lock.")
    shared_lock_supported: bool = Field(description="Bot supports shared locking. Eligibility varies by resource.")
    overnight: OvernightRule = Field(description="Current local overnight window for allowing a longer lease.")


def binding_rules(bot, user_id: str, observer) -> dict:
    policy = current_bot_rules(bot, user_id)
    maximum = policy["effective_max_lock_duration_seconds"]
    claim_window = policy["queue_claim_window_seconds"]
    thresholds = sorted(
        {
            source.threshold_percent
            for source in getattr(observer, "sources", ())
            if any(
                target.bot_id == bot.id and str(target.bot_uuid) == str(bot.uuid) and target.recover_idle
                for target in source.targets
            )
        }
    )
    low = policy["low_utilization"]
    return {
        "lock_rules": {
            **{
                key: policy[key]
                for key in (
                    "default_duration_seconds",
                    "max_lock_duration_seconds",
                    "effective_max_lock_duration_seconds",
                    "duration_limit_exempt",
                    "max_resources_per_user",
                    "overnight",
                )
            },
            "unit": policy["resource_unit"],
            "max_book_duration_seconds": maximum if claim_window is not None else None,
            "claim_window_seconds": claim_window,
            "renew_allowed": not policy["forbid_relock"],
            "shared_lock_supported": "slock" in policy["supported_operations"],
        },
        "low_utilization": {
            **low,
            "status": "known",
            "thresholds": {"accelerator_utilization_percent": thresholds} if thresholds else None,
            "action_on_timeout": ("release" if thresholds else None) if low["enabled"] else "none",
            "max_keeps": 1,
        },
    }
