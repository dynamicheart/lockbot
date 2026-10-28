"""Read-only inventory of legacy state paths before managed startup.

Run with ``python -m lockbot.backend.app.state_migration`` while stopped. This command
never relocates state or rewrites overrides: operators choose and verify a source before
copying it into the canonical deployment. Conflicting files are never guessed away.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lockbot.backend.app.managed_paths import ManagedPaths, get_managed_paths, protected_override_keys


class StateMigrationRequired(RuntimeError):
    """Legacy occupancy exists outside the managed file, or cannot be inspected safely."""


def _fingerprint(path: Path) -> dict:
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            return {"path": str(path), "error": "symlink requires operator inspection"}
        if not path.exists():
            return {"path": str(path), "exists": False}
        if not path.is_file():
            return {"path": str(path), "error": "not a regular state file"}
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return {"path": str(path), "exists": True, "sha256": digest.hexdigest()}
    except OSError:
        return {"path": str(path), "error": "state file is not readable"}


def inventory_bot(bot, paths: ManagedPaths) -> dict:
    """Describe actual historical core/router candidates without changing any file."""
    try:
        overrides = json.loads(bot.config_overrides or "{}")
        if not isinstance(overrides, dict):
            raise ValueError
    except (ValueError, TypeError):
        return {"bot_id": bot.id, "requires_migration": True, "error": "invalid config_overrides"}
    canonical = paths.state_file(bot.id)
    destination = _fingerprint(canonical)
    # Old backend defaults and core defaults disagreed when DATA_DIR was unset. With an
    # override, core treated DATA_DIR as the bots directory; router inserted another bots/.
    candidates = set()
    old_root = overrides.get("DATA_DIR")
    if old_root:
        if not isinstance(old_root, str):
            return {"bot_id": bot.id, "requires_migration": True, "error": "invalid legacy DATA_DIR"}
        from lockbot.backend.app.config import LEGACY_WORKING_DIR

        base = LEGACY_WORKING_DIR / old_root
        candidates.update((base / str(bot.id) / "bot_state.json", base / "bots" / str(bot.id) / "bot_state.json"))
    else:
        from lockbot.backend.app.config import LEGACY_CORE_ROOT

        candidates.add(LEGACY_CORE_ROOT / "bots" / str(bot.id) / "bot_state.json")
    # STATE_FILENAME was ignored by core but can still identify an operator-managed file.
    chosen = overrides.get("STATE_FILENAME")
    if isinstance(chosen, str) and chosen:
        from lockbot.backend.app.config import LEGACY_WORKING_DIR

        candidates.add(LEGACY_WORKING_DIR / chosen)
    # The deprecated filename can still be migrated by core's loader. Inventory it as
    # well, otherwise changing roots can silently strand that occupancy.
    for state_file in tuple(candidates) + (canonical,):
        candidates.add(state_file.with_name("cluster_status.json"))
    sources = [_fingerprint(p) for p in sorted(candidates) if p.absolute() != canonical]
    unresolved = bool(destination.get("error")) or any(
        source.get("error") or source.get("exists") for source in sources
    )
    return {
        "bot_id": bot.id,
        "ignored_keys": protected_override_keys(overrides),
        "canonical": destination,
        "legacy_candidates": sources,
        "requires_migration": unresolved,
    }


def assert_bot_state_ready(bot, paths: ManagedPaths) -> None:
    report = inventory_bot(bot, paths)
    if report["requires_migration"]:
        raise StateMigrationRequired(
            f"Bot {bot.id} has unresolved legacy state. Stop the deployment and run "
            "python -m lockbot.backend.app.state_migration; verify/copy the chosen state "
            "and archive conflicting sources before starting. No files were moved."
        )


def inventory(db, paths: ManagedPaths) -> dict:
    from lockbot.backend.app.bots.models import Bot

    reports = [inventory_bot(bot, paths) for bot in db.query(Bot).filter(Bot.is_deleted.is_(False)).order_by(Bot.id)]
    return {
        "root": str(paths.root),
        "bots": reports,
        "requires_migration": any(r["requires_migration"] for r in reports),
    }


def assert_state_store_ready(db, paths: ManagedPaths) -> None:
    report = inventory(db, paths)
    blocked = [str(row["bot_id"]) for row in report["bots"] if row["requires_migration"]]
    if blocked:
        raise StateMigrationRequired(
            "Unresolved legacy state for bots " + ", ".join(blocked) + ". "
            "Run python -m lockbot.backend.app.state_migration while stopped; no files were moved."
        )


def main() -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from lockbot.backend.app.config import DATABASE_URL
    from lockbot.backend.app.sqlite_paths import sqlite_database_file

    database = sqlite_database_file(DATABASE_URL)
    if database is None:
        raise SystemExit("Dry-run inventory requires an existing SQLite deployment database")
    if not database.is_file():
        raise SystemExit("Deployment database does not exist; inventory will not create it")
    # Explicit read-only URI prevents even an accidental schema/row write or empty DB creation.
    import sqlite3

    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(database.as_uri() + "?mode=ro", uri=True))
    try:
        with Session(engine) as db:
            report = inventory(db, get_managed_paths())
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 2 if report["requires_migration"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
