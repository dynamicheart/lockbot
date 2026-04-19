"""
Bot CRUD + lifecycle + webhook routes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import traceback
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import get_current_user, require_admin, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.credentials import decrypt_credentials, encrypt_credentials
from lockbot.backend.app.bots.manager import bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.schemas import BotCreate, BotDetail, BotOut, BotStatusOut, BotUpdate
from lockbot.backend.app.bots.webhook_handler import handle_webhook
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.core.config import Config
from lockbot.core.i18n import t
from lockbot.core.platforms.infoflow import check_signature

router = APIRouter(prefix="/api/bots", tags=["bots"])
logger = logging.getLogger(__name__)

VALID_BOT_TYPES = {"NODE", "DEVICE", "QUEUE"}


_DEFAULT_DATA_DIR = os.environ.get("DATA_DIR", "/data")


def _get_log_dir(bot_id: int) -> str:
    """Return the log directory for a bot, creating it if needed."""
    d = os.path.join(_DEFAULT_DATA_DIR, "bots", str(bot_id))
    os.makedirs(d, exist_ok=True)
    return d


def _get_log_file(bot_id: int) -> str:
    return os.path.join(_get_log_dir(bot_id), "logs.jsonl")


def _write_log(bot_id: int, message: str, level: str = "INFO", category: str = "system"):
    """Append a log entry to the bot's JSONL log file. Auto-prune to MAX_LOG_ENTRIES."""
    MAX_LOG_ENTRIES = 1000
    log_path = _get_log_file(bot_id)
    entry = {
        "id": hash((bot_id, message, datetime.utcnow().microsecond)),
        "category": category,
        "level": level,
        "message": message,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to write log for bot %d", bot_id)
        return

    # Auto-prune: keep only the most recent MAX_LOG_ENTRIES lines
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_LOG_ENTRIES:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_LOG_ENTRIES:])
    except OSError:
        pass


def _mask_bot(bot: Bot, db: Session | None = None) -> dict:
    """Build detail dict with masked sensitive fields."""
    data = {c.name: getattr(bot, c.name) for c in bot.__table__.columns}
    creds = decrypt_credentials(bot)
    data["credentials_raw"] = creds
    data["credentials_masked"] = {k: encryption.mask(v) for k, v in creds.items()}
    # Include owner username and role if db session available
    if db:
        owner = db.get(User, bot.user_id)
        data["owner"] = owner.username if owner else ""
        data["owner_role"] = owner.role if owner else ""
    return data


