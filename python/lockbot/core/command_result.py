"""Request-local delivery hints, separate from rendered messages and HTTP status.

Only committed success paths mark a result. Capture is opt-in at trusted ingress;
unknown results never suppress the caller's reply. Follow-up recipients are request-local.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CommandResult:
    operation: str | None = None
    committed: bool = False
    reaction_safe: bool = False
    affected_resources: tuple[str, ...] | None = None
    queue_delay_recipients: tuple[str, ...] = ()


_result = ContextVar("lockbot_delivery_result", default=None)


@contextmanager
def capture_result():
    result = CommandResult()
    token = _result.set(result)
    try:
        yield result
    finally:
        _result.reset(token)


def completed_reply(reply, operation, *, reaction_safe=True, affected_resources=None, queue_delay_recipients=()):
    result = _result.get()
    if result is not None:
        result.operation = operation
        result.committed = True
        result.reaction_safe = reaction_safe
        result.affected_resources = None if affected_resources is None else tuple(affected_resources)
        result.queue_delay_recipients = tuple(queue_delay_recipients)
    return reply
