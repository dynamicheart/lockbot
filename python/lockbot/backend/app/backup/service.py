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
from lockbot.backend.app.config import DATABASE_URL
from lockbot.backend.app.managed_paths import get_managed_paths, sanitize_managed_overrides
from lockbot.backend.app.settings.models import SiteSetting
from lockbot.backend.app.sqlite_paths import sqlite_database_file
from lockbot.backend.app.state_migration import assert_bot_state_ready

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
    db_path = sqlite_database_file(DATABASE_URL)
    if db_path is None or not db_path.is_file():
        raise RuntimeError("Backup requires an existing file-backed SQLite database")
    tmp_dir = tempfile.mkdtemp(prefix="lockbot_backup_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_path = Path(tmp_dir) / f"lockbot_backup_{ts}.zip"

    # Safe SQLite copy
    backup_db = Path(tmp_dir) / "lockbot.db"
    src = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(backup_db))
    src.backup(dst)
    src.close()
    dst.close()

    # Collect bot state files
    paths = get_managed_paths()
    bots_dir = paths.bots_root
    state_files: list[tuple[Path, str]] = []
    if bots_dir.exists():
        for directory in bots_dir.iterdir():
            if not directory.name.isascii() or not directory.name.isdigit() or int(directory.name) <= 0:
                continue
            state_file = paths.state_file(int(directory.name))
            if state_file.exists():
                arcname = str(state_file.relative_to(paths.root))
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
            # Agent options are optional in older archives; absence must not reset existing preferences.
            select_cols.extend(
                key for key in ("resource_auth_enabled", "resource_api_notify_enabled") if key in columns
            )
            if "utilization_config" in columns:
                select_cols.append("utilization_config")
            # uuid may not exist in older backups
            if "uuid" in columns:
                select_cols.insert(1, "uuid")

            query = f"SELECT {', '.join(select_cols)} FROM bots WHERE is_deleted = 0 OR is_deleted IS NULL"
            rows = conn.execute(query).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if (d.get("platform") or "").lower() == "feishu":
                    from lockbot.backend.app.feishu.backup import read_application_metadata

                    d["_feishu_application"] = read_application_metadata(conn, d["id"])
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


