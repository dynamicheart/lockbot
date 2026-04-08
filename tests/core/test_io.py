import json
import os
from unittest.mock import patch

from lockbot.core.config import Config
from lockbot.core.io import (
    _bot_dir,
    create_or_load_device_state,
    create_or_load_node_state,
    log_to_file,
    save_bot_state_to_file,
)


def _make_config(tmp_path, **overrides):
    """Create a Config instance with BOT_ID and DATA_DIR pointing to tmp_path."""
    data = {"BOT_ID": "test_io", "DATA_DIR": str(tmp_path)}
    data.update(overrides)
    return Config(config_dict=data)


def test_log_to_file(tmp_path):
    """Test log to file."""
    config = _make_config(tmp_path)

    user_id = "user1"
    command = "TEST_COMMAND"
    node_key = "node1"
    dev_ids = "dev1,dev2"
    duration = "3600"
    log_to_file(user_id, command, node_key, dev_ids, duration, config=config)

    log_file = os.path.join(_bot_dir(config), "bot.log")
    assert os.path.exists(log_file)

    with open(log_file, encoding="utf-8") as f:
        content = f.read()
    assert user_id in content
    assert command in content
    assert node_key in content
    assert dev_ids in content
    assert duration in content
    assert "-" in content and ":" in content


def test_create_or_load_device_state_file_not_exist(tmp_path):
    """Test create or load device state file not exist."""
    cluster_configs = {"node1": ["deviceA", "deviceB"]}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3600)

    # Ensure state file doesn't exist
    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    if os.path.exists(state_file):
        os.unlink(state_file)

    result = create_or_load_device_state(config=config)
    expected = {
        "node1": [
            {"dev_id": 0, "dev_model": "deviceA", "status": "idle", "current_users": []},
            {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
        ]
    }
    assert result == expected


def test_create_or_load_device_state_file_exists(tmp_path):
    """Test create or load device state file exists."""
    cluster_configs = {"node1": ["deviceA", "deviceB"]}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": [
                {
                    "dev_id": 0,
                    "dev_model": "deviceA",
                    "status": "exclusive",
                    "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 5000}],
                },
                {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
            ]
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    with patch("time.time", return_value=2000):
        result = create_or_load_device_state(config=config)

    expected = {
        "node1": [
            {
                "dev_id": 0,
                "dev_model": "deviceA",
                "status": "exclusive",
                "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 4000}],
            },
            {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
        ]
    }
    assert result == expected


def test_create_or_load_device_state_new_node(tmp_path):
    """Test create or load device state new node."""
    cluster_configs = {"node1": ["deviceA", "deviceB"], "node2": ["deviceC"]}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": [
                {
                    "dev_id": 0,
                    "dev_model": "deviceA",
                    "status": "exclusive",
                    "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 5000}],
                },
                {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
            ]
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    with patch("time.time", return_value=2000):
        result = create_or_load_device_state(config=config)

    expected = {
        "node1": [
            {
                "dev_id": 0,
                "dev_model": "deviceA",
                "status": "exclusive",
                "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 4000}],
            },
            {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
        ],
        "node2": [{"dev_id": 0, "dev_model": "deviceC", "status": "idle", "current_users": []}],
    }
    assert result == expected


def test_create_or_load_device_state_removed_node(tmp_path):
    """Test create or load device state removed node."""
    cluster_configs = {"node1": ["deviceA", "deviceB"]}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": [
                {"dev_id": 0, "dev_model": "deviceA", "status": "exclusive", "current_users": []},
                {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
            ],
            "node2": [{"dev_id": 0, "dev_model": "deviceC", "status": "exclusive", "current_users": []}],
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    result = create_or_load_device_state(config=config)

    expected = {
        "node1": [
            {"dev_id": 0, "dev_model": "deviceA", "status": "exclusive", "current_users": []},
            {"dev_id": 1, "dev_model": "deviceB", "status": "idle", "current_users": []},
        ]
    }
    assert result == expected


def test_create_or_load_node_state_file_not_exist(tmp_path):
    """Test create or load node state file not exist."""
    cluster_configs = {"node1": "Node One"}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3600)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    if os.path.exists(state_file):
        os.unlink(state_file)

    result = create_or_load_node_state(config=config)
    expected = {"node1": {"status": "idle", "current_users": [], "booking_list": []}}
    assert result == expected


def test_create_or_load_node_state_file_exists(tmp_path):
    """Test create or load node state file exists."""
    cluster_configs = {"node1": "Node One"}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": {
                "status": "shared",
                "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 5000}],
                "booking_list": [],
            }
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    with patch("time.time", return_value=2000):
        result = create_or_load_node_state(config=config)

    expected = {
        "node1": {
            "status": "shared",
            "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 4000}],
            "booking_list": [],
        }
    }
    assert result == expected


def test_create_or_load_node_state_new_node(tmp_path):
    """Test create or load node state new node."""
    cluster_configs = {"node1": "Node One", "node2": "Node Two"}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": {
                "status": "shared",
                "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 5000}],
                "booking_list": [],
            }
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    with patch("time.time", return_value=2000):
        result = create_or_load_node_state(config=config)

    expected = {
        "node1": {
            "status": "shared",
            "current_users": [{"user_id": "user1", "start_time": 1000, "duration": 4000}],
            "booking_list": [],
        },
        "node2": {"status": "idle", "current_users": [], "booking_list": []},
    }
    assert result == expected


def test_create_or_load_node_state_removed_node(tmp_path):
    """Test create or load node state removed node."""
    cluster_configs = {"node1": "Node One"}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {
        "cluster_status": {
            "node1": {"status": "shared", "current_users": [], "booking_list": []},
            "node2": {"status": "exclusive", "current_users": [], "booking_list": []},
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    result = create_or_load_node_state(config=config)

    expected = {"node1": {"status": "shared", "current_users": [], "booking_list": []}}
    assert result == expected


def test_create_or_load_node_state_no_booking_list_field(tmp_path):
    """Test create or load node state no booking list field."""
    cluster_configs = {"node1": "Node One"}
    config = _make_config(tmp_path, CLUSTER_CONFIGS=cluster_configs, MAX_LOCK_DURATION=3000)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    config_data = {"cluster_status": {"node1": {"status": "shared", "current_users": []}}}
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    result = create_or_load_node_state(config=config)

    expected = {"node1": {"status": "shared", "current_users": [], "booking_list": []}}
    assert result == expected


def test_save_bot_state_to_file(tmp_path):
    """Test save bot state to file."""
    config = _make_config(tmp_path)

    state_file = os.path.join(_bot_dir(config), "bot_state.json")
    if os.path.exists(state_file):
        os.unlink(state_file)

    data = {"node1": [{"dev_id": 0, "dev_model": "deviceA", "status": "exclusive", "current_users": []}]}
    save_bot_state_to_file(data, config=config)

    with open(state_file, encoding="utf-8") as f:
        file_data = json.load(f)
    assert "bot_state" in file_data
    assert file_data["bot_state"] == data
