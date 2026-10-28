"""
bot_instance.py - Bot instance factory.

Creates the appropriate Bot instance based on bot_type.
Each instance holds independent Config, State and Lock,
supporting multiple concurrent bots in a single process.
"""

from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot

_BOT_CLASS_MAP = {
    "NODE": NodeBot,
    "QUEUE": QueueBot,
    "DEVICE": DeviceBot,
    "QUEUE_DEVICE": QueueDeviceBot,
}


class BotInstance:
    """
    Wraps an independent Bot instance.

    Usage:
        instance = BotInstance("NODE", {"BOT_NAME": "my_bot", ...})
        instance.bot        # NodeBot / QueueBot / DeviceBot instance
        instance.bot.config # Config instance
        instance.bot.state  # State instance

    Scheduling is owned by BotManager. Construction never starts a thread.
    """

    def __init__(self, bot_type, config_dict=None, scheduler=None):
        if bot_type not in _BOT_CLASS_MAP:
            raise ValueError(f"Invalid bot_type '{bot_type}', must be one of {list(_BOT_CLASS_MAP.keys())}")

        full_config = dict(config_dict or {})
        full_config["BOT_TYPE"] = bot_type

        self.bot_type = bot_type
        bot_cls = _BOT_CLASS_MAP[bot_type]
        self.bot = bot_cls(config_dict=full_config)

        self.config = self.bot.config
        self.state = self.bot.state

        # The manager registers and removes this instance with its shared scheduler.
        self._scheduler = scheduler
