"""
Rate limiter singleton.

Uses in-memory storage by default.  Set the REDIS_URL environment variable
to automatically upgrade to Redis-backed storage for distributed deployments.

Example:
    REDIS_URL=redis://localhost:6379   → Redis storage
    (unset)                            → in-memory storage
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from lockbot.backend.app.config import REDIS_URL

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=REDIS_URL if REDIS_URL else "memory://",
)
