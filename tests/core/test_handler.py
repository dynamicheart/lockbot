from unittest.mock import MagicMock, patch

import pytest
from lockbot.core.handler import execute_command


@pytest.fixture
def base_msg():
    """Return a base message dict with fromuserid='user123' and empty content."""
    return {
        "message": {
            "header": {"fromuserid": "user123"},
            "body": [{"type": "TEXT", "content": ""}],
        }
    }


@pytest.fixture
def mock_robot():
    """Return a MagicMock simulating the Robot class with all command methods."""
    robot = MagicMock()
    robot.lock.return_value = {"action": "lock"}
    robot.slock.return_value = {"action": "slock"}
    robot.unlock.return_value = {"action": "unlock"}
    robot.kickout.return_value = {"action": "kickout"}
    robot.query.return_value = {"action": "query"}
    robot.print_help.return_value = {"action": "help"}
    robot.book.return_value = {"action": "book"}
    robot.take.return_value = {"action": "take"}

    robot.supported_commands.return_value = [
        "lock",
        "slock",
        "unlock",
        "kickout",
        "query",
        "help",
        "book",
        "take",
        "free",
    ]
    robot.config = None
    return robot


@patch("lockbot.core.config.Config.get")
def test_command_lock(config_get, base_msg, mock_robot):
    """Test that 'lock dev0' dispatches to robot.lock."""
    config_get.return_value = {}  # mock CLUSTER_CONFIGS
    base_msg["message"]["body"][0]["content"] = "lock dev0"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "lock"
    mock_robot.lock.assert_called_once_with("user123", "lock dev0")


@patch("lockbot.core.config.Config.get")
def test_command_help(config_get, base_msg, mock_robot):
    """Test that 'help' dispatches to robot.print_help."""
    config_get.return_value = {}
    base_msg["message"]["body"][0]["content"] = "help"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123")


@patch("lockbot.core.config.Config.get")
def test_command_node_key(config_get, base_msg, mock_robot):
    """Test that a known node_key dispatches to robot.query."""
    config_get.return_value = {"node1": {}}
    base_msg["message"]["body"][0]["content"] = "node1"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "query"
    mock_robot.query.assert_called_once_with("user123", "node1")


@patch("lockbot.core.config.Config.get")
def test_command_fallback(config_get, base_msg, mock_robot):
    """Test that an unrecognized command falls back to print_help."""
    config_get.return_value = {}
    base_msg["message"]["body"][0]["content"] = "foo bar"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123", "❌【未识别的命令】foo bar")


@patch("lockbot.core.config.Config.get")
def test_supported_command_allowed(config_get, base_msg, mock_robot):
    mock_robot.supported_commands.return_value = ["lock", "unlock", "help", "query", "free"]
    config_get.return_value = {}

    base_msg["message"]["body"][0]["content"] = "lock dev0"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "lock"
    mock_robot.lock.assert_called_once()


@patch("lockbot.core.config.Config.get")
def test_supported_command_disallowed(config_get, base_msg, mock_robot):
    # unlock/free not in supported_commands
    mock_robot.supported_commands.return_value = ["lock", "help", "query"]
    config_get.return_value = {}

    base_msg["message"]["body"][0]["content"] = "unlock dev0"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123", "❌【未识别的命令】unlock dev0")


@patch("lockbot.core.config.Config.get")
def test_supported_command_free_alias_not_supported(config_get, base_msg, mock_robot):
    # free as unlock alias, not in supported list
    mock_robot.supported_commands.return_value = ["lock", "help", "query"]
    config_get.return_value = {}

    base_msg["message"]["body"][0]["content"] = "free dev0"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123", "❌【未识别的命令】free dev0")


@patch("lockbot.core.config.Config.get")
def test_supported_command_empty_query(config_get, base_msg, mock_robot):
    # Empty command treated as query; if query unsupported, should error
    mock_robot.supported_commands.return_value = ["lock", "help"]
    config_get.return_value = {}

    base_msg["message"]["body"][0]["content"] = ""
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123", "❌【未识别的命令】")


@patch("lockbot.core.config.Config.get")
def test_supported_command_node_key_query(config_get, base_msg, mock_robot):
    # node_key as query param; if query unsupported, should error
    mock_robot.supported_commands.return_value = ["lock", "help"]
    config_get.return_value = {"node1": {}}

    base_msg["message"]["body"][0]["content"] = "node1"
    result = execute_command(base_msg, mock_robot)
    assert result["action"] == "help"
    mock_robot.print_help.assert_called_once_with("user123", "❌【未识别的命令】node1")
