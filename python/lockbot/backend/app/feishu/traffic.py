"""Bounded chat-event budgets after verified actor and configured group checks."""

import threading
import time
from collections import OrderedDict, deque


class IngressBudget:
    def __init__(self, app_limit=240, actor_limit=30):
        self.app_limit, self.actor_limit = app_limit, actor_limit
        self._apps, self._actors = OrderedDict(), OrderedDict()
        self._lock = threading.Lock()

    def claim(self, app_id, actor_id, *, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            app = self._apps.setdefault(app_id, deque())
            actor_key = (app_id, actor_id)
            actor = self._actors.setdefault(actor_key, deque())
            for values in (app, actor):
                while values and values[0] <= now - 60:
                    values.popleft()
            allowed = len(app) < self.app_limit and len(actor) < self.actor_limit
            if allowed:
                app.append(now)
                actor.append(now)
            self._apps.move_to_end(app_id)
            self._actors.move_to_end(actor_key)
            while len(self._apps) > 128:
                self._apps.popitem(last=False)
            while len(self._actors) > 4096:
                self._actors.popitem(last=False)
            return allowed


ingress_budget = IngressBudget()
