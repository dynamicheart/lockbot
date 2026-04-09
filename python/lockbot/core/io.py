r"""
lockbot I/O layer: bot state persistence and operation logging.
"""

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime

from lockbot.core.config import Config
from lockbot.core.utils import apply_max_duration_limit

logger = logging.getLogger(__name__)

# Node/device status constants
STATUS_IDLE = "idle"
STATUS_EXCLUSIVE = "exclusive"
STATUS_SHARED = "shared"


def _migrate_status_fields(node):
    """Convert old in_use/is_shared booleans to a single status field.

    Backwards-compatible: if the loaded state still uses the old two-boolean
    format, derive the status and drop the old keys.
    """
    if "status" not in node:
        in_use = node.pop("in_use", False)
        is_shared = node.pop("is_shared", False)
        if in_use:
            node["status"] = STATUS_SHARED if is_shared else STATUS_EXCLUSIVE
        else:
            node["status"] = STATUS_IDLE


def _migrate_user_info_fields(user_list):
    """Rename old 'timestamp' field to 'start_time' in user info records."""
    for user_info in user_list:
        if "timestamp" in user_info and "start_time" not in user_info:
            user_info["start_time"] = user_info.pop("timestamp")


def _cfg_get(config, key, default=None):
    """Resolve config value from instance or class-level Config."""
    if config is not None:
        return config.get_val(key, default)
    return Config.get(key, default)


def _bot_dir(config):
    """Return {DATA_DIR}/{bot_id} directory."""
    import tempfile

    bot_id = _cfg_get(config, "BOT_ID")
    if bot_id is None:
        return tempfile.mkdtemp()
    base = _cfg_get(config, "DATA_DIR") or os.path.join("/data", "bots")
    d = os.path.join(base, str(bot_id))
    os.makedirs(d, exist_ok=True)
    return d


def _migrate_old_state_file(state_filename, config):
    """Check for old-format state files and migrate if found.

    Only handles the old platform layout 'cluster_status.json'.
    """
    if os.path.exists(state_filename):
        return  # new file already exists, nothing to do

    # DEPRECATED: old platform layout "cluster_status.json" in same dir
    state_dir = os.path.dirname(state_filename)
    old_path = os.path.join(state_dir, "cluster_status.json")
    if os.path.exists(old_path):
        logger.info("Migrating old state file: %s -> %s", old_path, state_filename)
        os.makedirs(os.path.dirname(state_filename), exist_ok=True)
        os.rename(old_path, state_filename)


def _load_state_from_file(state_filename):
    """Read state JSON, supporting both old and new key names."""
    try:
        with open(state_filename, encoding="utf-8") as f:
            raw = json.load(f)
            # DEPRECATED: "cluster_status" / "cluster_state" are old key names, will be removed in future
            return raw.get("bot_state") or raw.get("cluster_state") or raw.get("cluster_status") or {}
    except Exception:
        return {}


# ── Operation logging ──────────────────────────────────────────


def log_to_file(user_id, command, node_key, dev_ids="", duration="", config=None):
    """Append a user operation log entry to the log file."""
    filename = os.path.join(_bot_dir(config), "bot.log")
    with open(filename, "a", encoding="utf-8") as f:
        now = datetime.now()
        formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{formatted_time}\t{user_id}\t{command}\t{node_key}\t{dev_ids}\t{duration}\n")


# ── Device state ───────────────────────────────────────────────


def create_or_load_device_state(config=None):
    """Create or load device cluster state, applying max-duration limits."""
    state_filename = os.path.join(_bot_dir(config), "bot_state.json")
    cluster_configs = _cfg_get(config, "CLUSTER_CONFIGS")
    max_duration = _cfg_get(config, "MAX_LOCK_DURATION", -1)

    default_state = {
        node_key: [
            {
                "dev_id": dev_id,
                "dev_model": devices[dev_id],
                "status": STATUS_IDLE,
                "current_users": [],
            }
            for dev_id in range(len(devices))
        ]
        for node_key, devices in cluster_configs.items()
    }

    _migrate_old_state_file(state_filename, config)

    if os.path.exists(state_filename):
        loaded_state = _load_state_from_file(state_filename)

        if not loaded_state:
            return default_state

        for node_key, node_status in loaded_state.items():
            if node_key not in cluster_configs:
                continue
            default_node = default_state[node_key]
            # Merge old state into default by dev_id
            old_by_id = {d["dev_id"]: d for d in node_status}
            for default_device in default_node:
                dev_id = default_device["dev_id"]
                if dev_id in old_by_id:
                    device_status = old_by_id[dev_id]
                    _migrate_status_fields(device_status)
                    _migrate_user_info_fields(device_status["current_users"])
                    apply_max_duration_limit(device_status["current_users"], max_duration)
                    default_device.update(**device_status)

        return default_state

    else:
        return default_state


# ── Node state ─────────────────────────────────────────────────


def create_or_load_node_state(config=None):
    """Create or load node state, applying max-duration limits."""
    state_filename = os.path.join(_bot_dir(config), "bot_state.json")
    cluster_configs = _cfg_get(config, "CLUSTER_CONFIGS")
    max_duration = _cfg_get(config, "MAX_LOCK_DURATION", -1)

    default_state = {
        shortname: {
            "status": STATUS_IDLE,
            "current_users": [],
            "booking_list": [],
        }
        for shortname in cluster_configs
    }

    _migrate_old_state_file(state_filename, config)

    if os.path.exists(state_filename):
        loaded_state = _load_state_from_file(state_filename)

        if not loaded_state:
            return default_state

        for node_key, node_status in loaded_state.items():
            if node_key not in default_state:
                continue
            _migrate_status_fields(node_status)
            node_status.pop("fullname", None)
            _migrate_user_info_fields(node_status.get("current_users", []))
            _migrate_user_info_fields(node_status.get("booking_list", []))
            apply_max_duration_limit(node_status.get("current_users", []), max_duration)
            default_state[node_key].update(**node_status)

        return default_state

    else:
        return default_state


# ── State persistence ──────────────────────────────────────────


def save_bot_state_to_file(data, config=None):
    """Persist bot state to file using atomic write."""
    file_path = os.path.join(_bot_dir(config), "bot_state.json")
    dir_name = os.path.dirname(file_path)

    os.makedirs(dir_name, exist_ok=True)

    # Write to temp file first, then atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"bot_state": data}, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, file_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
