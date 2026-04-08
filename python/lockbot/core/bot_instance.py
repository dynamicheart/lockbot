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


class BotInstance:
    """
    Wraps an independent Bot instance.

    Usage:
        instance = BotInstance("NODE", {"BOT_NAME": "my_bot", ...})
        instance.bot        # NodeBot / QueueBot / DeviceBot instance
        instance.bot.config # Config instance
        instance.bot.state  # State instance

    The timer routine (periodic lock expiry check) starts automatically
    unless auto_start=False is passed.
    """

    def __init__(self, bot_type, config_dict=None, auto_start=True):
        if bot_type not in _BOT_CLASS_MAP:
            raise ValueError(f"Invalid bot_type '{bot_type}', must be one of {list(_BOT_CLASS_MAP.keys())}")

        full_config = dict(config_dict or {})
        full_config["BOT_TYPE"] = bot_type

        self.bot_type = bot_type
        bot_cls = _BOT_CLASS_MAP[bot_type]
        self.bot = bot_cls(config_dict=full_config)

        self.config = self.bot.config
        self.state = self.bot.state

        if auto_start:
            self.bot.timer_routine()
