"""No auto-resume: every incomplete cutover is fenced before normal autostart."""

from lockbot.backend.app.bot_migrations.models import BotMigration
from lockbot.backend.app.bot_migrations.preservation import needs_preservation


def restore_fences(factory, manager):
    with factory() as db:
        rows = db.query(BotMigration).filter(needs_preservation(include_plans=False)).all()

        # Unknown future/corrupt phases also stay unavailable; never select an old channel.
        records = [(row.bot_id, row.id) for row in rows]
        if len({bot_id for bot_id, _ in records}) != len(records):
            raise RuntimeError("Conflicting durable migrations require administrator recovery")
        manager.restore_migration_fences(records)
