"""Short-lived identity statements for administrator-registered relying services."""

import hashlib
import json
import os
import re
from datetime import timezone
from typing import Literal

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lockbot.backend.app.database import get_db
from lockbot.backend.app.private_files import read_private_file
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_api.v2.binding import api_error, require_principal, router
from lockbot.backend.app.resource_auth.service import _utcnow

CONFIG_ENV = "AGENT_IDENTITY_TICKETS_FILE"
TICKET_TYPE = "lockbot-identity+jwt"
AUDIENCE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]{0,127}$"
TTL = 120


class IdentityTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: str = Field(pattern=AUDIENCE_PATTERN)


class IdentityTicketReply(BaseModel):
    token_type: Literal["Bearer"] = "Bearer"
    ticket: str
    expires_at: int


def _unique_object(fields):
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("Duplicate identity-ticket configuration field")
    return result


def _signing_key(audience: str) -> Ed25519PrivateKey:
    path = os.environ.get(CONFIG_ENV)
    if not path:
        raise api_error("identity_tickets_disabled", 404)
    try:
        raw, config_file = read_private_file(path, 16384)
        config = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(config, dict) or set(config) != {"version", "key_file", "audiences"}:
            raise ValueError("Invalid identity-ticket configuration")
        audiences = config["audiences"]
        if (
            type(config["version"]) is not int
            or config["version"] != 1
            or not isinstance(audiences, list)
            or not 1 <= len(audiences) <= 32
            or not all(isinstance(value, str) and re.fullmatch(AUDIENCE_PATTERN, value) for value in audiences)
            or len(set(audiences)) != len(audiences)
        ):
            raise ValueError("Invalid audience registry")
        if audience not in audiences:
            raise api_error("identity_ticket_audience_denied", 403)
        pem, key_file = read_private_file(config["key_file"], 4096)
        if key_file == config_file:
            raise ValueError("Key must be in a separate file")
        key = load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Identity tickets require an Ed25519 key")
        return key
    except (OSError, ValueError, TypeError, RecursionError, UnsupportedAlgorithm):
        raise api_error("identity_ticket_unavailable", 503) from None


@router.post("/identity-tickets", response_model=IdentityTicketReply)
@limiter.limit("30/minute")
def issue_identity_ticket(
    request: Request, response: Response, body: IdentityTicketRequest, db: Session = Depends(get_db)
):
    response.headers["Cache-Control"] = "no-store"
    principal = require_principal(request, db)
    key = _signing_key(body.audience)
    now = int(_utcnow().timestamp())
    expiry = now + TTL
    if principal.credential_expires_at is not None:
        expires_at = int(principal.credential_expires_at.replace(tzinfo=timezone.utc).timestamp())
        expiry = min(expiry, expires_at)
    if expiry <= now:
        raise api_error("credential_invalid", 401)
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ticket = jwt.encode(
        {
            "iss": ensure_instance_id(db),
            "sub": principal.public_id,
            "aud": body.audience,
            "iat": now,
            "exp": expiry,
        },
        key,
        algorithm="EdDSA",
        headers={"typ": TICKET_TYPE, "kid": hashlib.sha256(public_key).hexdigest()[:16]},
    )
    return IdentityTicketReply(ticket=ticket, expires_at=expiry)
