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
from lockbot.core.scheduler import BotScheduler

logger = logging.getLogger(__name__)


class BotManager:
    """
    Manages bot instances running in the FastAPI process.

    Usage:
        manager = BotManager()
        manager.start_scheduler()
        manager.start_bot(bot_id=1, config_dict={...})
        instance = manager.get_instance(bot_id=1)
        manager.stop_bot(bot_id=1)
        manager.shutdown_all()
    """

    def __init__(self):
        self._bots: dict[int, BotInstance] = {}
        self._lock = threading.Lock()
        self._scheduler = BotScheduler()

    def start_scheduler(self) -> None:
        """Start the shared scheduler background thread. Call once at app startup."""
        self._scheduler.start()

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

            instance = BotInstance(
                config_dict.get("BOT_TYPE", "NODE"),
                config_dict,
                scheduler=self._scheduler,  # managed mode: shared scheduler
            )
            self._bots[bot_id] = instance

        # Schedule first check immediately (outside _lock to avoid lock ordering issues)
        self._scheduler.add(
            bot_id,
            instance,
            delay=0.0,
            on_fatal_error=self._make_fatal_error_handler(bot_id),
        )
        # Wire up state-change callback so new locks wake the scheduler immediately
        instance.bot._on_state_changed = lambda: self._scheduler.reschedule_soon(bot_id)
        logger.info("Started bot %d in-process (type=%s)", bot_id, instance.bot_type)
        return os.getpid()

    def _make_fatal_error_handler(self, bot_id: int):
        """Return a closure that marks the bot as 'error' in DB after too many failures."""

        def _on_fatal_error(failed_bot_id: int) -> None:
            # Remove from active bots (scheduler already removed itself)
            with self._lock:
                self._bots.pop(failed_bot_id, None)

            # Update DB status — runs in scheduler thread, create own session
            try:
                from lockbot.backend.app.bots.models import Bot as BotModel
                from lockbot.backend.app.database import SessionLocal

                db = SessionLocal()
                try:
                    bot = db.query(BotModel).filter(BotModel.id == failed_bot_id).first()
                    if bot:
                        bot.status = "error"
                        bot.pid = None
                        db.commit()
                        logger.critical(
                            "Bot %d (%s): marked as error in DB after repeated failures",
                            failed_bot_id,
                            bot.name,
                        )
                finally:
                    db.close()
            except Exception:
                logger.exception("Bot %d: failed to update DB status to error", failed_bot_id)

        return _on_fatal_error

    def stop_bot(self, bot_id: int) -> None:
        """
        Stop a bot instance.

        Raises:
            RuntimeError: If the bot is not running.
        """
        # Cancel scheduling before removing from _bots
        self._scheduler.remove(bot_id)
        with self._lock:
            if bot_id not in self._bots:
                raise RuntimeError(f"Bot {bot_id} is not running")
            self._bots.pop(bot_id)
        logger.info("Stopped bot %d", bot_id)

    def restart_bot(self, bot_id: int, config_dict: dict) -> int:
        """Stop then start a bot. Returns PID."""
        if self.is_running(bot_id):
            self.stop_bot(bot_id)
        return self.start_bot(bot_id, config_dict)

    def shutdown_all(self) -> None:
        """Stop the scheduler and clear all running bots. Called on platform shutdown."""
        self._scheduler.stop()
        with self._lock:
            self._bots.clear()
        # Clear scheduler state so it can be safely restarted (e.g. in tests)
        with self._scheduler._lock:
            self._scheduler._instances.clear()
            self._scheduler._heap.clear()
            self._scheduler._gens.clear()
            self._scheduler._failure_counts.clear()
            self._scheduler._callbacks.clear()


# Module-level singleton
bot_manager = BotManager()
