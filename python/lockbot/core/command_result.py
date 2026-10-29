"""Request-local delivery hints, separate from rendered messages and HTTP status.

Only committed success paths mark a result. Capture is opt-in at trusted ingress;
unknown results never suppress a reply. No command text or user identity is stored.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CommandResult:
    operation: str | None = None
    committed: bool = False
    reaction_safe: bool = False


_result = ContextVar("lockbot_delivery_result", default=None)


@contextmanager
def capture_result():
    result = CommandResult()
    token = _result.set(result)
    try:
        yield result
    finally:
        _result.reset(token)


def completed_reply(reply, operation, *, reaction_safe=True):
    result = _result.get()
    if result is not None:
        result.operation = operation
        result.committed = True
        result.reaction_safe = reaction_safe
    return reply
