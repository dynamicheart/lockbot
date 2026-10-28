"""Final API authorization and revocation barriers.

A bot guard fences revocation for that bot. A principal guard serializes the
same verified identity; chat revocation is always scoped to the current bot.
Locks are always acquired in bot-then-principal order; no path acquires a second
bot guard while holding a principal guard. Lifecycle replacement drains leases.
"""

import threading
from contextlib import contextmanager

_registry_lock = threading.Lock()
_locks = {}


def _lock_for(key):
    with _registry_lock:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


@contextmanager
def authority_guard(bot_id, *, platform=None, external_user_id=None):
    with _lock_for(("bot", bot_id)):
        if platform is None or external_user_id is None:
            yield
        else:
            with _lock_for(("principal", platform, external_user_id)):
                yield
