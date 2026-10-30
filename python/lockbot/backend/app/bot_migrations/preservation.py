"""One fail-closed policy for preserving unsupported or unfinished migration evidence."""

from sqlalchemy import or_

from lockbot.backend.app.bot_migrations.models import JOURNAL_VERSION, BotMigration


def _allowed_phases(include_plans):
    terminal = ("completed", "aborted")
    return terminal if include_plans else (*terminal, "planned")


def needs_preservation(*, include_plans=True):
    """Startup excludes ordinary plans from freeze; deletion and backups preserve them."""
    return or_(
        BotMigration.schema_version.is_(None),
        BotMigration.schema_version != JOURNAL_VERSION,
        BotMigration.phase.is_(None),
        BotMigration.phase.not_in(_allowed_phases(include_plans)),
    )


def preservation_sql(*, include_plans=True):
    """Fixed-column SQLite counterpart for reading archives without attaching their ORM.

    The values here are source-code constants, never request-supplied identifiers.
    Callers must reject unknown table columns before using this predicate.
    """
    phases = ", ".join("'" + phase + "'" for phase in _allowed_phases(include_plans))
    return f"schema_version IS NULL OR schema_version != {JOURNAL_VERSION} OR phase IS NULL OR phase NOT IN ({phases})"
