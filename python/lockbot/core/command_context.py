"""Internal command origin; never chosen by a personal API request body."""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

command_source = ContextVar("lockbot_command_source", default="unknown")
command_action = ContextVar("lockbot_command_action", default=None)
command_initiator = ContextVar("lockbot_command_initiator", default=None)
command_correlation = ContextVar("lockbot_command_correlation", default=None)

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
    actor_token = command_initiator.set({"kind": "operator", "bot_id": bot_id} if source == "operator" else None)
    correlation_token = command_correlation.set(uuid.uuid4().hex)
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
        command_initiator.reset(actor_token)
        command_correlation.reset(correlation_token)
        observe_operation(bot_id, source, command, outcome)
