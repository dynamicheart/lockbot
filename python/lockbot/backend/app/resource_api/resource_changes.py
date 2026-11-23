"""Bounded in-process signalling for committed resource snapshots."""

import asyncio
import threading
from collections import deque
from contextlib import contextmanager, suppress

MAX_RESOURCE_STREAMS = 512
MAX_RESOURCE_STREAMS_PER_IDENTITY = 2


class ResourceChanges:
    def __init__(self):
        self._lock = threading.Lock()
        self._sequence = 0
        self._changes = deque(maxlen=256)
        self._listeners = {}

    def publish(self, bot_id, *, ended=()):
        with self._lock:
            self._sequence += 1
            self._changes.append((self._sequence, bot_id, tuple(ended)))
            loops = tuple(self._listeners)
        for loop in loops:
            if not loop.is_closed():
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._signal, loop)

    def _signal(self, loop):
        with self._lock:
            listener = self._listeners.get(loop)
            if listener is not None:
                listener[1].set()
                listener[1] = asyncio.Event()

    @contextmanager
    def listen(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            listener = self._listeners.setdefault(loop, [0, asyncio.Event()])
            listener[0] += 1
        try:
            yield
        finally:
            with self._lock:
                listener[0] -= 1
                if not listener[0]:
                    self._listeners.pop(loop, None)

    def current(self):
        with self._lock:
            return self._sequence

    async def after(self, sequence, timeout):
        loop = asyncio.get_running_loop()
        with self._lock:
            event = self._listeners[loop][1]
            changed = sequence != self._sequence
        if not changed:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
        with self._lock:
            if self._changes and sequence < self._changes[0][0] - 1:
                return self._sequence, None, []
            changed = set()
            ends = []
            for number, bot_id, ended in self._changes:
                if number > sequence:
                    changed.add(bot_id)
                    ends.extend((bot_id, *item) for item in ended)
            return self._sequence, changed, ends


resource_changes = ResourceChanges()
