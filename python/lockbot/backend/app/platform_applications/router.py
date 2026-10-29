"""Super-administrator discovery for versioned provider credentials.

Provider-specific creation and credential rotation deliberately retain their
existing validators, network verification and receiver lifecycle controls.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.feishu.models import FeishuApplication
from lockbot.backend.app.platform_applications.service import PlatformApplication, credentials_for
from lockbot.backend.app.rate_limit import limiter

router = APIRouter(prefix="/api/admin/platform-applications", tags=["Messaging applications"])


class Rename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=128)
    expected_generation: int = Field(ge=1)


def _current(db, user):
    user_id, version = user.id, user.effective_token_version
    db.rollback()
    user = db.get(User, user_id)
    if user is None or user.role != "super_admin" or user.effective_token_version != version:
        raise HTTPException(403, "Current super administrator authority required")
    return user


def _source(db, row):
    if row.provider == "feishu":
        return db.query(FeishuApplication).filter_by(app_id=row.stable_identity).one_or_none()
    if row.provider == "infoflow":
        return db.query(Bot).filter_by(uuid=row.stable_identity).one_or_none()
    raise HTTPException(409, "Unsupported messaging provider")


def _record(db, application_id):
    row = db.get(PlatformApplication, application_id)
    if row is None:
        raise HTTPException(404, "Messaging application not found")
    return row


def _audit(db, user, request, row, action):
    # Commit is mandatory before reveal. Never serialize model state, payload or form values.
    db.add(
        AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role=user.role,
            action=action,
            target_type="platform_app",
            target_id=row.id,
            target_name=f"#{row.id}",
            detail=json.dumps(
                {"provider": row.provider, "schema_version": row.schema_version, "generation": row.generation}
            ),
            ip_address=request.client.host if request.client else None,
            result="success",
        )
    )
    db.flush()


def _metadata(db, row):
    source = _source(db, row)
    return {
        "id": row.id,
        "provider": row.provider,
        "stable_identity": row.stable_identity,
        "name": row.name,
        "schema_version": row.schema_version,
        "generation": row.generation,
        "referenced": source is not None,
        "provider_management_path": f"/api/feishu/applications/{source.id}"
        if source is not None and row.provider == "feishu"
        else f"/api/bots/{source.id}"
        if source is not None
        else None,
    }


@router.get("")
def applications(response: Response, user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    _current(db, user)
    rows = db.query(PlatformApplication).order_by(PlatformApplication.id).limit(1000).all()
    return {"items": [_metadata(db, row) for row in rows], "limit": 1000}


@router.patch("/{application_id}")
def rename(
    application_id: int,
    body: Rename,
    request: Request,
    response: Response,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    user = _current(db, user)
    row = _record(db, application_id)
    if row.generation != body.expected_generation:
        raise HTTPException(409, "Messaging application changed; refresh before editing")
    source = _source(db, row)
    if source is None:
        raise HTTPException(409, "Messaging application source is unavailable")
    credentials_for(source)  # Unsupported/mismatched layouts cannot be concealed by a metadata edit.
    if row.provider == "feishu":
        source.name = body.name
    else:
        row.name = body.name
        row.name_from_source = False
    _audit(db, user, request, row, "platform_application.rename")
    db.commit()
    return _metadata(db, row)


@router.post("/{application_id}/reveal")
@limiter.limit("6/minute")
def reveal(
    application_id: int,
    request: Request,
    response: Response,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    user = _current(db, user)
    row = _record(db, application_id)
    source = _source(db, row)
    if source is None:
        raise HTTPException(409, "Messaging application source is unavailable")
    values = credentials_for(source)
    generation, version, user_id = row.generation, user.effective_token_version, user.id
    _audit(db, user, request, row, "platform_application.reveal")
    db.commit()
    db.expire_all()
    current = db.get(User, user_id)
    row = _record(db, application_id)
    if current is None or current.role != "super_admin" or current.effective_token_version != version:
        raise HTTPException(403, "Current super administrator authority required")
    if row.generation != generation or credentials_for(_source(db, row)) != values:
        raise HTTPException(409, "Messaging application changed during disclosure")
    return {"provider": row.provider, "schema_version": row.schema_version, "credentials": values}


@router.delete("/{application_id}", status_code=204)
def delete_orphan(
    application_id: int, request: Request, user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    user = _current(db, user)
    row = _record(db, application_id)
    if _source(db, row) is not None:
        raise HTTPException(409, "Application is referenced; use provider-specific lifecycle management")
    _audit(db, user, request, row, "platform_application.delete")
    db.delete(row)
    db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
