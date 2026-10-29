"""Immutable managed deployment paths; isolated core IO keeps its existing layout."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MANAGED_OVERRIDE_KEYS = frozenset(
    {
        "DATA_DIR",
        "STATE_FILENAME",
        "CONFIG_FILENAME",
        "LOG_FILENAME",
        "LOG_DIR",
        "BOT_ID",
        "BOT_TYPE",
        "PLATFORM",
        "_MANAGED_BOT_DIR",
        "_IDENTITY_REVISION",
        "_LIFECYCLE_BOT_UUID",
        "_AUTHORIZED_GROUP_IDS",
        "_BOT_VIEW_LINK",
    }
)


class ManagedPathError(ValueError):
    """An unmanaged or aliased child path would escape deployment ownership."""


def protected_override_keys(overrides: dict) -> list[str]:
    return sorted(key for key in overrides if key in MANAGED_OVERRIDE_KEYS or key.startswith(("_MANAGED_", "_FEISHU_")))


def sanitize_managed_overrides(overrides: dict, *, bot_id=None) -> dict:
    """Ignore legacy/restored control keys, reporting names only (never their values)."""
    blocked = protected_override_keys(overrides)
    if blocked:
        logger.warning("Ignoring managed control overrides for bot %s: %s", bot_id, ", ".join(blocked))
    return {key: value for key, value in overrides.items() if key not in blocked}


@dataclass(frozen=True)
class ManagedPaths:
    root: Path

    def __init__(self, root: str | os.PathLike):
        object.__setattr__(self, "root", Path(root).expanduser().resolve())

    def checked(self, *parts: str) -> Path:
        """Reject child symlinks, even in-root ones, so bot IDs cannot alias each other.

        The deployment root itself may be supplied via a symlink at construction. After
        resolution, replacing that root or its parents must not redirect future IO.
        The root is operator-owned, not writable by untrusted local users.
        """
        if self.root.resolve() != self.root:
            raise ManagedPathError("Managed deployment root was redirected")
        path = self.root
        for part in parts:
            if not part or part in (".", "..") or Path(part).name != part or "\\" in part:
                raise ManagedPathError("Invalid managed path component")
            path = path / part
            if path.is_symlink():
                raise ManagedPathError("Symlink in managed deployment path")
            if path.exists() and path.is_file() and path.stat().st_nlink > 1:
                raise ManagedPathError("Hard-linked managed file")
        if not path.resolve().is_relative_to(self.root):
            raise ManagedPathError("Managed path escaped deployment root")
        return path

    @property
    def bots_root(self) -> Path:
        return self.checked("bots")

    @property
    def lock_file(self) -> Path:
        return self.checked(".state-store-owner.lock")

    def bot_dir(self, bot_id: int) -> Path:
        if isinstance(bot_id, bool) or not str(bot_id).isascii() or not str(bot_id).isdigit() or int(bot_id) <= 0:
            raise ManagedPathError("Managed bot ID must be a positive integer")
        return self.checked("bots", str(int(bot_id)))

    def bot_file(self, bot_id: int, name: str) -> Path:
        directory = self.bot_dir(bot_id)
        return self.checked("bots", directory.name, name)

    def state_file(self, bot_id: int) -> Path:
        return self.bot_file(bot_id, "bot_state.json")

    def core_directory(self, bot_id: int) -> str:
        """Recheck all core IO targets on each managed core call, not just at start."""
        directory = self.bot_dir(bot_id)
        for name in ("bot_state.json", "cluster_status.json", "bot.log"):
            self.bot_file(bot_id, name)
        return str(directory)


def get_managed_paths() -> ManagedPaths:
    # One deployment-owned object. Tests substitute the deployment, not per-bot paths.
    from lockbot.backend.app.config import MANAGED_PATHS

    return MANAGED_PATHS
