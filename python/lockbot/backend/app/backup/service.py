"""
BOS backup service — create archive and upload to Baidu Object Storage.
"""

import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyzipper
from baidubce.auth.bce_credentials import BceCredentials
from baidubce.bce_client_configuration import BceClientConfiguration
from baidubce.services.bos.bos_client import BosClient
from sqlalchemy.orm import Session

from lockbot.backend.app.bots.encryption import decrypt, encrypt
from lockbot.backend.app.config import DATA_DIR, DATABASE_URL
from lockbot.backend.app.settings.models import SiteSetting

logger = logging.getLogger(__name__)

# Setting keys
_KEYS = [
    "backup_method",
    "backup_bos_ak",
    "backup_bos_sk",
    "backup_bos_endpoint",
    "backup_bos_bucket",
    "backup_bos_prefix",
    "backup_zip_password",
    "backup_frequency",
    "backup_auto_enabled",
    "backup_last_time",
    "backup_last_status",
    "backup_total_count",
]

ENCRYPTED_KEYS = {"backup_bos_sk"}


def get_backup_config(db: Session) -> dict:
    """Read all backup_* settings, decrypting sensitive fields."""
    rows = db.query(SiteSetting).filter(SiteSetting.key.in_(_KEYS)).all()
    config = {k: "" for k in _KEYS}
    for row in rows:
        val = row.value or ""
        if row.key in ENCRYPTED_KEYS and val:
            try:
                val = decrypt(val)
            except Exception:
                val = ""
        config[row.key] = val
    return config


def save_backup_config(db: Session, data: dict) -> None:
    """Save backup settings, encrypting sensitive fields. Skips masked values."""
    for key, value in data.items():
        if key not in _KEYS:
            continue
        # Skip masked placeholder (frontend sends *** for unchanged secrets)
        if key in ENCRYPTED_KEYS and value and value.startswith("***"):
            continue
        store_val = value
        if key in ENCRYPTED_KEYS and value:
            store_val = encrypt(value)
        row = db.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key, value=store_val, updated_at=datetime.now(timezone.utc))
            db.add(row)
        else:
            row.value = store_val
            row.updated_at = datetime.now(timezone.utc)
    db.commit()


def _update_backup_stats(db: Session, success: bool) -> None:
    """Update last backup time, status, and increment count."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    updates = {
        "backup_last_time": now,
        "backup_last_status": "success" if success else "failed",
    }
    if success:
        row = db.get(SiteSetting, "backup_total_count")
        count = int(row.value) if row and row.value else 0
        updates["backup_total_count"] = str(count + 1)

    for key, val in updates.items():
        row = db.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key, value=val, updated_at=datetime.now(timezone.utc))
            db.add(row)
        else:
            row.value = val
            row.updated_at = datetime.now(timezone.utc)
    db.commit()


def create_backup_archive(password: str | None = None) -> Path:
    """Create a zip archive of DB + bot state files. Returns path to temp zip."""
    if not DATABASE_URL.startswith("sqlite:///"):
        raise RuntimeError("Backup only supported for SQLite")

    db_path = DATABASE_URL[len("sqlite:///") :]
    tmp_dir = tempfile.mkdtemp(prefix="lockbot_backup_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_path = Path(tmp_dir) / f"lockbot_backup_{ts}.zip"

    # Safe SQLite copy
    backup_db = Path(tmp_dir) / "lockbot.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(backup_db))
    src.backup(dst)
    src.close()
    dst.close()

    # Collect bot state files
    bots_dir = Path(DATA_DIR) / "bots"
    state_files: list[tuple[Path, str]] = []
    if bots_dir.exists():
        for state_file in bots_dir.rglob("bot_state.json"):
            arcname = str(state_file.relative_to(Path(DATA_DIR)))
            state_files.append((state_file, arcname))

    # Create zip (AES encrypted if password set)
    if password:
        with pyzipper.AESZipFile(
            str(zip_path), "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(password.encode())
            zf.write(str(backup_db), "lockbot.db")
            for fpath, arcname in state_files:
                zf.write(str(fpath), arcname)
    else:
        with pyzipper.ZipFile(str(zip_path), "w", compression=pyzipper.ZIP_DEFLATED) as zf:
            zf.write(str(backup_db), "lockbot.db")
            for fpath, arcname in state_files:
                zf.write(str(fpath), arcname)

    # Cleanup temp db
    backup_db.unlink(missing_ok=True)
    return zip_path


def _build_bos_client(config: dict) -> BosClient:
    """Create BOS client from config dict."""
    return BosClient(
        BceClientConfiguration(
            credentials=BceCredentials(config["backup_bos_ak"], config["backup_bos_sk"]),
            endpoint=config["backup_bos_endpoint"],
        )
    )


def upload_to_bos(filepath: Path, config: dict) -> str:
    """Upload file to BOS. Returns the object key."""
    client = _build_bos_client(config)
    prefix = config["backup_bos_prefix"].strip("/")
    object_key = f"{prefix}/{filepath.name}" if prefix else filepath.name
    client.put_object_from_file(config["backup_bos_bucket"], object_key, str(filepath))
    return object_key


def test_bos_connection(config: dict) -> bool:
    """Test BOS connectivity by checking bucket existence."""
    client = _build_bos_client(config)
    client.list_objects(config["backup_bos_bucket"], max_keys=1)
    return True


def run_backup(db: Session) -> dict:
    """Execute full backup flow: archive → upload → update stats."""
    config = get_backup_config(db)
    zip_path = None
    try:
        password = config["backup_zip_password"] or None
        zip_path = create_backup_archive(password)
        object_key = upload_to_bos(zip_path, config)
        size = zip_path.stat().st_size
        _update_backup_stats(db, success=True)
        return {"success": True, "object_key": object_key, "size": size}
    except Exception as e:
        logger.exception("Backup failed")
        _update_backup_stats(db, success=False)
        return {"success": False, "error": str(e)}
    finally:
        if zip_path and zip_path.exists():
            zip_path.unlink(missing_ok=True)
            zip_path.parent.rmdir()
