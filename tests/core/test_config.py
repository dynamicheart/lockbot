import json
import os

import pytest
from lockbot.core.config import Config, ConfigValidationError


def test_set_valid_node_config():
    """Test setting a valid NODE config."""
    Config.reset()
    Config.set("BOT_TYPE", "NODE")
    Config.set("CLUSTER_CONFIGS", {"短名1": "真实名1"})
    assert Config.get("BOT_TYPE") == "NODE"


def test_set_invalid_bot_type():
    """Test that an invalid BOT_TYPE raises ConfigValidationError."""
    Config.reset()
    with pytest.raises(ConfigValidationError):
        Config.set("BOT_TYPE", "INVALID")
        Config.validate_config()


def test_set_invalid_cluster_for_node():
    """Test that invalid CLUSTER_CONFIGS for NODE raises ConfigValidationError."""
    Config.reset()
    Config.set("BOT_TYPE", "NODE")
    with pytest.raises(ConfigValidationError):
        Config.set("CLUSTER_CONFIGS", {"短名": ["应该是字符串"]})
        Config.validate_config()


def test_set_invalid_cluster_for_device():
    """Test that invalid CLUSTER_CONFIGS for DEVICE raises ConfigValidationError."""
    Config.reset()
    Config.set("BOT_TYPE", "DEVICE")
    with pytest.raises(ConfigValidationError):
        Config.set("CLUSTER_CONFIGS", {"机器名": "不是列表"})
        Config.validate_config()


def test_valid_device_config():
    """Test a valid DEVICE config passes validation."""
    Config.reset()
    Config.set("BOT_TYPE", "DEVICE")
    Config.set("CLUSTER_CONFIGS", {"machine1": ["gpu0", "gpu1"]})
    Config.validate_config()
    assert Config.get("BOT_TYPE") == "DEVICE"


def test_node_cluster_configs_support_list():
    """Test that NODE type auto-converts a list CLUSTER_CONFIGS to {k: k} dict."""
    Config.reset()
    Config.set("BOT_TYPE", "NODE")
    Config.set("CLUSTER_CONFIGS", ["node1", "node2"])

    Config.validate_config()

    assert Config.get("CLUSTER_CONFIGS") == {
        "node1": "node1",
        "node2": "node2",
    }


def test_queue_cluster_configs_support_list():
    """Test that QUEUE type auto-converts a list CLUSTER_CONFIGS to {k: k} dict."""
    Config.reset()
    Config.set("BOT_TYPE", "QUEUE")
    Config.set("CLUSTER_CONFIGS", ["queueA", "queueB"])

    Config.validate_config()

    assert Config.get("CLUSTER_CONFIGS") == {
        "queueA": "queueA",
        "queueB": "queueB",
    }


def test_node_cluster_configs_invalid_type():
    """Test that non-dict/list CLUSTER_CONFIGS for NODE raises ConfigValidationError."""
    Config.reset()
    Config.set("BOT_TYPE", "NODE")

    with pytest.raises(ConfigValidationError):
        Config.set("CLUSTER_CONFIGS", "not_a_list_or_dict")
        Config.validate_config()


def test_queue_cluster_configs_invalid_type():
    """Test that non-list/dict CLUSTER_CONFIGS for QUEUE raises ConfigValidationError."""
    Config.reset()
    Config.set("BOT_TYPE", "QUEUE")

    with pytest.raises(ConfigValidationError):
        Config.set("CLUSTER_CONFIGS", 123)
        Config.validate_config()


def test_load_valid_config_file(tmp_path):
    """Test loading a valid config file."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    filename = config_dir / "demo_bot_bot_config.json"

    config_data = {"BOT_TYPE": "NODE", "CLUSTER_CONFIGS": {"节点A": "node-a"}}
    filename.write_text(json.dumps(config_data))

    Config.reset()
    Config.set("CONFIG_FILENAME", str(filename))

    Config.load_from_file()

    assert Config.get("BOT_TYPE") == "NODE"
    assert Config.get("CLUSTER_CONFIGS") == {"节点A": "node-a"}


def test_load_config_file_with_invalid_json(tmp_path):
    """Test that invalid JSON in config file raises JSONDecodeError."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    filename = config_dir / "demo_bot_bot_config.json"
    filename.write_text("{ this is not: valid json }")

    Config.reset()
    Config.set("CONFIG_FILENAME", str(filename))

    with pytest.raises(json.JSONDecodeError):
        Config.load_from_file()


def test_load_config_file_with_invalid_cluster_config(tmp_path):
    """Test that invalid cluster config in file raises ConfigValidationError."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    filename = config_dir / "demo_bot_bot_config.json"
    # NODE expects str values in CLUSTER_CONFIGS, not list
    filename.write_text(json.dumps({"BOT_TYPE": "NODE", "CLUSTER_CONFIGS": {"节点1": ["不合法"]}}))

    Config.reset()
    Config.set("CONFIG_FILENAME", str(filename))

    with pytest.raises(ConfigValidationError):
        Config.load_from_file()


def test_load_config_file_missing(tmp_path):
    """Test that a missing config file does not raise and keeps defaults."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()

    Config.reset()
    # No CONFIG_FILENAME set — load_from_file should silently skip

    Config.load_from_file()

    assert Config.get("BOT_NAME") == "demo_bot"


def set_env_vars(env_vars):
    for key, value in env_vars.items():
        os.environ[key] = value


def clear_env_vars(env_vars):
    for key in env_vars:
        if key in os.environ:
            del os.environ[key]


def test_load_boolean_env():
    env_vars = {
        "EARLY_NOTIFY": "true",
        "LANGUAGE": "en",
    }
    set_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["EARLY_NOTIFY"] is True
    assert Config._config_data["LANGUAGE"] == "en"

    clear_env_vars(env_vars)


def test_load_integer_env():
    env_vars = {
        "PORT": "8080",
        "MAX_LOCK_DURATION": "30",
    }
    set_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["PORT"] == 8080
    assert Config._config_data["MAX_LOCK_DURATION"] == 30

    clear_env_vars(env_vars)


def test_load_string_env():
    env_vars = {
        "BOT_NAME": "test_bot",
        "TOKEN": "test_token_value",
    }
    set_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["BOT_NAME"] == "test_bot"
    assert Config._config_data["TOKEN"] == "test_token_value"

    clear_env_vars(env_vars)


def test_load_invalid_type_env():
    """Test that unsupported env var types are skipped, keeping defaults."""
    env_vars = {
        "CLUSTER_CONFIGS": "{'cluster_name': 'test_cluster'}",
    }
    set_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["CLUSTER_CONFIGS"] == {}

    clear_env_vars(env_vars)


def test_load_env_missing():
    """Test that missing env vars fall back to defaults."""
    env_vars = {
        "BOT_TYPE": "NODE",
    }
    clear_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["BOT_TYPE"] == "NODE"


def test_load_invalid_integer_env():
    """Test that an invalid integer env var falls back to default."""
    env_vars = {
        "PORT": "invalid_port",
    }
    set_env_vars(env_vars)
    Config.load_from_env()

    assert Config._config_data["PORT"] == 5000

    clear_env_vars(env_vars)