def _get_user_bot(bot_id: int, user: User, db: Session) -> Bot:
    """Fetch a bot owned by the user (or any bot if admin), or raise 404."""
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted or (bot.user_id != user.id and user.role not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


def _normalize_cluster_configs(cc):
    """Normalize device model strings to lowercase."""
    if isinstance(cc, dict):
        return {k: [m.lower() for m in v] if isinstance(v, list) else v for k, v in cc.items()}
    if isinstance(cc, list):
        return [k.lower() for k in cc]
    return cc


def _build_config_dict(bot: Bot, db: Session | None = None) -> dict:
    """
    Build the full config dict from a DB Bot record.
    Decrypts sensitive fields and merges config_overrides + site settings.
    """
    config = {
        "BOT_ID": bot.id,
        "BOT_NAME": bot.name,
        "BOT_TYPE": bot.bot_type,
        "PLATFORM": bot.platform or "Infoflow",
        "CLUSTER_CONFIGS": _normalize_cluster_configs(json.loads(bot.cluster_configs)),
    }
    # Merge platform-specific credentials into config dict
    config.update(decrypt_credentials(bot))
    # Resolve owner username for help text display
    if db:
        owner = db.get(User, bot.user_id)
        if owner:
            config["BOT_OWNER"] = owner.username
    if bot.config_overrides:
        overrides = json.loads(bot.config_overrides)
        config.update(overrides)
    # Inject site-wide settings (lower priority than bot-level overrides)
    _inject_site_settings(config, db)
    return config


def _inject_site_settings(config: dict, db: Session | None = None):
    """Merge site_settings into bot config if not already set."""
    if db is None:
        return
    try:
        from lockbot.backend.app.settings.router import get_all_settings

        site = get_all_settings(db)
        for key in ("github_url", "platform_url"):
            if key not in config or not config[key]:
                val = site.get(key, "")
                if val:
                    config[key.upper()] = val
    except Exception:
        pass  # don't break bot startup if settings table is unavailable


# ── CRUD endpoints ───────────────────────────────────────────


@router.get("", response_model=list[BotOut])
def list_bots(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.query(Bot).filter(Bot.user_id == user.id, Bot.is_deleted.is_(False)).all()


@router.post("", response_model=BotOut, status_code=status.HTTP_201_CREATED)
def create_bot(
    request: Request,
    body: BotCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.bot_type.upper() not in VALID_BOT_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid bot_type, must be one of {VALID_BOT_TYPES}")

    exists = (
        db.query(Bot)
        .filter(
            Bot.user_id == user.id,
            Bot.name == body.name,
            Bot.is_deleted.is_(False),
        )
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="Bot name already exists")

    bot = Bot(
        user_id=user.id,
        name=body.name,
        bot_type=body.bot_type.upper(),
        platform=body.platform,
        group_id=body.group_id,
        credentials=encrypt_credentials(body.credentials),
        cluster_configs=json.dumps(body.cluster_configs, ensure_ascii=False),
        config_overrides=json.dumps(body.config_overrides or {}, ensure_ascii=False),
    )
    db.add(bot)
    db.flush()
    write_audit_log(
        db,
        user,
        "bot.create",
        target_type="bot",
        target_id=bot.id,
        target_name=bot.name,
        detail={"bot_type": bot.bot_type, "platform": bot.platform},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(bot)

    # Auto-start the bot if within quota
    auto_started = False
    if user.role not in ("admin", "super_admin"):
        running_count = (
            db.query(Bot)
            .filter(
                Bot.user_id == user.id,
                Bot.status == "running",
                Bot.is_deleted.is_(False),
            )
            .count()
        )
        if running_count >= user.max_running_bots:
            pass  # Quota reached — bot created but not started
        else:
            auto_started = True
    else:
        auto_started = True

    if auto_started:
        config_dict = _build_config_dict(bot, db)
        try:
            pid = bot_manager.start_bot(bot.id, config_dict)
            bot.status = "running"
            bot.pid = pid
            bot.consecutive_failures = 0
            db.commit()
            db.refresh(bot)
            _write_log(bot.id, "Bot created and auto-started")
        except Exception:
            db.rollback()
            logger.exception("Failed to auto-start bot %d", bot.id)

    result = BotOut.model_validate(bot)
    return result


# ── Running states (batch) ─────────────────────────────────


@router.get("/running-states")
def get_running_states(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return state for all bots owned by the user (running from memory, stopped from file)."""
    user_bots = db.query(Bot).filter(Bot.user_id == user.id, Bot.is_deleted.is_(False)).all()
    result = {}
    for bot in user_bots:
        instance = bot_manager.get_instance(bot.id)
        if instance:
            result[bot.id] = instance.state.bot_state
        else:
            # Bot not running — try loading persisted state file
            state_file = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
            if os.path.exists(state_file):
                try:
                    with open(state_file, encoding="utf-8") as f:
                        raw = json.load(f)
                        state = raw.get("bot_state") or raw.get("cluster_state") or raw.get("cluster_status")
                        if state:
                            result[bot.id] = state
                except Exception:
                    pass
    return result


@router.get("/{bot_id}", response_model=BotDetail)
def get_bot(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)
    return _mask_bot(bot, db)


@router.put("/{bot_id}", response_model=BotOut)
def update_bot(
    bot_id: int,
    request: Request,
    body: BotUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Only owner or super_admin can edit
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot edit another user's bot")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Cannot update a running bot. Stop it first.")

    changes = {}
    if body.name is not None:
        # Check for duplicate name (excluding current bot and deleted bots)
        dup = (
            db.query(Bot)
            .filter(
                Bot.user_id == bot.user_id,
                Bot.name == body.name,
                Bot.id != bot_id,
                Bot.is_deleted.is_(False),
            )
            .first()
        )
        if dup:
            raise HTTPException(status_code=409, detail="Bot name already exists")
        changes["name"] = [bot.name, body.name]
        bot.name = body.name
    if body.group_id is not None:
        bot.group_id = body.group_id
    if body.credentials is not None:
        changes["credentials"] = True
        # Merge changed fields into existing credentials; empty strings clear the key
        creds = decrypt_credentials(bot)
        for k, v in body.credentials.items():
            if v:
                creds[k] = v
            else:
                creds.pop(k, None)
        bot.credentials = encrypt_credentials(creds)
    if body.cluster_configs is not None:
        changes["cluster_configs"] = True
        bot.cluster_configs = json.dumps(body.cluster_configs, ensure_ascii=False)
    if body.config_overrides is not None:
        changes["config_overrides"] = True
        bot.config_overrides = json.dumps(body.config_overrides, ensure_ascii=False)

    write_audit_log(
        db,
        user,
        "bot.update",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        detail={"changed": list(changes.keys())},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(bot)
    return bot


@router.delete("/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bot(
    bot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Only owner or super_admin can delete
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot delete another user's bot")

    # Stop the bot first if running
    if bot.status in ("running", "error"):
        with contextlib.suppress(RuntimeError):
            bot_manager.stop_bot(bot_id)
        bot.status = "stopped"
        bot.pid = None

    bot_name = bot.name
    bot.is_deleted = True
    bot.deleted_at = datetime.utcnow()
    write_audit_log(
        db,
        user,
        "bot.delete",
        target_type="bot",
        target_id=bot_id,
        target_name=bot_name,
        ip=request.client.host if request.client else None,
    )
    db.commit()


@router.put("/{bot_id}/owner")
def transfer_bot_owner(
    bot_id: int,
    body: dict,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Transfer bot ownership. Super_admin can transfer all bots."""
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")

    target_username = (body.get("username") or "").strip()
    if not target_username:
        raise HTTPException(status_code=400, detail="username is required")

    target_user = db.query(User).filter(User.username == target_username).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    old_owner = db.get(User, bot.user_id)
    old_name = old_owner.username if old_owner else "unknown"
    bot.user_id = target_user.id
    write_audit_log(
        db,
        user,
        "bot.transfer",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        detail={"from": old_name, "to": target_username},
        ip=request.client.host if request.client else None,
    )
    db.commit()

    _write_log(bot_id, f"Owner transferred from {old_name} to {target_username} by {user.username}")
    return {"id": bot.id, "owner": target_username}


# ── Lifecycle endpoints ──────────────────────────────────────


MAX_CONSECUTIVE_FAILURES = 3


@router.post("/{bot_id}/start", response_model=BotStatusOut)
@limiter.limit("10/minute")
def start_bot(
    request: Request,
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Only owner or super_admin can start
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot start another user's bot")

    if bot.status == "running":
        return BotStatusOut(
            id=bot.id,
            status=bot.status,
            pid=bot.pid,
            consecutive_failures=bot.consecutive_failures or 0,
            message="Bot is already running",
        )

    # Enforce max_running_bots quota (admins are exempt)
    if user.role not in ("admin", "super_admin"):
        running_count = (
            db.query(Bot)
            .filter(
                Bot.user_id == user.id,
                Bot.status == "running",
                Bot.is_deleted.is_(False),
            )
            .count()
        )
        if running_count >= user.max_running_bots:
            raise HTTPException(
                status_code=403,
                detail=f"Running bot limit reached ({user.max_running_bots}). Stop a bot first or contact admin.",
            )

    config_dict = _build_config_dict(bot, db)

    try:
        pid = bot_manager.start_bot(bot_id, config_dict)
    except Exception as e:
        bot.consecutive_failures = (bot.consecutive_failures or 0) + 1
        if bot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            bot.status = "error"
            db.commit()
            _write_log(bot_id, f"连续启动失败 {bot.consecutive_failures} 次，已标记为 error: {e}", level="ERROR")
            raise HTTPException(
                status_code=409,
                detail=f"Bot marked as error after {bot.consecutive_failures} consecutive failures: {e}",
            ) from None
        db.commit()
        _write_log(bot_id, f"启动失败 ({bot.consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}", level="ERROR")
        raise HTTPException(status_code=409, detail=str(e)) from None

    bot.status = "running"
    bot.pid = pid
    bot.consecutive_failures = 0
    write_audit_log(
        db,
        user,
        "bot.start",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(bot)
    _write_log(bot_id, "Bot 已启动")

    return BotStatusOut(id=bot.id, status=bot.status, pid=pid, message="Bot started")


@router.post("/{bot_id}/stop", response_model=BotStatusOut)
@limiter.limit("10/minute")
def stop_bot(
    request: Request,
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Only owner or super_admin can stop
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot stop another user's bot")

    if bot.status not in ("running", "error"):
        raise HTTPException(status_code=409, detail=f"Bot is not running (status={bot.status})")

    with contextlib.suppress(RuntimeError):
        bot_manager.stop_bot(bot_id)

    bot.status = "stopped"
    bot.pid = None
    bot.consecutive_failures = 0
    write_audit_log(
        db,
        user,
        "bot.stop",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    _write_log(bot_id, "Bot 已停止")

    return BotStatusOut(id=bot.id, status=bot.status, message="Bot stopped")


@router.post("/{bot_id}/restart", response_model=BotStatusOut)
@limiter.limit("10/minute")
def restart_bot(
    request: Request,
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Only owner or super_admin can restart
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot restart another user's bot")

    config_dict = _build_config_dict(bot, db)

    try:
        pid = bot_manager.restart_bot(bot_id, config_dict)
    except RuntimeError as e:
        _write_log(bot_id, f"重启失败: {e}", level="ERROR")
        raise HTTPException(status_code=500, detail=str(e)) from None

    bot.status = "running"
    bot.pid = pid
    write_audit_log(
        db,
        user,
        "bot.restart",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(bot)
    _write_log(bot_id, "Bot 已重启")

    return BotStatusOut(id=bot.id, status=bot.status, pid=pid, message="Bot restarted")


# ── Language endpoint ─────────────────────────────────────


@router.put("/{bot_id}/language")
def set_bot_language(
    bot_id: int,
    request: Request,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Switch bot display language at runtime (no restart needed)."""
    lang = body.get("language", "").strip().lower()
    if lang not in ("zh", "en"):
        raise HTTPException(status_code=400, detail="language must be 'zh' or 'en'")

    bot = _get_user_bot(bot_id, user, db)

    # Persist in config_overrides
    overrides = json.loads(bot.config_overrides or "{}")
    overrides["LANGUAGE"] = lang
    bot.config_overrides = json.dumps(overrides, ensure_ascii=False)

    write_audit_log(
        db,
        user,
        "bot.set_language",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        detail={"language": lang},
        ip=request.client.host if request.client else None,
    )
    db.commit()

    # Hot-apply to running instance (no restart)
    instance = bot_manager.get_instance(bot_id)
    if instance and hasattr(instance.bot, "config"):
        instance.bot.config.set_val("LANGUAGE", lang)

    _write_log(bot_id, f"语言切换为 {lang}")
    return {"id": bot.id, "language": lang}


def _get_bot_data_dir(bot: Bot) -> str:
    """Return the data directory for a bot, preferring config_overrides.DATA_DIR."""
    try:
        overrides = json.loads(bot.config_overrides or "{}")
        data_dir = overrides.get("DATA_DIR") or _DEFAULT_DATA_DIR
    except (json.JSONDecodeError, AttributeError):
        data_dir = _DEFAULT_DATA_DIR
    return os.path.join(data_dir, "bots", str(bot.id))


# ── State endpoints ────────────────────────────────────────


@router.get("/{bot_id}/state")
def get_bot_state(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import os

    bot = _get_user_bot(bot_id, user, db)
    instance = bot_manager.get_instance(bot_id)

    if instance:
        return instance.state.bot_state

    # Bot not running — try loading persisted state file first
    state_file = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                raw = json.load(f)
                state = raw.get("bot_state") or raw.get("cluster_state") or raw.get("cluster_status")
                if state:
                    return state
        except Exception:
            pass

    # No state file — build initial state from cluster_configs
    try:
        cluster_configs = json.loads(bot.cluster_configs)
    except (json.JSONDecodeError, TypeError):
        return {}

    if bot.bot_type == "DEVICE":
        return {
            node_key: [
                {
                    "dev_id": dev_id,
                    "dev_model": devices[dev_id],
                    "status": "idle",
                    "current_users": [],
                }
                for dev_id in range(len(devices))
            ]
            for node_key, devices in cluster_configs.items()
        }

    # NODE / QUEUE: default idle state
    return {name: {"status": "idle", "current_users": [], "booking_list": []} for name in cluster_configs}


# ── State validation ────────────────────────────────────────

_VALID_STATUSES = {"idle", "exclusive", "shared"}
_REQUIRED_USER_KEYS = {"user_id", "start_time", "duration"}


def _validate_user_info(user_info, label, config):
    """Validate a single user_info dict, filling missing fields. Returns warnings."""
    warnings = []
    for key in _REQUIRED_USER_KEYS:
        if key not in user_info:
            user_info[key] = "" if key == "user_id" else 0
            warnings.append(t("state.missing_key", config=config, label=label, field_name=key))
    if "is_notified" not in user_info:
        user_info["is_notified"] = False
    return warnings


def _validate_and_align_state(state, bot_type, cluster_configs, config=None):
    """Validate and normalize bot state against cluster_configs.

    Returns (normalized_state, warnings) where warnings is a list of
    human-readable strings for issues that were auto-fixed.
    """
    if not isinstance(state, dict):
        return _default_state_for(cluster_configs, bot_type), [t("state.state_not_dict", config=config)]

    warnings = []
    if bot_type == "DEVICE":
        return _validate_device_state(state, cluster_configs, warnings, config)
    # NODE and QUEUE share the same structure
    return _validate_node_queue_state(state, cluster_configs, warnings, config)


def _default_state_for(cluster_configs, bot_type):
    """Build a fresh default state matching cluster_configs."""
    if bot_type == "DEVICE":
        return {
            node_key: [
                {"dev_id": i, "dev_model": model, "status": "idle", "current_users": []}
                for i, model in enumerate(devices)
            ]
            for node_key, devices in cluster_configs.items()
        }
    return {name: {"status": "idle", "current_users": [], "booking_list": []} for name in cluster_configs}


def _validate_node_queue_state(state, cluster_configs, warnings, config):
    result = {}
    # Add entries for nodes in cluster_configs
    for name in cluster_configs:
        if name not in state:
            warnings.append(t("state.node_missing", config=config, name=name))
            result[name] = {"status": "idle", "current_users": [], "booking_list": []}
            continue
        node = state[name]
        if not isinstance(node, dict):
            warnings.append(t("state.node_not_dict", config=config, name=name))
            result[name] = {"status": "idle", "current_users": [], "booking_list": []}
            continue
        # Validate status
        status = node.get("status", "idle")
        if status not in _VALID_STATUSES:
            warnings.append(t("state.invalid_status", config=config, name=name, status=status))
            status = "idle"
        # Validate current_users
        current_users = node.get("current_users", [])
        if not isinstance(current_users, list):
            warnings.append(t("state.current_users_not_list", config=config, name=name))
            current_users = []
        for i, u in enumerate(current_users):
            if isinstance(u, dict):
                warnings.extend(_validate_user_info(u, f"Node '{name}', current_users[{i}]", config))
            else:
                warnings.append(t("state.entry_not_dict", config=config, name=name, field=f"current_users[{i}]"))
                current_users[i] = {"user_id": "", "start_time": 0, "duration": 0, "is_notified": False}
        # Validate booking_list
        booking_list = node.get("booking_list", [])
        if not isinstance(booking_list, list):
            warnings.append(t("state.booking_list_not_list", config=config, name=name))
            booking_list = []
        for i, u in enumerate(booking_list):
            if isinstance(u, dict):
                warnings.extend(_validate_user_info(u, f"Node '{name}', booking_list[{i}]", config))
            else:
                warnings.append(t("state.entry_not_dict", config=config, name=name, field=f"booking_list[{i}]"))
                booking_list[i] = {"user_id": "", "start_time": 0, "duration": 0, "is_notified": False}
        result[name] = {"status": status, "current_users": current_users, "booking_list": booking_list}
    # Warn about extra nodes not in cluster_configs
    for name in state:
        if name not in cluster_configs:
            warnings.append(t("state.node_not_in_config", config=config, name=name))
    return result, warnings


def _validate_device_state(state, cluster_configs, warnings, config):
    result = {}
    for node_key, devices in cluster_configs.items():
        if node_key not in state:
            warnings.append(t("state.node_missing", config=config, name=node_key))
            result[node_key] = [
                {"dev_id": i, "dev_model": model, "status": "idle", "current_users": []}
                for i, model in enumerate(devices)
            ]
            continue
        node_state = state[node_key]
        if not isinstance(node_state, list):
            warnings.append(t("state.node_not_list", config=config, name=node_key))
            result[node_key] = [
                {"dev_id": i, "dev_model": model, "status": "idle", "current_users": []}
                for i, model in enumerate(devices)
            ]
            continue
        expected_count = len(devices)
        actual_count = len(node_state)
        # Trim excess devices
        if actual_count > expected_count:
            warnings.append(
                t("state.device_excess", config=config, name=node_key, actual=actual_count, expected=expected_count)
            )
            node_state = node_state[:expected_count]
        # Validate each device
        device_list = []
        for i in range(expected_count):
            if i < len(node_state):
                dev = node_state[i]
                if not isinstance(dev, dict):
                    warnings.append(t("state.device_not_dict", config=config, name=node_key, index=i))
                    dev = {}
            else:
                warnings.append(t("state.device_missing", config=config, name=node_key, index=i))
                dev = {}
            # Validate dev_id
            dev.setdefault("dev_id", i)
            if dev["dev_id"] != i:
                warnings.append(
                    t("state.dev_id_corrected", config=config, name=node_key, index=i, old=dev["dev_id"], new=i)
                )
                dev["dev_id"] = i
            # Sync dev_model from cluster_configs
            dev["dev_model"] = devices[i]
            # Validate status
            status = dev.get("status", "idle")
            if status not in _VALID_STATUSES:
                warnings.append(t("state.invalid_status", config=config, name=f"{node_key}, device {i}", status=status))
                status = "idle"
            dev["status"] = status
            # Validate current_users
            current_users = dev.get("current_users", [])
            if not isinstance(current_users, list):
                warnings.append(t("state.current_users_not_list", config=config, name=f"{node_key}, device {i}"))
                current_users = []
            for j, u in enumerate(current_users):
                if isinstance(u, dict):
                    warnings.extend(
                        _validate_user_info(u, f"Node '{node_key}', device {i}, current_users[{j}]", config)
                    )
                else:
                    warnings.append(
                        t(
                            "state.entry_not_dict",
                            config=config,
                            name=f"{node_key}, device {i}",
                            field=f"current_users[{j}]",
                        )
                    )
                    current_users[j] = {"user_id": "", "start_time": 0, "duration": 0, "is_notified": False}
            dev["current_users"] = current_users
            device_list.append(dev)
        result[node_key] = device_list
    # Warn about extra nodes
    for name in state:
        if name not in cluster_configs:
            warnings.append(t("state.node_not_in_config", config=config, name=name))
    return result, warnings


@router.put("/{bot_id}/state")
def update_bot_state(
    bot_id: int,
    request: Request,
    state: dict,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    import os

    bot = _get_user_bot(bot_id, user, db)

    # Admin can only edit own bot state, super_admin can edit any
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot edit another user's bot state")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Stop the bot before editing state")

    # Validate and align state with cluster_configs
    cluster_configs = _normalize_cluster_configs(json.loads(bot.cluster_configs))
    config_dict = _build_config_dict(bot, db)
    from lockbot.core.config import Config

    config_obj = Config(config_dict)
    state, warnings = _validate_and_align_state(state, bot.bot_type, cluster_configs, config_obj)

    instance = bot_manager.get_instance(bot_id)
    if instance:
        instance.state.bot_state = state

    # Persist to state file so it survives restarts
    state_file = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    import tempfile

    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(state_file), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"bot_state": state}, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, state_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    result = {"message": "State updated"}
    if warnings:
        result["warnings"] = warnings
    write_audit_log(
        db,
        user,
        "bot.edit_state",
        target_type="bot",
        target_id=bot_id,
        target_name=bot.name,
        detail={"warnings": warnings} if warnings else None,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


# ── Logs endpoints ─────────────────────────────────────────


@router.get("/{bot_id}/logs")
def get_bot_logs(
    bot_id: int,
    page: int = 1,
    limit: int = 50,
    level: str | None = None,
    category: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_user_bot(bot_id, user, db)

    log_path = _get_log_file(bot_id)
    if not os.path.exists(log_path):
        return []

    # Read and parse all entries (file is auto-pruned to 1000 max)
    entries = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    with contextlib.suppress(json.JSONDecodeError):
                        entries.append(json.loads(line))
    except OSError:
        return []

    # Filter
    if level:
        level_upper = level.upper()
        entries = [e for e in entries if e.get("level", "").upper() == level_upper]
    if category:
        entries = [e for e in entries if e.get("category") == category]

    # Sort newest first, paginate
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    offset = (page - 1) * limit
    return entries[offset : offset + limit]


# ── Webhook endpoint ────────────────────────────────────────


@router.post("/webhook/{bot_id}")
@limiter.limit("120/minute")
async def webhook(bot_id: int, request: Request, db: Session = Depends(get_db)):
    """
    IM callback endpoint.

    Each bot's callback URL is configured in the IM platform as:
        http://<platform_host>:8000/api/bots/webhook/{bot_id}

    No JWT auth required — the IM platform authenticates via signature.
    """
    # Parse request data
    body = await request.body()
    form = {}
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        form_data = await request.form()
        form = dict(form_data)

    args = dict(request.query_params)
    headers = dict(request.headers)

    logger.debug(
        "[Webhook] bot=%d content_type=%s body_len=%d client=%s",
        bot_id,
        content_type,
        len(body),
        request.client.host if request.client else "?",
    )

    instance = bot_manager.get_instance(bot_id)
    if not instance:
        logger.debug("[Webhook] bot=%d not running, replying 'not started'", bot_id)
        # Bot not running — try to reply "not started" via IM
        return await _reply_bot_not_running(bot_id, form, args, body, headers, db)

    try:
        text, code, meta = handle_webhook(
            instance.bot, raw_form=form, raw_args=args, raw_body=body, raw_headers=headers
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("[Webhook] handler crashed for bot %d: %s", bot_id, e)
        _write_log(bot_id, f"Webhook 处理异常: {e}\n{tb}", level="ERROR")
        return PlainTextResponse(content="internal error", status_code=500)

    logger.debug(
        "[Webhook] bot=%d response: code=%d, event=%s, user=%s, cmd=%s",
        bot_id,
        code,
        meta.get("event", ""),
        meta.get("user_id", ""),
        meta.get("command", ""),
    )

    # For verification events just return immediately (no DB updates needed).
    # Feishu challenge response must be JSON {"challenge": "..."}; detect by content shape.
    if meta.get("event") in ("url_verification", "url_verification_failed", "signature_failed"):
        if text.startswith("{") and "challenge" in text:
            return JSONResponse(content=json.loads(text), status_code=code)
        return PlainTextResponse(content=text, status_code=code)

    # Update last_request_at
    bot = db.get(Bot, bot_id)
    if bot and not bot.is_deleted:
        bot.last_request_at = datetime.utcnow()
        db.commit()

    # Write logs for commands, update group_id/last_user_id for any valid request
    uid = meta.get("user_id")
    gid = meta.get("group_id")
    if meta.get("command"):
        _write_log(bot_id, f"[{uid or 'unknown'}] {meta['command']}", category="command")
    if gid and bot:
        db.refresh(bot)
        existing = set(bot.group_id.split(",") if bot.group_id else [])
        existing.add(str(gid))
        bot.group_id = ",".join(sorted(existing))
        db.commit()
    if uid and bot:
        db.refresh(bot)
        bot.last_user_id = uid
        db.commit()

    return PlainTextResponse(content=text, status_code=code)


async def _reply_bot_not_running(
    bot_id: int,
    form: dict,
    args: dict,
    body: bytes,
    headers: dict,
    db: Session,
) -> PlainTextResponse:
    """When a bot is not running, POST a 'not started' message back to the IM platform."""
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")

    body_str = body.decode("utf-8") if body else ""

    # Build a lightweight adapter from DB credentials
    config_dict = {
        "PLATFORM": bot.platform or "Infoflow",
    }
    config_dict.update(decrypt_credentials(bot))
    config = Config(config_dict)
    from lockbot.core.platforms import PLATFORM_REGISTRY

    adapter_cls = PLATFORM_REGISTRY.get(bot.platform or "Infoflow", PLATFORM_REGISTRY["Infoflow"])
    adapter = adapter_cls(config=config)

    # URL verification must still work even when the bot is stopped
    # Slack: JSON body with type == "url_verification"
    if body_str:
        try:
            maybe_json = json.loads(body_str)
            if maybe_json.get("type") == "url_verification":
                ts = headers.get("x-slack-request-timestamp", "")
                sig = headers.get("x-slack-signature", "")
                if adapter.verify_request(sig, timestamp=ts, body=body_str):
                    return PlainTextResponse(content=maybe_json["challenge"], status_code=200)
                return PlainTextResponse(content="check signature fail", status_code=401)
        except (ValueError, KeyError):
            pass

    # Infoflow: echostr handshake
    echostr = form.get("echostr")
    if echostr:
        creds = decrypt_credentials(bot)
        token = creds.get("token", "")
        sig = form.get("signature")
        rn = form.get("rn")
        ts = form.get("timestamp")
        if check_signature(sig, rn, ts, token):
            return PlainTextResponse(content=echostr, status_code=200)
        return PlainTextResponse(content="check signature fail", status_code=401)

    # Verify signature
    slack_sig = headers.get("x-slack-signature")
    if slack_sig:
        ts = headers.get("x-slack-request-timestamp", "")
        if not adapter.verify_request(slack_sig, timestamp=ts, body=body_str):
            return PlainTextResponse(content="check signature fail", status_code=401)
    else:
        signature = args.get("signature")
        rn = args.get("rn")
        timestamp = args.get("timestamp")
        if not adapter.verify_request(signature, rn=rn, timestamp=timestamp):
            return PlainTextResponse(content="check signature fail", status_code=401)

    # Decrypt the incoming message to get user_id and group_id
    msg_data = adapter.decrypt_payload(body)
    if msg_data is None:
        return PlainTextResponse(content="decrypt failed", status_code=400)

    try:
        user_id, group_id, _ = adapter.extract_command(msg_data)
    except Exception:
        logger.warning("Failed to extract command from stopped-bot webhook (bot %d)", bot_id)
        return PlainTextResponse(content="ok", status_code=200)

    # Determine language from bot's config_overrides
    lang = "zh"
    with contextlib.suppress(Exception):
        overrides = json.loads(bot.config_overrides or "{}")
        lang = overrides.get("LANGUAGE", "zh")

    # Get owner username for the reply
    owner = db.get(User, bot.user_id)
    owner_username = owner.username if owner else ""

    # Build and send "bot not running" reply
    if bot.status == "error":
        content = t("webhook.bot_error", lang=lang, bot_name=bot.name, owner_username=owner_username)
    else:
        content = t("webhook.bot_not_running", lang=lang, bot_name=bot.name, owner_username=owner_username)
    reply = adapter.build_reply(content, [user_id], group_id=group_id)
    adapter.set_reply_target(reply, group_id)

    try:
        adapter.send(reply)
    except Exception:
        logger.exception("Failed to send 'bot not running' reply for bot %d", bot_id)

    return PlainTextResponse(content="bot not running", status_code=200)
