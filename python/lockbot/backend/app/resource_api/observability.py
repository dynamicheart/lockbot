"""Per-bot Agent command logs. Routine API traffic does not enter admin audit.

Access attempts are counted separately in resource_api.activity. A command log only
records dispatch, never proof of a successful resource mutation.
"""

from __future__ import annotations

import logging

from lockbot.backend.app.resource_api.sources import SOURCE_API
from lockbot.backend.app.resource_auth.dependencies import ResourceActor

logger = logging.getLogger(__name__)


def record_api_command(*, actor: ResourceActor, bot_id: int, command: str) -> None:
    """Best-effort bot command log with server-verified credential attribution."""
    try:
        from lockbot.backend.app.bots.router import _write_log

        _write_log(
            bot_id,
            f"[{actor.external_user_id}] {command}",
            category="command",
            source=SOURCE_API,
            credential_id=actor.binding_record_id,
        )
    except Exception:
        logger.warning("Bot %d: failed to write the API command log entry", bot_id, exc_info=True)
