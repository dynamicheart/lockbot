"""Internal command origin; never chosen by a personal API request body."""

from contextvars import ContextVar

command_source = ContextVar("lockbot_command_source", default="unknown")
