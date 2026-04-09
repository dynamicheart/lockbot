"""Configuration management with class-level and instance-level validation."""

import json
import os
import warnings

from lockbot.core.env import get_boolean_env


class ConfigValidationError(Exception):
    """Raised when configuration is invalid."""


# ── Config schema (key -> default, description, env_supported) ──────────
_CONFIG_SCHEMA = {
    "WEBHOOK_URL": {
        "default": "",
        "description": "Bot webhook callback URL for sending messages",
        "env": True,
    },
    "TOKEN": {
        "default": "",
        "description": "Bot token for signature verification",
        "env": True,
    },
    "AESKEY": {
        "default": "",
        "description": "Bot AES key for message decryption",
        "env": True,
    },
    "CLUSTER_CONFIGS": {
        "default": {},
        "description": ("Cluster config. NODE/QUEUE: [name_list]; DEVICE: {node_name: [device_model_list]}"),
        "env": False,
    },
    "BOT_NAME": {
        "default": "demo_bot",
        "description": "Bot name, used for config/state/log filenames",
        "env": True,
    },
    "BOT_TYPE": {
        "default": "NODE",
        "description": "Bot type: NODE / DEVICE / QUEUE",
        "env": True,
    },
    "DEFAULT_DURATION": {
        "default": 2 * 60 * 60,
        "description": "Default lock duration in seconds (default 7200 = 2h)",
        "env": True,
    },
    "TIME_ALERT": {
        "default": 5 * 60,
        "description": "Expiry alert threshold in seconds (default 300 = 5min)",
        "env": True,
    },
    "MAX_LOCK_DURATION": {
        "default": -1,
        "description": "Max lock duration in seconds, -1 means unlimited",
        "env": True,
    },
    "EARLY_NOTIFY": {
        "default": False,
        "description": "True: warn before expiry; False: release and notify on expiry",
        "env": True,
    },
    "PORT": {
        "default": 5000,
        "description": "Flask server listen port",
        "env": True,
    },
    "LANGUAGE": {
        "default": "zh",
        "description": "Bot display language: zh (Chinese) or en (English)",
        "env": True,
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
    """
    Config class for managing configuration with validation.

    Supports two usage modes:
    - Class-level (legacy): Config.get/set/load_from_file/load_from_env
    - Instance-level (new): config = Config(config_dict)
    """

    _ALLOWED_BOT_TYPES = {"NODE", "DEVICE", "QUEUE"}

    _default_config = {key: item["default"] for key, item in _CONFIG_SCHEMA.items()}
    _default_config.update(_INTERNAL_DEFAULTS)

    # ── Class-level global config (legacy path) ──────────────────────────
    _config_data = _default_config.copy()

    _DEPRECATION_MSG = (
        "Class-level Config methods are deprecated and will be removed in a future version. "
        "Use instance-level methods: config = Config(config_dict); config.get_val() / config.set_val()."
    )

    # ── Instance support (new path) ──────────────────────────────────────

    def __init__(self, config_dict=None):
        """
        Create an independent Config instance.

        Args:
            config_dict: Initial config overrides on top of defaults.
        """
        self._data = self._default_config.copy()
        if config_dict:
            self._data.update(config_dict)
        self._normalize()

    def _normalize(self):
        """Normalize config values (e.g. list -> dict for CLUSTER_CONFIGS)."""
        cc = self._data.get("CLUSTER_CONFIGS")
        if isinstance(cc, list):
            self._data["CLUSTER_CONFIGS"] = {k: k for k in cc}

    def get_val(self, key, default=None):
        """Instance-level get (same logic as classmethod get)."""
        if key in self._data:
            return self._data[key]
        return self._derive_path(key, default)

    def set_val(self, key, value):
        """Instance-level set."""
        self._data[key] = value

    def _derive_path(self, key, default=None):
        """Derive computed path keys. Uses BOT_ID-based layout: /data/bots/{bot_id}/."""
        bot_id = self._data.get("BOT_ID")
        if not bot_id:
            return default
        base_dir = os.path.join("/data", "bots", str(bot_id))
        if key == "STATE_FILENAME":
            return os.path.join(base_dir, "bot_state.json")
        return default

    # ── Class-level methods (deprecated — use instance-level instead) ────

    @classmethod
    def load_from_file(cls):
        """Load configuration from file and validate it.

        .. deprecated:: Use instance-level Config(config_dict) instead.
        """
        warnings.warn(cls._DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
        filename = cls.get("CONFIG_FILENAME")
        if filename and os.path.exists(filename):
            with open(filename) as f:
                loaded = json.load(f)
                for key, value in loaded.items():
                    print(f"Loaded configuration file: {key}: {value}")
                cls._config_data.update(loaded)
                cls.validate_config()

    @classmethod
    def load_from_env(cls):
        """Load configuration from environment variables (bool/int/str only).

        .. deprecated:: Use instance-level Config(config_dict) instead.
        """
        for key, default_value in cls._default_config.items():
            env_value = os.environ.get(key)

            if env_value is not None:
                print(f"Loading environment variable '{key}': {env_value}")
                if isinstance(default_value, bool):
                    cls._config_data[key] = get_boolean_env(key, str(default_value))
                elif isinstance(default_value, int):
                    try:
                        cls._config_data[key] = int(env_value)
                    except ValueError:
                        print(f"Warning: Environment variable '{key}' is not a valid integer. Using default value.")
                        cls._config_data[key] = default_value
                elif isinstance(default_value, str):
                    cls._config_data[key] = env_value
                else:
                    print(
                        f"Warning: Environment variable '{key}' with type "
                        f"{type(default_value)} is not supported. Skipping."
                    )

        cls.validate_config()

    @classmethod
    def get(cls, key, default=None):
        """Get configuration by key.

        .. deprecated:: Use config.get_val(key, default) instead.
        """
        if key in cls._config_data:
            return cls._config_data[key]
        return default

    @classmethod
    def set(cls, key, value):
        """Set configuration by key.

        .. deprecated:: Use config.set_val(key, value) instead.
        """
        cls._config_data[key] = value

    @classmethod
    def get_all(cls):
        """Return a shallow copy of all class-level config data.

        .. deprecated:: Use config._data instead.
        """
        return cls._config_data.copy()

    @classmethod
    def reset(cls):
        """Reset to default config (for testing purposes).

        .. deprecated:: Create a new Config instance instead.
        """
        cls._config_data = cls._default_config.copy()

    @classmethod
    def validate_config(cls):
        """Validate critical config fields.

        .. deprecated:: Create a new Config instance for validation instead.
        """
        BOT_TYPE = cls._config_data.get("BOT_TYPE")
        if BOT_TYPE not in cls._ALLOWED_BOT_TYPES:
            raise ConfigValidationError(f"Invalid BOT_TYPE '{BOT_TYPE}', must be one of {cls._ALLOWED_BOT_TYPES}")

        cluster_configs = cls._config_data.get("CLUSTER_CONFIGS", {})

        if BOT_TYPE in ["NODE", "QUEUE"]:
            if isinstance(cluster_configs, dict):
                for k, v in cluster_configs.items():
                    if not isinstance(v, str):
                        raise ConfigValidationError(
                            f"NODE CLUSTER_CONFIGS must map to str. Got {type(v)} for key '{k}'"
                        )
            elif isinstance(cluster_configs, list):
                cluster_configs = {k: k for k in cluster_configs}
                cls._config_data["CLUSTER_CONFIGS"] = cluster_configs
            else:
                raise ConfigValidationError("CLUSTER_CONFIGS must be either a list or a dict.")
        elif BOT_TYPE == "DEVICE":
            if not isinstance(cluster_configs, dict):
                raise ConfigValidationError("CLUSTER_CONFIGS must be a dictionary.")
            for k, v in cluster_configs.items():
                if not isinstance(v, list):
                    raise ConfigValidationError(f"DEVICE CLUSTER_CONFIGS must map to list. Got {type(v)} for key '{k}'")

    @classmethod
    def show_all(cls, as_json=False):
        """Display all current config key-values.

        .. deprecated:: Use config._data directly instead.
        """
        print("Current Configuration:")
        if as_json:
            return json.dumps(cls._config_data, indent=4, ensure_ascii=False)
        else:
            for key, value in cls._config_data.items():
                print(f"{key}: {value}")

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
                    "env_supported": item["env"],
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

            env_tag = "[ENV]" if item["env"] else "     "
            lines.append(f"  {env_tag} {key}")
            lines.append(f"         {item['description']}")
            lines.append(f"         Default: {default_str}")
            lines.append("")

        lines.append("-" * 70)
        lines.append("[ENV] marked items can be overridden via environment variables")
        lines.append("Priority: environment variable > config file > default")
        lines.append("")
        return "\n".join(lines)