def _mark_recovery_required(db, bot_id: int) -> None:
    """Persist a bot into the ``recovery_required`` isolation status that every start entry
    refuses. Used when a restore cannot prove the committed config and the installed state file
    are consistent (state install failed, or the commit result is unknown). Best-effort and
    idempotent: if even this write fails, the bot's prior committed config/state is unchanged and
    the next restore can retry."""
    import contextlib

    from lockbot.backend.app.bots.models import Bot

    with contextlib.suppress(Exception):
        db.rollback()
        row = db.get(Bot, bot_id)
        if row is not None and not row.is_deleted:
            row.status = "recovery_required"
            row.pid = None
            db.commit()


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
    import contextlib
    import os
    import uuid as _uuid_mod

    from sqlalchemy.exc import IntegrityError

    from lockbot.backend.app.bots import manager as bot_mgr_mod
    from lockbot.backend.app.bots.manager import BotNotRunning, InstanceRetiring, MayHaveExecuted
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot

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
        if isinstance(config_overrides, str):
            config_overrides = json.loads(config_overrides)
        if not isinstance(config_overrides, dict):
            raise ValueError("Backup config_overrides must be an object")
        raw_config_overrides = config_overrides
        config_overrides = sanitize_managed_overrides(config_overrides, bot_id=bb.get("id"))
        group_id = bb.get("group_id")
        bot_id_str = str(bb.get("id", ""))

        # Decrypt credentials (re-encrypt with current key)
        wh_plain, wh_ok = _decrypt_field(bb.get("webhook_url", ""), source_secret_key)
        ak_plain, ak_ok = _decrypt_field(bb.get("aes_key", ""), source_secret_key)
        tk_plain, tk_ok = _decrypt_field(bb.get("token", ""), source_secret_key)

        webhook_enc = encrypt(wh_plain) if wh_ok and wh_plain else ""
        aes_key_enc = encrypt(ak_plain) if ak_ok and ak_plain else ""
        token_enc = encrypt(tk_plain) if tk_ok and tk_plain else ""

        # Only restore settings actually present in the archive. In particular, an old
        # archive must not turn a deliberately disabled Agent setting back on.
        agent_options = {}
        invalid_options = False
        for key in ("resource_auth_enabled", "resource_api_notify_enabled"):
            if key not in bb:
                continue
            value = bb[key]
            if type(value) not in (bool, int) or value not in (0, 1):
                errors.append(f"Bot '{name}': invalid archived Agent setting {key}; skipped")
                invalid_options = True
                break
            agent_options[key] = bool(value)
        if invalid_options:
            continue

        if "utilization_config" in bb:
            from lockbot.backend.app.bots.datasource import UtilizationConfig, serialized_datasource

            try:
                raw_source = bb["utilization_config"]
                source = UtilizationConfig.model_validate_json(raw_source) if raw_source else None
                agent_options["utilization_config"] = serialized_datasource(source)
            except (ValueError, TypeError):
                errors.append(f"Bot '{name}': invalid archived utilization configuration; skipped")
                continue

        # Find existing bot by uuid
        existing = db.query(Bot).filter(Bot.uuid == uuid, Bot.is_deleted.is_(False)).first()

        cc_str = (
            cluster_configs if isinstance(cluster_configs, str) else json.dumps(cluster_configs, ensure_ascii=False)
        )
        co_str = (
            config_overrides if isinstance(config_overrides, str) else json.dumps(config_overrides, ensure_ascii=False)
        )

        def _write_state_file(target_id, bot_id_str=bot_id_str):
            """Atomically write the archived state file (temp + os.replace), if present."""
            if bot_id_str not in state_files:
                return
            state_path = get_managed_paths().state_file(target_id)
            bot_data_dir = str(state_path.parent)
            os.makedirs(bot_data_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=bot_data_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(state_files[bot_id_str])
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, state_path)
                directory_fd = os.open(bot_data_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                if state_path.read_bytes() != state_files[bot_id_str]:
                    raise OSError("Installed state verification failed")
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise

        try:
            from types import SimpleNamespace

            # Do not erase the only clue to old occupancy before checking it. Existing
            # deployment overrides and the incoming archive both need this preflight.
            paths = get_managed_paths()
            if bot_id_str in state_files:
                decoded = json.loads(state_files[bot_id_str])
                if not isinstance(decoded, dict):
                    raise ValueError("Archived state must be a JSON object")
                raw_state = next(
                    (decoded[k] for k in ("bot_state", "cluster_state", "cluster_status") if k in decoded), None
                )
                if not isinstance(raw_state, dict):
                    raise ValueError("Archived state is missing its state object")
                for node in raw_state.values():
                    entries = node if bot_type in {"DEVICE", "QUEUE_DEVICE"} else [node]
                    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                        raise ValueError("Archived resource state does not match bot type")
                    for entry in entries:
                        for field in ("current_users", "booking_list"):
                            if not isinstance(entry.get(field, []), list):
                                raise ValueError("Archived holder/booking list is invalid")
                            for holder in entry.get(field, []):
                                if not isinstance(holder, dict) or not isinstance(holder.get("user_id"), str):
                                    raise ValueError("Archived holder identity is invalid")
            if existing:
                assert_bot_state_ready(existing, paths)
            assert_bot_state_ready(
                SimpleNamespace(id=bb.get("id"), config_overrides=json.dumps(raw_config_overrides)), paths
            )
            if existing:
                # Overwrite under the transition owner so restore no longer bypasses the lifecycle
                # serialization: a concurrent start/stop/restart/delete on another thread fences
                # this bot (skipped, no durable effect); the live instance is drained first; and
                # old credentials are revoked (identity discontinuity, AC-9). Row + state commit
                # under the owner, then the state file is written atomically.
                try:
                    existing_id = existing.id
                    with bot_mgr_mod.bot_manager.transition(existing_id):
                        db.rollback()
                        existing = db.get(Bot, existing_id)
                        if existing is None or existing.is_deleted or existing.uuid != uuid:
                            raise ValueError("Restore target identity changed")
                        if bot_id_str not in state_files and (
                            existing.status == "recovery_required"
                            or existing.bot_type != bot_type
                            or json.loads(existing.cluster_configs) != json.loads(cc_str)
                            or json.loads(existing.config_overrides or "{}") != json.loads(co_str)
                        ):
                            raise ValueError(
                                "Restore requires an archived state file for recovery or resource configuration changes"
                            )
                        try:
                            bot_mgr_mod.bot_manager.stop_bot(existing.id)
                        except MayHaveExecuted:
                            raise  # a worker may still mutate state; do not overwrite
                        except BotNotRunning:
                            pass  # no instance exists; other cleanup failures must propagate
                        # A webhook may have committed metadata while the old instance drained.
                        db.rollback()
                        existing = db.get(Bot, existing_id)
                        if existing is None or existing.is_deleted or existing.uuid != uuid:
                            raise ValueError("Restore target identity changed during drain")
                        revoke_bindings_for_bot(db, existing.id)
                        existing.identity_revision += 1
                        existing.binding_accept_after = datetime.utcnow()
                        existing.observed_group_ids = None
                        existing.last_user_id = None
                        existing.last_request_at = None
                        for key, value in agent_options.items():
                            setattr(existing, key, value)
                        existing.name = name
                        existing.bot_type = bot_type
                        existing.platform = platform
                        existing.group_id = group_id
                        existing.cluster_configs = cc_str
                        existing.config_overrides = co_str
                        if webhook_enc:
                            existing.webhook_url = webhook_enc
                        if aes_key_enc:
                            existing.aes_key = aes_key_enc
                        if token_enc:
                            existing.token = token_enc
                        existing.status = "recovery_required"
                        existing.pid = None
                        from lockbot.backend.app.feishu.backup import restore_route

                        restore_route(db, existing, bb, source_secret_key)
                        # Commit the new config, then install the archived state atomically. If
                        # the state cannot be installed after the config commit (or the commit
                        # result is unknown), the row and its saved state may be inconsistent — a
                        # start would load the wrong occupancy. Fence the bot in a persisted
                        # ``recovery_required`` status every start entry refuses, instead of
                        # leaving a startable mismatch (R1).
                        try:
                            db.commit()
                            _write_state_file(existing.id)
                            existing.status = "stopped"
                            db.commit()
                        except Exception:
                            _mark_recovery_required(db, existing.id)
                            raise
                except InstanceRetiring:
                    errors.append(f"Bot '{name}': a lifecycle transition is in progress; skipped")
                    continue
                overwritten += 1
            else:
                # A uuid is unique across the whole table (tombstones included), so a soft-deleted
                # row holding this uuid is a conflict, not a free slot: do not silently recreate
                # over it. Require a deliberate operator action instead (R2).
                prior = db.query(Bot).filter(Bot.uuid == uuid).first()
                if prior is not None:
                    errors.append(
                        f"Bot '{name}': a bot with uuid {uuid} already exists (possibly deleted). "
                        "Purge or restore it explicitly rather than creating a duplicate."
                    )
                    continue
                # Insert UNREADY: the row is not startable until its archived state is installed,
                # so a crash between the insert and the state write cannot expose a startable bot
                # with missing occupancy (R1). The uuid unique index makes a concurrent create of
                # the same uuid fail its commit, so only one restore wins (R2).
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
                    cluster_configs=cc_str,
                    config_overrides=co_str,
                    **agent_options,
                    status="recovery_required",
                )
                db.add(bot_obj)
                try:
                    db.flush()  # get bot_obj.id
                    from lockbot.backend.app.feishu.backup import restore_route

                    restore_route(db, bot_obj, bb, source_secret_key)
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    errors.append(f"Bot '{name}': uuid {uuid} was created concurrently; skipped.")
                    continue
                # A brand-new id cannot be owned by anyone else; take the owner for symmetry and
                # to serialize the state write against any immediately-following transition. Only
                # after the state is installed is the row promoted to a startable ``stopped``.
                try:
                    with bot_mgr_mod.bot_manager.transition(bot_obj.id):
                        _write_state_file(bot_obj.id)
                        bot_obj.status = "stopped"
                        db.commit()
                except Exception:
                    _mark_recovery_required(db, bot_obj.id)
                    raise
                created += 1

        except Exception as e:
            with contextlib.suppress(Exception):
                db.rollback()
            errors.append(f"Bot '{name}': {e}")
            continue

    with contextlib.suppress(Exception):
        db.commit()
    return {"restored": created + overwritten, "created": created, "overwritten": overwritten, "errors": errors}
