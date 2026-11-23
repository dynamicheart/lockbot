"""Request-local delivery hints, separate from rendered messages and HTTP status.

Only committed success paths mark a result. Capture is opt-in at trusted ingress;
unknown results never suppress the caller's reply. Follow-up recipients and optional
detached state observations stay within the request.
"""

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class CommandResult:
    operation: str | None = None
    committed: bool = False
    reaction_safe: bool = False
    affected_resources: tuple[str, ...] | None = None
    capture_observation: bool = False
    observation: tuple[datetime, dict] | None = field(default=None, repr=False)
    queue_delay_recipients: tuple[str, ...] = ()


_result = ContextVar("lockbot_delivery_result", default=None)


@contextmanager
def capture_result(*, observe_state=False):
    result = CommandResult(capture_observation=observe_state)
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


def observe_locked_state(state):
    """Opt-in request-local copy, called before the allocator releases its lock."""
    result = _result.get()
    if result is not None and result.capture_observation:
        result.observation = datetime.now(timezone.utc), copy.deepcopy(state)
