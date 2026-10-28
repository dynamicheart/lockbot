"""Binding enrollment routes.

Two routes only, both deliberately minimal:

- ``POST /api/resource-auth/bindings`` is **unauthenticated** and writes nothing durable.
  It hands back an inert credential plus a public bind code. It keeps an address-based
  rate limit, because there is no credential to meter against yet.
- ``GET /api/resource-auth/bindings/{binding_id}`` reports status only. It never discloses
  the bind code or the credential.

Confirmation does not happen here. It happens in chat, through a signature-verified
callback, which is the only place a trustworthy identity exists.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.pending import PendingCapExceeded

router = APIRouter(prefix="/api/resource-auth", tags=["Public API"])


class BindingCreate(BaseModel):
    """Creation request.

    ``extra="forbid"`` is load-bearing: it is what makes an attempt to supply
    ``platform``, ``external_user_id`` or ``actor`` a 422 rather than a silently ignored
    field.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=64, description="Free-text note shown in chat listings")


class BindingCreated(BaseModel):
    binding_id: str
    credential: str = Field(description="Shown exactly once. Inert until confirmed in chat.")
    bind_code: str = Field(description="Give this to a human to send as `bind <code>` in the bot's group.")
    bind_expires_at: datetime


class BindingStatus(BaseModel):
    binding_id: str
    status: str = Field(description="pending | confirmed | revoked | expired")


@router.post("/bindings", response_model=BindingCreated, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def create_binding(request: Request, body: BindingCreate | None = None) -> BindingCreated:
    """Issue an inert credential and a single-use bind code. Writes nothing to the database."""
    source = request.client.host if request.client else ""
    try:
        issued = service.create_binding(source=source, label=body.label if body else None)
    except PendingCapExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many outstanding unconfirmed bindings ({exc.scope})",
            headers={"Retry-After": "600"},
        ) from None
    return BindingCreated(**issued)


@router.get("/bindings/{binding_id}", response_model=BindingStatus)
@limiter.limit("60/minute")
def get_binding_status(binding_id: str, request: Request, db: Session = Depends(get_db)) -> BindingStatus:
    """Report whether a binding is pending, confirmed, revoked, or gone."""
    return BindingStatus(binding_id=binding_id, status=service.binding_status(db, binding_id))
