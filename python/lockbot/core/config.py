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
    "DEFAULT_DURATION": {
        "default": 2 * 60 * 60,
        "description": "Default lock duration in seconds (default 7200 = 2h)",
    },
    "TIME_ALERT": {
        "default": 5 * 60,
        "description": "Expiry alert threshold in seconds (default 300 = 5min)",
    },
    "MAX_LOCK_DURATION": {
        "default": -1,
        "description": "Max lock duration in seconds, -1 means unlimited",
    },
    "EARLY_NOTIFY": {
        "default": False,
        "description": "True: warn before expiry; False: release and notify on expiry",
    },
    "LANGUAGE": {
        "default": "zh",
        "description": "Bot display language: zh (Chinese) or en (English)",
    },
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
            self._data.update(config_dict)
        self._normalize()

    def _normalize(self):
        """Normalize config values (e.g. list -> dict for CLUSTER_CONFIGS)."""
        cc = self._data.get("CLUSTER_CONFIGS")
        if isinstance(cc, list):
            self._data["CLUSTER_CONFIGS"] = {k: k for k in cc}

    def get_val(self, key, default=None):
        """Read a value from this bot."""
        if key in self._data:
            return self._data[key]
        return self._derive_path(key, default)

    def set_val(self, key, value):
        """Instance-level set."""
        self._data[key] = value

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
