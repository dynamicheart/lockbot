"""Per-bot configuration; platform settings supply runtime values."""

import copy
import json
import os


class ConfigValidationError(Exception):
    """Raised when configuration is invalid."""


# ── Config schema (key -> per-bot default and description) ────────────
_CONFIG_SCHEMA = {
    "WEBHOOK_URL": {
        "default": "",
        "description": "Bot webhook callback URL for sending messages",
    },
    "TOKEN": {
        "default": "",
        "description": "Bot token for signature verification",
    },
    "AESKEY": {
        "default": "",
        "description": "Bot AES key for message decryption",
    },
    "CLUSTER_CONFIGS": {
        "default": {},
        "description": ("Cluster config. NODE/QUEUE: [name_list]; DEVICE: {node_name: [device_model_list]}"),
    },
    "BOT_NAME": {
        "default": "demo_bot",
        "description": "Bot name, used for config/state/log filenames",
    },
    "BOT_TYPE": {
        "default": "NODE",
        "description": "Bot type: NODE / DEVICE / QUEUE",
    },
    "FEISHU_QUERY_PANEL": {"default": False, "description": "Refresh the latest Feishu query on reply", "env": False},
    "FEISHU_MEMBERSHIP_NOTICES": {
        "default": False,
        "description": "Observe Feishu bot membership and warn administrators on removal",
    },
    "FEISHU_REACTION_REPLIES": {"default": False, "description": "React instead of short successful Feishu replies"},
    "FORBID_RELOCK": {"default": False, "description": "Reject renewal of existing holdings", "env": True},
    "MAX_RESOURCES_PER_USER": {
        "default": -1,
        "description": "Held and booked resources per user; -1 unlimited",
        "env": True,
    },
    "DEFAULT_DURATION": {
        "default": 2 * 60 * 60,
        "description": "Default lock duration in seconds (default 7200 = 2h)",
    },
    "TIME_ALERT": {
        "default": 5 * 60,
        "description": "Expiry alert threshold in seconds (default 300 = 5min)",
    },
    "OVERNIGHT_ENABLED": {"default": False, "description": "Allow bounded overnight leases", "env": False},
    "OVERNIGHT_PAUSE_IDLE_CHECK": {
        "default": False,
        "description": "Pause low-utilization checks during the configured overnight window",
        "env": False,
    },
    "OVERNIGHT_START": {"default": "22:00", "description": "Local evening start", "env": False},
    "OVERNIGHT_END": {"default": "09:00", "description": "Local morning cutoff", "env": False},
    "OVERNIGHT_TIMEZONE": {
        "default": "Asia/Shanghai",
        "description": "IANA timezone for the overnight window",
        "env": False,
    },
    "MAX_LOCK_DURATION": {
        "default": -1,
        "description": "Max lock duration in seconds, -1 means unlimited",
    },
    "EARLY_NOTIFY": {
        "default": False,
        "description": "True: warn before expiry; False: release and notify on expiry",
    },
    "IDLE_UTILIZATION_CHECK_ENABLED": {
        "default": True,
        "description": "Allow idle utilization checks on operator-configured targets",
        "env": False,
    },
    "GROUP_REMIND_AGENT_IDLE": {
        "default": False,
        "description": "Send group reminders for low-utilization Agent locks",
        "env": False,
    },
    "GROUP_REMIND_API_RESOURCES": {
        "default": False,
        "description": "Include API holdings and bookings in automatic group reminders",
    },
    "QUEUE_AUTO_LOCK": {
        "default": False,
        "description": "Automatically lock an available resource for the first booking",
    },
    "LANGUAGE": {
        "default": "zh",
        "description": "Bot display language: zh (Chinese) or en (English)",
    },
    "ALLOW_USER_LABEL": {"default": False, "description": "Allow users to set or clear shared node labels"},
    "SHOW_NODE_LABEL": {"default": False, "description": "Show shared node labels in chat replies"},
    "NODE_LABELS": {"default": {}, "description": "Shared labels by exact node name"},
    "ALLOWED_GROUP_IDS": {
        "default": [],
        "description": "List of group_ids where opkick is allowed. Empty = allow from any group.",
    },
}

# ── Internal constants (not configurable via file/env) ─────────────────
_INTERNAL_DEFAULTS = {
    "DEFAULT_USER_INFO": {
        "user_id": "xxx",
        "start_time": 0,
        "duration": 0,
        "is_notified": False,
    },
}

_LEGACY_LABEL_KEYS = {
    "ALLOW_USER_ALIAS": "ALLOW_USER_LABEL",
    "SHOW_NODE_ALIAS": "SHOW_NODE_LABEL",
    "NODE_ALIASES": "NODE_LABELS",
}


def normalize_label_settings(config):
    values = config.copy()
    for old, new in _LEGACY_LABEL_KEYS.items():
        if old in values:
            values.setdefault(new, values.pop(old))
    return values


