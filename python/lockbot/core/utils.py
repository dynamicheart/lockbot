"""Shared utility functions (duration formatting, user info, etc.)."""

import copy
import time

from lockbot.core.config import Config
from lockbot.core.i18n import t


def create_user_info(user_id, duration, start_time=None, config=None):
    """Create a user info record."""
    if config is not None:
        user_info = copy.deepcopy(config.get_val("DEFAULT_USER_INFO"))
    else:
        user_info = copy.deepcopy(Config.get("DEFAULT_USER_INFO"))
    user_info["user_id"] = user_id
    user_info["start_time"] = int(time.time()) if start_time is None else start_time
    user_info["duration"] = duration
    return user_info


def is_first_user(user_info_list, user_id):
    """Check if user_id matches the first user in the list."""
    if not user_info_list:
        return False
    return user_info_list[0]["user_id"] == user_id


def find_user_info(user_info_list, user_id):
    """Find user info by user_id, or return None."""
    for user_info in user_info_list:
        if user_info["user_id"] == user_id:
            return user_info
    return None


def remove_user_info(user_info_list, user_id):
    """Remove a user from the list by user_id."""
    for i, user_info in enumerate(user_info_list):
        if user_info["user_id"] == user_id:
            user_info_list.pop(i)
            break


def duration_to_seconds(duration, unit):
    """Convert duration to seconds based on unit (d/h/m)."""
    assert unit in ["d", "h", "m"]
    if unit == "d":
        return duration * 86400
    elif unit == "h":
        return duration * 3600
    else:
        return duration * 60


def format_duration(duration, config=None):
    """Format duration in seconds to a human-readable string."""
    if duration >= 86400:
        return t("duration.days", config=config, value=f"{duration / 86400:.1f}")
    elif duration >= 3600:
        return t("duration.hours", config=config, value=f"{duration / 3600:.1f}")
    else:
        return t("duration.minutes", config=config, value=f"{duration / 60:.0f}")


def remaining_duration(start_time, duration):
    """Calculate remaining duration in seconds."""
    current_timestamp = int(time.time())
    assert current_timestamp >= start_time
    elapsed_duration = current_timestamp - start_time
    return max(duration - elapsed_duration, 0)


def apply_max_duration_limit(user_list, max_duration):
    """Clamp each user's remaining duration to max_duration in place."""
    for user_info in user_list:
        final_duration = remaining_duration(user_info["start_time"], user_info["duration"])
        if max_duration > 0 and final_duration > max_duration:
            user_info["duration"] = user_info["duration"] - final_duration + max_duration


def format_access_mode(status: str, config=None) -> str:
    """Return a display string indicating shared or exclusive access mode."""
    if status == "shared":
        return t("access_mode.shared", config=config)
    return t("access_mode.exclusive", config=config)
