"""
Backup API — configuration + trigger endpoints.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.encryption import mask
from lockbot.backend.app.database import get_db

from .scheduler import backup_scheduler
from .service import (
    ENCRYPTED_KEYS,
    get_backup_config,
    run_backup,
    save_backup_config,
    test_bos_connection,
)

router = APIRouter(prefix="/api/admin/backup", tags=["backup"])


class BackupSettingsIn(BaseModel):
    settings: dict[str, str]


@router.get("/settings")
def get_settings(
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Return backup config with sensitive fields masked."""
    config = get_backup_config(db)
    # Mask secrets for display
    for key in ENCRYPTED_KEYS:
        if config.get(key):
            config[key] = mask(config[key])
    return config


@router.put("/settings")
def update_settings(
    body: BackupSettingsIn,
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Save backup configuration."""
    save_backup_config(db, body.settings)
    backup_scheduler.reload()
    return {"ok": True}


@router.get("/scheduler/status")
def scheduler_status(
    _admin: User = Depends(require_super_admin),
):
    """Return backup scheduler liveness status."""
    return backup_scheduler.status()


@router.post("/run")
def trigger_backup(
    request: Request,
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Trigger an immediate backup to BOS."""
    result = run_backup(db)
    write_audit_log(
        db,
        _admin,
        "admin.bos_backup",
        detail={"object_key": result.get("object_key"), "size": result.get("size")},
        ip=request.client.host if request.client else None,
        result="success" if result["success"] else "failure",
    )
    db.commit()
    return result


@router.post("/test-connection")
def test_connection(
    _admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Test BOS connectivity."""
    config = get_backup_config(db)
    try:
        test_bos_connection(config)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