class Config:
    """Independent configuration for one bot; no process-global configuration."""

    _ALLOWED_BOT_TYPES = {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}

    _default_config = {key: item["default"] for key, item in _CONFIG_SCHEMA.items()}
    _default_config.update(_INTERNAL_DEFAULTS)

    def __init__(self, config_dict=None):
        """
        Create an independent Config instance.

        Args:
            config_dict: Initial config overrides on top of defaults.
        """
        self._data = copy.deepcopy(self._default_config)
        if config_dict:
            self._data.update(normalize_label_settings(config_dict))
        if config_dict and any(key.startswith("OVERNIGHT_") for key in config_dict):
            from lockbot.core.overnight import window_settings

            for key in ("OVERNIGHT_ENABLED", "OVERNIGHT_PAUSE_IDLE_CHECK"):
                if not isinstance(self._data[key], bool):
                    raise ValueError(f"{key} must be a boolean")
            window_settings(self._data)
        self._normalize()
        cap = self.get_val("MAX_RESOURCES_PER_USER", -1)
        if isinstance(cap, bool) or not isinstance(cap, int) or (cap != -1 and cap < 1):
            raise ValueError("MAX_RESOURCES_PER_USER must be -1 or a positive integer")
        if not isinstance(self.get_val("FEISHU_QUERY_PANEL", False), bool):
            raise ValueError("FEISHU_QUERY_PANEL must be a boolean")

        if not isinstance(self.get_val("FEISHU_MEMBERSHIP_NOTICES", False), bool):
            raise ValueError("FEISHU_MEMBERSHIP_NOTICES must be a boolean")
        if not isinstance(self.get_val("FEISHU_REACTION_REPLIES", False), bool):
            raise ValueError("FEISHU_REACTION_REPLIES must be a boolean")
        if not isinstance(self.get_val("FORBID_RELOCK", False), bool):
            raise ValueError("FORBID_RELOCK must be a boolean")
        if not isinstance(self.get_val("QUEUE_AUTO_LOCK"), bool):
            raise ValueError("QUEUE_AUTO_LOCK must be a boolean")

    def _normalize(self):
        """Normalize config values (e.g. list -> dict for CLUSTER_CONFIGS)."""
        cc = self._data.get("CLUSTER_CONFIGS")
        if isinstance(cc, list):
            self._data["CLUSTER_CONFIGS"] = {k: k for k in cc}

    def get_val(self, key, default=None):
        """Read a value from this bot."""
        key = _LEGACY_LABEL_KEYS.get(key, key)
        if key in self._data:
            return self._data[key]
        return self._derive_path(key, default)

    def set_val(self, key, value):
        """Instance-level set."""
        self._data[_LEGACY_LABEL_KEYS.get(key, key)] = value

    def _derive_path(self, key, default=None):
        """Derive computed path keys. Uses BOT_ID-based layout: {DATA_DIR}/bots/{bot_id}/."""
        bot_id = self._data.get("BOT_ID")
        if not bot_id:
            return default
        data_dir = self._data.get("DATA_DIR") or os.environ.get("DATA_DIR", "/data")
        base_dir = os.path.join(data_dir, "bots", str(bot_id))
        if key == "STATE_FILENAME":
            return os.path.join(base_dir, "bot_state.json")
        return default

    def validate_config(self):
        """Validate the bot type and normalized cluster configuration."""
        BOT_TYPE = self._data.get("BOT_TYPE")
        if BOT_TYPE not in self._ALLOWED_BOT_TYPES:
            raise ConfigValidationError(f"Invalid BOT_TYPE '{BOT_TYPE}', must be one of {self._ALLOWED_BOT_TYPES}")

        cluster_configs = self._data.get("CLUSTER_CONFIGS", {})

        if BOT_TYPE in ["NODE", "QUEUE"]:
            if isinstance(cluster_configs, dict):
                for k, v in cluster_configs.items():
                    if not isinstance(v, str):
                        raise ConfigValidationError(
                            f"NODE CLUSTER_CONFIGS must map to str. Got {type(v)} for key '{k}'"
                        )
            elif isinstance(cluster_configs, list):
                cluster_configs = {k: k for k in cluster_configs}
                self._data["CLUSTER_CONFIGS"] = cluster_configs
            else:
                raise ConfigValidationError("CLUSTER_CONFIGS must be either a list or a dict.")
        elif BOT_TYPE in {"DEVICE", "QUEUE_DEVICE"}:
            if not isinstance(cluster_configs, dict):
                raise ConfigValidationError("CLUSTER_CONFIGS must be a dictionary.")
            for k, v in cluster_configs.items():
                if not isinstance(v, list):
                    raise ConfigValidationError(f"DEVICE CLUSTER_CONFIGS must map to list. Got {type(v)} for key '{k}'")

    # ── Config documentation ─────────────────────────────────────────

    @staticmethod
    def help(as_text=True):
        """
        Generate configuration reference documentation.

        Args:
            as_text: If True, return formatted text; otherwise return
                     list of dicts.

        Returns:
            str or list: Configuration documentation.
        """
        if not as_text:
            return [
                {
                    "key": key,
                    "default": item["default"],
                    "description": item["description"],
                    "env_supported": False,
                }
                for key, item in _CONFIG_SCHEMA.items()
            ]

        lines = []
        lines.append("=" * 70)
        lines.append("lockbot Configuration Reference")
        lines.append("=" * 70)
        lines.append("")

        for key, item in _CONFIG_SCHEMA.items():
            default_val = item["default"]
            if isinstance(default_val, (dict, list)):
                default_str = json.dumps(default_val, ensure_ascii=False)
            else:
                default_str = repr(default_val)

            lines.append(f"  {key}")
            lines.append(f"         {item['description']}")
            lines.append(f"         Default: {default_str}")
            lines.append("")

        lines.append("-" * 70)
        lines.append("Platform-supplied per-bot values override defaults")
        lines.append("")
        return "\n".join(lines)
