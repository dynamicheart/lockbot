"""
Bot instance manager (in-process, shared port).

All bots run inside the FastAPI process as BotInstance objects.
Webhook callbacks arrive at /api/bots/webhook/{bot_id} and are
dispatched to the corresponding BotInstance.
"""

import logging
import os
import threading

from lockbot.core.bot_instance import BotInstance

logger = logging.getLogger(__name__)


class BotManager:
    """
    Manages bot instances running in the FastAPI process.

    Usage:
        manager = BotManager()
        manager.start_bot(bot_id=1, config_dict={...})
        instance = manager.get_instance(bot_id=1)
        manager.stop_bot(bot_id=1)
    """

    def __init__(self):
        self._bots: dict[int, BotInstance] = {}
        self._lock = threading.Lock()

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self._bots

    def get_pid(self, bot_id: int) -> int | None:
        if bot_id in self._bots:
            return os.getpid()
        return None

    def get_instance(self, bot_id: int) -> BotInstance | None:
        """Get the BotInstance for webhook dispatching."""
        return self._bots.get(bot_id)

    def start_bot(self, bot_id: int, config_dict: dict) -> int:
        """
        Start a bot instance in-process.

        Returns:
            PID of the current process.

        Raises:
            RuntimeError: If the bot is already running.
        """
        with self._lock:
            if bot_id in self._bots:
                raise RuntimeError(f"Bot {bot_id} is already running")

            instance = BotInstance(config_dict.get("BOT_TYPE", "NODE"), config_dict)

            self._bots[bot_id] = instance
            logger.info("Started bot %d in-process (type=%s)", bot_id, instance.bot_type)
            return os.getpid()

    def stop_bot(self, bot_id: int) -> None:
        """
        Stop a bot instance.

        Raises:
            RuntimeError: If the bot is not running.
        """
        with self._lock:
            if bot_id not in self._bots:
                raise RuntimeError(f"Bot {bot_id} is not running")

            instance = self._bots.pop(bot_id)
            _cancel_timer(instance.bot)
            logger.info("Stopped bot %d", bot_id)

    def restart_bot(self, bot_id: int, config_dict: dict) -> int:
        """Stop then start a bot. Returns PID."""
        if self.is_running(bot_id):
            self.stop_bot(bot_id)
        return self.start_bot(bot_id, config_dict)

    def shutdown_all(self) -> None:
        """Stop all running bots. Called on platform shutdown."""
        with self._lock:
            for bot_id in list(self._bots.keys()):
                try:
                    instance = self._bots.pop(bot_id)
                    _cancel_timer(instance.bot)
                except Exception:
                    logger.exception("Error stopping bot %d during shutdown", bot_id)


def _cancel_timer(bot) -> None:
    """Best-effort cancel of a bot's recurring timer thread."""
    timer = getattr(bot, "_timer", None)
    if timer and isinstance(timer, threading.Timer):
        timer.cancel()
        bot._timer = None


# Module-level singleton
bot_manager = BotManager()
