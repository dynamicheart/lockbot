"""
Tests for BotManager (in-process mode with mocked BotInstance).
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager


@pytest.fixture()
def manager():
    return BotManager()


@pytest.fixture()
def sample_config():
    return {
        "BOT_NAME": "test_bot",
        "BOT_TYPE": "NODE",
        "WEBHOOK_URL": "https://example.com/webhook",
        "AESKEY": "",
        "TOKEN": "tok123",
        "CLUSTER_CONFIGS": {"node1": "node1"},
    }


class TestStartBot:
    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_start_bot_success(self, mock_cls, manager, sample_config):
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        pid = manager.start_bot(bot_id=1, config_dict=sample_config)

        assert pid == os.getpid()
        assert manager.is_running(1)
        assert manager.get_pid(1) == os.getpid()
        mock_cls.assert_called_once_with("NODE", sample_config, scheduler=manager._scheduler)

    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_start_bot_already_running(self, mock_cls, manager, sample_config):
        mock_cls.return_value = MagicMock()

        manager.start_bot(bot_id=1, config_dict=sample_config)
        with pytest.raises(RuntimeError, match="already running"):
            manager.start_bot(bot_id=1, config_dict=sample_config)

    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_get_instance(self, mock_cls, manager, sample_config):
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        manager.start_bot(bot_id=1, config_dict=sample_config)
        assert manager.get_instance(1) is mock_instance

    def test_get_instance_not_running(self, manager):
        assert manager.get_instance(99) is None


class TestStopBot:
    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_stop_bot_success(self, mock_cls, manager, sample_config):
        mock_cls.return_value = MagicMock()

        manager.start_bot(bot_id=2, config_dict=sample_config)
        manager.stop_bot(bot_id=2)

        assert not manager.is_running(2)
        assert manager.get_pid(2) is None

    def test_stop_bot_not_running(self, manager):
        with pytest.raises(RuntimeError, match="not running"):
            manager.stop_bot(bot_id=99)


class TestRestartBot:
    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_restart_running_bot(self, mock_cls, manager, sample_config):
        mock_cls.return_value = MagicMock()

        manager.start_bot(bot_id=4, config_dict=sample_config)
        new_pid = manager.restart_bot(bot_id=4, config_dict=sample_config)

        assert new_pid == os.getpid()
        assert manager.is_running(4)

    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_restart_stopped_bot(self, mock_cls, manager, sample_config):
        mock_cls.return_value = MagicMock()

        pid = manager.restart_bot(bot_id=5, config_dict=sample_config)

        assert pid == os.getpid()
        assert manager.is_running(5)


class TestShutdownAll:
    @patch("lockbot.backend.app.bots.manager.BotInstance")
    def test_shutdown_all(self, mock_cls, manager, sample_config):
        mock_cls.return_value = MagicMock()

        for i in range(3):
            manager.start_bot(bot_id=10 + i, config_dict=sample_config)
        manager.shutdown_all()

        for i in range(3):
            assert not manager.is_running(10 + i)
