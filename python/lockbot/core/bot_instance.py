"""
bot_instance.py - Bot instance factory.

Creates the appropriate Bot instance based on bot_type.
Each instance holds independent Config, State and Lock,
supporting multiple concurrent bots in a single process.
"""

from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot

_BOT_CLASS_MAP = {
    "NODE": NodeBot,
    "QUEUE": QueueBot,
    "DEVICE": DeviceBot,
}

_STANDALONE_KEY = 0  # internal scheduler key used in standalone mode


class BotInstance:
    """
    Wraps an independent Bot instance.

    Usage:
        instance = BotInstance("NODE", {"BOT_NAME": "my_bot", ...})
        instance.bot        # NodeBot / QueueBot / DeviceBot instance
        instance.bot.config # Config instance
        instance.bot.state  # State instance

    Scheduling:
        Standalone mode (scheduler=None, default):
            A private BotScheduler is created and started automatically.
            Call instance.shutdown() to stop it when done.

        Managed mode (scheduler provided by BotManager):
            No scheduler is created here.  BotManager calls
            scheduler.add(bot_id, instance) after construction and
            scheduler.remove(bot_id) on teardown.
    """

    def __init__(self, bot_type, config_dict=None, scheduler=None, auto_start=True):
        if bot_type not in _BOT_CLASS_MAP:
            raise ValueError(f"Invalid bot_type '{bot_type}', must be one of {list(_BOT_CLASS_MAP.keys())}")

        full_config = dict(config_dict or {})
        full_config["BOT_TYPE"] = bot_type

        self.bot_type = bot_type
        bot_cls = _BOT_CLASS_MAP[bot_type]
        self.bot = bot_cls(config_dict=full_config)

        self.config = self.bot.config
        self.state = self.bot.state

        if not auto_start:
            # Testing/manual mode: no scheduler
            self._scheduler = None
            self._owns_scheduler = False
        elif scheduler is None:
            # Standalone mode: own scheduler, start immediately
            from lockbot.core.scheduler import BotScheduler

            self._scheduler = BotScheduler()
            self._scheduler.start()
            self._owns_scheduler = True
            self._scheduler.add(_STANDALONE_KEY, self, delay=0.0)
        else:
            # Managed mode: shared scheduler injected by BotManager
            self._scheduler = scheduler
            self._owns_scheduler = False

    def shutdown(self) -> None:
        """Stop scheduling (standalone mode only). No-op in managed mode."""
        if self._owns_scheduler:
            self._scheduler.remove(_STANDALONE_KEY)
            self._scheduler.stop()
