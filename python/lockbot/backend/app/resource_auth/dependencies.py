"""Credential authentication dependency for the agent-facing API.

Strict separation of principal types, in both directions:

- The **platform management JWT** (``auth/dependencies.get_current_user``) authenticates a
  human operating the web UI. It is rejected here.
- The **legacy per-bot service key** (``lbk_``) keeps the pre-existing ``locked-users`` and
  ``opkick`` routes. It is rejected here.
- The **personal resource credential** (``lbr_``) acts as a verified IM user on exactly one
  bot. It is rejected on every management route and on the legacy routes, because those
  routes authenticate through their own dependencies and never consult this one.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.http_errors import ResourceAuthenticationError
from lockbot.backend.app.resource_auth.models import ResourceBinding


@dataclass(frozen=True)
class ResourceActor:
    """The verified identity behind an authenticated API request."""

    platform: str
    external_user_id: str
    bot_id: int
    #: Primary key of the durable binding row, for log and audit attribution.
    binding_record_id: int
    binding_public_id: str

    def as_payload(self) -> dict:
        """Platform-neutral actor block for an HTTP response body."""
        return {"platform": self.platform, "external_user_id": self.external_user_id}


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ResourceAuthenticationError("Missing credential", "credential_missing")
    return header[7:].strip()


def require_resource_actor(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> ResourceActor:
    """Authenticate a personal credential for ``bot_id`` and return its verified identity.

    The caller cannot influence who it acts as: identity comes entirely from the durable
    row written at chat-confirmation time.
    """
    token = _bearer_token(request)

    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot not found")

    row: ResourceBinding | None = service.authenticate(db, bot_id=bot_id, bot_uuid=bot.uuid, token=token)
    if row is None:
        # Preserve the released 401/detail text. A valid key for a different bot
        # gets an additive recovery code; never disclose that bot without its key.
        valid_elsewhere = service.authenticate_for_any_bot(db, token=token)
        code = "wrong_bot" if valid_elsewhere is not None else "credential_invalid"
        raise ResourceAuthenticationError("Invalid credential", code)

    service.touch_last_used(db, row)
    db.commit()

    actor = ResourceActor(
        platform=row.platform,
        external_user_id=row.external_user_id,
        bot_id=bot_id,
        binding_record_id=row.id,
        binding_public_id=row.public_id,
    )
    request.state.resource_actor = actor
    request.state.resource_activity_engine = db.get_bind()
    if not bot.resource_auth_enabled:
        raise _agent_access_disabled()
    return actor


def _agent_access_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "agent_access_disabled", "message": "Agent access is disabled for this bot."},
    )


def reverify_actor(db: Session, request: Request, actor: ResourceActor) -> bool:
    """Re-check, INSIDE the admission lease, that the credential behind ``actor`` is still valid.

    ``require_resource_actor`` runs as a dependency BEFORE the route takes the admission lease,
    in a separate transaction. A restore/delete/revoke that commits in that window would let a
    now-stale actor execute. Call this once the lease is held: it ends the current read
    transaction, re-reads the bot and binding fresh, and re-runs the same authentication
    predicate. Returns False if the credential is no longer valid (revoked, bot deleted, uuid
    changed), so the caller can refuse with a 401 rather than acting on stale authorization.
    """
    token = _bearer_token(request)
    db.rollback()
    bot = db.get(Bot, actor.bot_id)
    if not bot or bot.is_deleted:
        return False
    valid = service.authenticate(db, bot_id=actor.bot_id, bot_uuid=bot.uuid, token=token) is not None
    if valid and not bot.resource_auth_enabled:
        raise _agent_access_disabled()
    return valid
