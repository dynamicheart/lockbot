"""Exclusive ownership of the bot state store.

This design assumes exactly one process serves a deployment, and several parts of it are
incorrect if that assumption breaks:

- Lock state lives in the memory of live ``BotInstance`` objects and is persisted to
  per-bot JSON files. Two processes would each believe they own that state and would
  overwrite each other's writes.
- The pending-binding store
  (:mod:`lockbot.backend.app.resource_auth.pending`) is **in-process**, so a bind code
  created by one process could never be confirmed by another. That store sits behind a thin
  interface precisely so this dependency is explicit and substitutable, but as shipped it
  is memory-only.

Checking a worker-count setting is not enough: it cannot stop a second, independently
launched process. So startup takes a real advisory file lock and holds it for the lifetime
of the process. A second process fails fast instead of silently corrupting state.
"""

from __future__ import annotations

import fcntl
import logging
import os
import stat

from lockbot.backend.app.managed_paths import ManagedPaths

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".state-store-owner.lock"

#: Kept open for the whole process lifetime. Closing the handle releases the flock, so this
#: reference must not be dropped.
_owner_handle = None
_database_handle = None


class StateStoreOwnershipError(RuntimeError):
    """Raised when another process already owns the bot state store."""


def state_lock_path(data_dir: str) -> str:
    return str(ManagedPaths(data_dir).lock_file)


def acquire_state_store_ownership(data_dir: str) -> str:
    """Take exclusive ownership of ``data_dir``, or raise.

    Idempotent within one process: calling it twice keeps the same handle rather than
    deadlocking or double-locking.
    """
    global _owner_handle

    path = state_lock_path(data_dir)
    if _owner_handle is not None:
        # Idempotent only for the SAME managed root. Requesting a *different* root after
        # ownership is acquired is a canonical-root violation (two roots could target the same
        # overridden state files), not a harmless re-call, so fail fast rather than silently
        # returning an unrelated existing handle. Compare resolved real paths so a symlink alias
        # of the same directory is correctly treated as the same root.
        if os.path.realpath(_owner_handle.name) != os.path.realpath(path):
            raise StateStoreOwnershipError(
                f"This process already owns the bot state store at {_owner_handle.name!r}; "
                f"refusing to also claim a different managed root at {path!r}. The managed state "
                f"root is fixed for the lifetime of the process."
            )
        return _owner_handle.name

    os.makedirs(data_dir, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 — intentionally kept open for the process lifetime
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.seek(0)
        holder = handle.read().strip() or "unknown"
        handle.close()
        raise StateStoreOwnershipError(
            f"Another process already owns the bot state store at {data_dir} "
            f"(lock holder: {holder}). lockbot must run as a single process: bot lock state "
            f"and the pending-binding store are both in-process. Stop the other process, or "
            f"point this one at a different DATA_DIR."
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    _owner_handle = handle
    logger.info("Acquired exclusive ownership of the bot state store at %s", data_dir)
    return path


def acquire_database_ownership(database_url: str) -> str | None:
    """A separate root must not let another deployment serve the same SQLite DB.

    The state-root lock remains primary. This sibling lock covers an explicitly placed
    database shared by otherwise different roots; memory databases are test-local.
    """
    global _database_handle
    from lockbot.backend.app.sqlite_paths import sqlite_database_file

    database = sqlite_database_file(database_url)
    if database is None:
        return None
    if database.exists() and database.stat().st_nlink > 1:
        raise StateStoreOwnershipError("Hard-linked database cannot establish deployment ownership")
    path = database.with_name(database.name + ".lockbot-owner.lock")
    if path.is_symlink():
        raise StateStoreOwnershipError("Database ownership lock must not be a symlink")
    if _database_handle is not None:
        if _database_handle.name != str(path):
            raise StateStoreOwnershipError("This process already owns a different deployment database")
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        handle.close()
        raise StateStoreOwnershipError("Database ownership lock must be a regular, non-hard-linked file")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise StateStoreOwnershipError(
            "Another process owns this database, possibly under a different DATA_DIR"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    _database_handle = handle
    return str(path)


def release_state_store_ownership() -> None:
    """Release ownership. Normally unnecessary — the process exit does it — but tests need it."""
    global _owner_handle, _database_handle
    for handle in (_database_handle, _owner_handle):
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
    _database_handle = None
    _owner_handle = None


def owns_state_store() -> bool:
    return _owner_handle is not None
