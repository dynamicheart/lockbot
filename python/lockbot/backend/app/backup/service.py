"""
BOS backup service — create archive and upload to Baidu Object Storage.
"""

import json
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

BACKUP_FORMAT = "lockbot-backup"
BACKUP_FORMAT_VERSION = 1


def _get_lockbot_version() -> str:
    try:
        from importlib.metadata import version

        return version("lockbot")
    except Exception:
        return "unknown"


def _build_manifest(state_files: list[tuple[Path, str]], encrypted: bool) -> dict:
    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "lockbot_version": _get_lockbot_version(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "encrypted": encrypted,
        "contains": {
            "database": True,
            "bot_states": True,
        },
        "files": {
            "database": "lockbot.db",
            "bot_states": [arcname for _, arcname in state_files],
        },
    }


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

    manifest = json.dumps(_build_manifest(state_files, encrypted=bool(password)), ensure_ascii=False, indent=2)

    # Create zip (AES encrypted if password set)
    if password:
        with pyzipper.AESZipFile(
            str(zip_path), "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(password.encode())
            zf.write(str(backup_db), "lockbot.db")
            zf.writestr("manifest.json", manifest)
            for fpath, arcname in state_files:
                zf.write(str(fpath), arcname)
    else:
        with pyzipper.ZipFile(str(zip_path), "w", compression=pyzipper.ZIP_DEFLATED) as zf:
            zf.write(str(backup_db), "lockbot.db")
            zf.writestr("manifest.json", manifest)
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


def _open_backup_zip(file_bytes: bytes, password: str | None = None):
    """Open backup zip bytes, returning a pyzipper handle.

    Raises ValueError with specific messages for password issues.
    """
    import io

    buf = io.BytesIO(file_bytes)
    try:
        zf = pyzipper.AESZipFile(buf, "r")
        if password:
            zf.setpassword(password.encode())
        # Validate by reading a file to trigger password errors
        names = zf.namelist()
        test_file = "manifest.json" if "manifest.json" in names else ("lockbot.db" if "lockbot.db" in names else None)
        if test_file:
            zf.read(test_file)
            # Reset for subsequent reads
            buf.seek(0)
            zf = pyzipper.AESZipFile(buf, "r")
            if password:
                zf.setpassword(password.encode())
        return zf
    except ValueError:
        raise
    except RuntimeError as e:
        err_msg = str(e).lower()
        if "password required" in err_msg or "encrypted" in err_msg:
            raise ValueError("BACKUP_PASSWORD_REQUIRED") from None
        if "bad password" in err_msg or "wrong password" in err_msg:
            raise ValueError("BACKUP_PASSWORD_WRONG") from None
        raise ValueError(f"Cannot open backup file: {e}") from None
    except Exception:
        # Fallback to regular zip
        buf.seek(0)
        try:
            zf = pyzipper.ZipFile(buf, "r")
            zf.namelist()
            return zf
        except Exception as e2:
            raise ValueError(f"Cannot open backup file: {e2}") from None


def _read_backup_manifest(zf) -> dict:
    """Read and parse manifest.json from open zip. Returns {} if not found."""
    try:
        return json.loads(zf.read("manifest.json"))
    except Exception:
        return {}


def _read_backup_bots(zf) -> list[dict]:
    """Read all bots from lockbot.db inside the backup zip."""
    import sqlite3
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
    try:
        tmp.write(zf.read("lockbot.db"))
        tmp.flush()
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row
        try:
            # Detect available columns
            cursor = conn.execute("PRAGMA table_info(bots)")
            columns = {row["name"] for row in cursor.fetchall()}

            select_cols = [
                "id",
                "name",
                "bot_type",
                "platform",
                "group_id",
                "webhook_url",
                "aes_key",
                "token",
                "cluster_configs",
                "config_overrides",
            ]
            # uuid may not exist in older backups
            if "uuid" in columns:
                select_cols.insert(1, "uuid")

            query = f"SELECT {', '.join(select_cols)} FROM bots WHERE is_deleted = 0 OR is_deleted IS NULL"
            rows = conn.execute(query).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if "uuid" not in d:
                    d["uuid"] = None
                result.append(d)
            return result
        finally:
            conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _decrypt_field(value: str, source_key: str | None = None) -> tuple[str, bool]:
    """Try to decrypt a Fernet-encrypted field. Returns (decrypted_or_original, ok)."""
    if not value:
        return value, True
    # Try current key first
    try:
        return decrypt(value), True
    except Exception:
        pass
    # Try source key if provided
    if source_key:
        try:
            from cryptography.fernet import Fernet as _Fernet

            f = _Fernet(source_key.encode() if isinstance(source_key, str) else source_key)
            return f.decrypt(value.encode()).decode(), True
        except Exception:
            pass
    return "", False


def restore_preview(
    file_bytes: bytes, password: str | None, source_secret_key: str | None, current_user_id: int, db
) -> dict:
    """
    Parse a backup zip and return a preview of what would be restored.

    Returns:
        {
          "manifest": {...},
          "bots": [
            {
              "uuid": str,
              "name": str,
              "bot_type": str,
              "platform": str,
              "action": "create" | "overwrite" | "skip",
              "existing_id": int | None,
              "existing_owner": str | None,
              "credentials_ok": bool,  # whether webhook/token/aes can be decrypted
            }
          ],
          "warnings": [str],
        }
    """
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bots.models import Bot

    warnings = []
    try:
        zf = _open_backup_zip(file_bytes, password)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Cannot open backup file: {e}") from None

    try:
        manifest = _read_backup_manifest(zf)

        try:
            backup_bots = _read_backup_bots(zf)
        except Exception as e:
            raise ValueError(f"Cannot read bots from backup database: {e}") from None

        # Read state files from zip
        state_files = {}
        try:
            for name_in_zip in zf.namelist():
                if name_in_zip.startswith("bots/") and name_in_zip.endswith("/bot_state.json"):
                    parts = name_in_zip.split("/")
                    if len(parts) == 3:
                        state_files[parts[1]] = zf.read(name_in_zip)
        except Exception:
            pass
    finally:
        zf.close()

    # Import validation function
    from lockbot.backend.app.bots.router import _validate_and_align_state

    result_bots = []
    for bb in backup_bots:
        uuid = bb.get("uuid")
        name = bb.get("name", "")
        bot_type = bb.get("bot_type", "")
        platform = bb.get("platform", "")
        bot_id_str = str(bb.get("id", ""))

        # Find existing bot by uuid
        existing: Bot | None = None
        existing_owner: str | None = None
        if uuid:
            existing = db.query(Bot).filter(Bot.uuid == uuid, Bot.is_deleted.is_(False)).first()
        if existing:
            user = db.query(User).filter(User.id == existing.user_id).first()
            existing_owner = user.username if user else str(existing.user_id)
            action = "overwrite"
        else:
            action = "create"

        # Check credentials decryptability
        _, wh_ok = _decrypt_field(bb.get("webhook_url", ""), source_secret_key)
        _, ak_ok = _decrypt_field(bb.get("aes_key", ""), source_secret_key)
        _, tk_ok = _decrypt_field(bb.get("token", ""), source_secret_key)
        credentials_ok = wh_ok and ak_ok and tk_ok

        # Validate state if available
        has_state = bot_id_str in state_files
        state_warnings = []
        if has_state:
            try:
                raw_state = json.loads(state_files[bot_id_str])
                # bot_state.json wraps in {"bot_state": ...}
                state_data = raw_state.get("bot_state") or raw_state.get("cluster_state") or raw_state
                cluster_configs = json.loads(bb.get("cluster_configs", "{}"))
                _, sw = _validate_and_align_state(state_data, bot_type, cluster_configs)
                state_warnings = sw
            except Exception as e:
                state_warnings = [f"State parse error: {e}"]

        result_bots.append(
            {
                "uuid": uuid,
                "name": name,
                "bot_type": bot_type,
                "platform": platform,
                "action": action,
                "existing_id": existing.id if existing else None,
                "existing_owner": existing_owner,
                "credentials_ok": credentials_ok,
                "has_state": has_state,
                "state_warnings": state_warnings,
            }
        )

    # Summary warnings (structured for frontend i18n)
    failed_bots = [b["name"] for b in result_bots if not b["credentials_ok"]]
    if failed_bots:
        if source_secret_key:
            warnings.append({"code": "credentials_key_wrong", "count": len(failed_bots)})
        else:
            warnings.append({"code": "credentials_no_key", "count": len(failed_bots)})
    elif source_secret_key:
        warnings.insert(0, {"code": "credentials_all_ok"})

    if manifest and manifest.get("format") != BACKUP_FORMAT:
        warnings.insert(0, {"code": "unrecognized_format", "format": manifest.get("format")})

    return {"manifest": manifest, "bots": result_bots, "warnings": warnings}


def restore_apply(
    file_bytes: bytes,
    password: str | None,
    source_secret_key: str | None,
    selected_uuids: list[str],
    current_user_id: int,
    db,
) -> dict:
    """
    Actually restore selected bots from a backup zip.

    For each selected bot:
      - "create": insert new Bot record (owner = current admin)
      - "overwrite": update existing Bot matched by uuid

    Credentials are re-encrypted with the current instance key.
    State files are copied to the bot's data directory.

    Returns: {"restored": int, "created": int, "overwritten": int, "errors": [str]}
    """
    import os
    import uuid as _uuid_mod

    from lockbot.backend.app.bots.models import Bot

    try:
        zf = _open_backup_zip(file_bytes, password)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Cannot open backup file: {e}") from None

    try:
        backup_bots = _read_backup_bots(zf)

        # Read state files
        state_files: dict[str, bytes] = {}
        for name_in_zip in zf.namelist():
            if name_in_zip.startswith("bots/") and name_in_zip.endswith("/bot_state.json"):
                parts = name_in_zip.split("/")
                if len(parts) == 3:
                    state_files[parts[1]] = zf.read(name_in_zip)
    finally:
        zf.close()

    created = 0
    overwritten = 0
    errors = []

    selected_set = set(selected_uuids)

    for bb in backup_bots:
        uuid = bb.get("uuid")
        if not uuid or uuid not in selected_set:
            continue

        name = bb.get("name", "")
        bot_type = bb.get("bot_type", "")
        platform = bb.get("platform", "Infoflow")
        cluster_configs = bb.get("cluster_configs", "{}")
        config_overrides = bb.get("config_overrides", "{}")
        group_id = bb.get("group_id")
        bot_id_str = str(bb.get("id", ""))

        # Decrypt credentials (re-encrypt with current key)
        wh_plain, wh_ok = _decrypt_field(bb.get("webhook_url", ""), source_secret_key)
        ak_plain, ak_ok = _decrypt_field(bb.get("aes_key", ""), source_secret_key)
        tk_plain, tk_ok = _decrypt_field(bb.get("token", ""), source_secret_key)

        webhook_enc = encrypt(wh_plain) if wh_ok and wh_plain else ""
        aes_key_enc = encrypt(ak_plain) if ak_ok and ak_plain else ""
        token_enc = encrypt(tk_plain) if tk_ok and tk_plain else ""

        # Find existing bot by uuid
        existing = db.query(Bot).filter(Bot.uuid == uuid, Bot.is_deleted.is_(False)).first()

        try:
            if existing:
                # Overwrite
                existing.name = name
                existing.bot_type = bot_type
                existing.platform = platform
                existing.group_id = group_id
                existing.cluster_configs = (
                    cluster_configs
                    if isinstance(cluster_configs, str)
                    else json.dumps(cluster_configs, ensure_ascii=False)
                )
                existing.config_overrides = (
                    config_overrides
                    if isinstance(config_overrides, str)
                    else json.dumps(config_overrides, ensure_ascii=False)
                )
                if webhook_enc:
                    existing.webhook_url = webhook_enc
                if aes_key_enc:
                    existing.aes_key = aes_key_enc
                if token_enc:
                    existing.token = token_enc
                bot_obj = existing
                overwritten += 1
            else:
                # Create new bot
                bot_obj = Bot(
                    uuid=uuid or str(_uuid_mod.uuid4()),
                    user_id=current_user_id,
                    name=name,
                    bot_type=bot_type,
                    platform=platform,
                    group_id=group_id,
                    webhook_url=webhook_enc or encrypt(""),
                    aes_key=aes_key_enc or encrypt(""),
                    token=token_enc or encrypt(""),
                    cluster_configs=cluster_configs
                    if isinstance(cluster_configs, str)
                    else json.dumps(cluster_configs, ensure_ascii=False),
                    config_overrides=config_overrides
                    if isinstance(config_overrides, str)
                    else json.dumps(config_overrides, ensure_ascii=False),
                    status="stopped",
                )
                db.add(bot_obj)
                db.flush()  # get bot_obj.id
                created += 1

            # Copy state file if available
            if bot_id_str in state_files:
                bot_data_dir = os.path.join(DATA_DIR, "bots", str(bot_obj.id))
                os.makedirs(bot_data_dir, exist_ok=True)
                state_path = os.path.join(bot_data_dir, "bot_state.json")
                with open(state_path, "wb") as f:
                    f.write(state_files[bot_id_str])

        except Exception as e:
            errors.append(f"Bot '{name}': {e}")
            continue

    db.commit()
    return {"restored": created + overwritten, "created": created, "overwritten": overwritten, "errors": errors}
