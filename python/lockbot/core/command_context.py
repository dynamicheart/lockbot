"""Internal command origin; never chosen by a personal API request body."""

from contextlib import contextmanager
from contextvars import ContextVar

command_source = ContextVar("lockbot_command_source", default="unknown")
command_action = ContextVar("lockbot_command_action", default=None)

# Optional backend-owned telemetry sink. Core remains usable without the backend.
operation_observer = None
RESOURCE_OPERATIONS = frozenset({"lock", "slock", "unlock", "book", "take", "kickout", "kicklock", "opkick"})


def observe_operation(bot_id, source, command, outcome):
    """Best-effort, fixed-label metadata only; never pass text, keys or user identities."""
    import logging

    if operation_observer is None:
        return
    try:
        operation_observer(bot_id=bot_id, source=source, command=command, outcome=outcome)
    except Exception:
        logging.getLogger(__name__).exception("Operation telemetry failed")


@contextmanager
def operation_attempt(bot_id, source, command):
    """Count a direct operational mutation independently of its transport."""
    outcome = "dispatched"
    source_token = command_source.set(source)
    action_token = command_action.set(command)
    try:
        yield
    except ValueError:
        outcome = "rejected"
        raise
    except Exception:
        outcome = "uncertain"
        raise
    finally:
        command_source.reset(source_token)
        command_action.reset(action_token)
        observe_operation(bot_id, source, command, outcome)
