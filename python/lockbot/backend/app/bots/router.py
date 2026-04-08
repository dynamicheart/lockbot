"""
Bot CRUD + lifecycle + webhook routes.
"""

import contextlib
import json
import logging
import os
import traceback
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.dependencies import get_current_user, require_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.manager import bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.schemas import BotCreate, BotDetail, BotOut, BotStatusOut, BotUpdate
from lockbot.backend.app.bots.webhook_handler import handle_webhook
from lockbot.backend.app.database import get_db
from lockbot.core.config import Config
from lockbot.core.i18n import t
from lockbot.core.msg_utils import check_signature
from lockbot.core.platforms.infoflow import InfoflowAdapter

router = APIRouter(prefix="/api/bots", tags=["bots"])
logger = logging.getLogger(__name__)

VALID_BOT_TYPES = {"NODE", "DEVICE", "QUEUE"}


def _get_log_dir(bot_id: int) -> str:
    """Return the log directory for a bot, creating it if needed."""
    d = os.path.join("/data", "bots", str(bot_id))
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
        "created_at": datetime.utcnow().isoformat(),
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
    # Decrypt to raw values (separate field names to avoid overwriting DB columns)
    raw_webhook = encryption.decrypt(bot.webhook_url)
    raw_aes_key = encryption.decrypt(bot.aes_key)
    raw_token = encryption.decrypt(bot.token)
    data["webhook_url_raw"] = raw_webhook
    data["aes_key_raw"] = raw_aes_key
    data["token_raw"] = raw_token
    data["webhook_url_masked"] = encryption.mask(raw_webhook)
    data["aes_key_masked"] = encryption.mask(raw_aes_key)
    data["token_masked"] = encryption.mask(raw_token)
    # Include owner username and role if db session available
    if db:
        owner = db.get(User, bot.user_id)
        data["owner"] = owner.username if owner else ""
        data["owner_role"] = owner.role if owner else ""
    return data


def _get_user_bot(bot_id: int, user: User, db: Session) -> Bot:
    """Fetch a bot owned by the user (or any bot if admin), or raise 404."""
    bot = db.get(Bot, bot_id)
    if not bot or (bot.user_id != user.id and user.role not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


def _normalize_cluster_configs(cc):
    """Normalize device model strings to lowercase."""
    if isinstance(cc, dict):
        return {k: [m.lower() for m in v] if isinstance(v, list) else v for k, v in cc.items()}
    if isinstance(cc, list):
        return [k.lower() for k in cc]
    return cc


def _build_config_dict(bot: Bot) -> dict:
    """
    Build the full config dict from a DB Bot record.
    Decrypts sensitive fields and merges config_overrides.
    """
    config = {
        "BOT_ID": bot.id,
        "BOT_NAME": bot.name,
        "BOT_TYPE": bot.bot_type,
        "WEBHOOK_URL": encryption.decrypt(bot.webhook_url),
        "AESKEY": encryption.decrypt(bot.aes_key),
        "TOKEN": encryption.decrypt(bot.token),
        "CLUSTER_CONFIGS": _normalize_cluster_configs(json.loads(bot.cluster_configs)),
    }
    if bot.config_overrides:
        overrides = json.loads(bot.config_overrides)
        config.update(overrides)
    return config


# ── CRUD endpoints ───────────────────────────────────────────


@router.get("", response_model=list[BotOut])
def list_bots(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.query(Bot).filter(Bot.user_id == user.id).all()


@router.post("", response_model=BotOut, status_code=status.HTTP_201_CREATED)
def create_bot(
    body: BotCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.bot_type.upper() not in VALID_BOT_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid bot_type, must be one of {VALID_BOT_TYPES}")

    exists = db.query(Bot).filter(Bot.user_id == user.id, Bot.name == body.name).first()
    if exists:
        raise HTTPException(status_code=409, detail="Bot name already exists")

    bot = Bot(
        user_id=user.id,
        name=body.name,
        bot_type=body.bot_type.upper(),
        platform=body.platform,
        group_id=body.group_id,
        webhook_url=encryption.encrypt(body.webhook_url),
        aes_key=encryption.encrypt(body.aes_key),
        token=encryption.encrypt(body.token),
        cluster_configs=json.dumps(body.cluster_configs, ensure_ascii=False),
        config_overrides=json.dumps(body.config_overrides or {}, ensure_ascii=False),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


# ── Running states (batch) ─────────────────────────────────


@router.get("/running-states")
def get_running_states(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return live state for all running bots owned by the user."""
    user_bots = db.query(Bot).filter(Bot.user_id == user.id, Bot.status == "running").all()
    result = {}
    for bot in user_bots:
        instance = bot_manager.get_instance(bot.id)
        if instance:
            result[bot.id] = instance.state.bot_state
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
    body: BotUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Admin cannot edit other admins' bots
    if user.role == "admin" and bot.user_id != user.id:
        owner = db.get(User, bot.user_id)
        if owner and owner.role in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Cannot edit another admin's bot")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Cannot update a running bot. Stop it first.")

    if body.name is not None:
        bot.name = body.name
    if body.group_id is not None:
        bot.group_id = body.group_id
    if body.webhook_url is not None:
        bot.webhook_url = encryption.encrypt(body.webhook_url)
    if body.aes_key is not None:
        bot.aes_key = encryption.encrypt(body.aes_key)
    if body.token is not None:
        bot.token = encryption.encrypt(body.token)
    if body.cluster_configs is not None:
        bot.cluster_configs = json.dumps(body.cluster_configs, ensure_ascii=False)
    if body.config_overrides is not None:
        bot.config_overrides = json.dumps(body.config_overrides, ensure_ascii=False)

    db.commit()
    db.refresh(bot)
    return bot


@router.delete("/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bot(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    # Admin cannot delete other admins' bots
    if user.role == "admin" and bot.user_id != user.id:
        owner = db.get(User, bot.user_id)
        if owner and owner.role in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Cannot delete another admin's bot")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running bot. Stop it first.")

    db.delete(bot)
    db.commit()


@router.put("/{bot_id}/owner")
def transfer_bot_owner(
    bot_id: int,
    body: dict,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Transfer bot ownership. Admin can transfer regular users' bots, super_admin can transfer all."""
    bot = db.get(Bot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Permission: admin cannot transfer other admins' bots
    if user.role == "admin" and bot.user_id != user.id:
        owner = db.get(User, bot.user_id)
        if owner and owner.role in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Cannot transfer another admin's bot")

    target_username = (body.get("username") or "").strip()
    if not target_username:
        raise HTTPException(status_code=400, detail="username is required")

    target_user = db.query(User).filter(User.username == target_username).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    # Admin cannot transfer bot to another admin
    if user.role == "admin" and target_user.role in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Cannot transfer bot to an admin")

    old_owner = db.get(User, bot.user_id)
    old_name = old_owner.username if old_owner else "unknown"
    bot.user_id = target_user.id
    db.commit()

    _write_log(bot_id, f"Owner transferred from {old_name} to {target_username} by {user.username}")
    return {"id": bot.id, "owner": target_username}


# ── Lifecycle endpoints ──────────────────────────────────────


MAX_CONSECUTIVE_FAILURES = 3


@router.post("/{bot_id}/start", response_model=BotStatusOut)
def start_bot(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

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
        running_count = db.query(Bot).filter(Bot.user_id == user.id, Bot.status == "running").count()
        if running_count >= user.max_running_bots:
            raise HTTPException(
                status_code=403,
                detail=f"Running bot limit reached ({user.max_running_bots}). Stop a bot first or contact admin.",
            )

    config_dict = _build_config_dict(bot)

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
    db.commit()
    db.refresh(bot)
    _write_log(bot_id, "Bot 已启动")

    return BotStatusOut(id=bot.id, status=bot.status, pid=pid, message="Bot started")


@router.post("/{bot_id}/stop", response_model=BotStatusOut)
def stop_bot(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    if bot.status not in ("running", "error"):
        raise HTTPException(status_code=409, detail=f"Bot is not running (status={bot.status})")

    with contextlib.suppress(RuntimeError):
        bot_manager.stop_bot(bot_id)

    bot.status = "stopped"
    bot.pid = None
    bot.consecutive_failures = 0
    db.commit()
    _write_log(bot_id, "Bot 已停止")

    return BotStatusOut(id=bot.id, status=bot.status, message="Bot stopped")


@router.post("/{bot_id}/restart", response_model=BotStatusOut)
def restart_bot(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)

    config_dict = _build_config_dict(bot)

    try:
        pid = bot_manager.restart_bot(bot_id, config_dict)
    except RuntimeError as e:
        _write_log(bot_id, f"重启失败: {e}", level="ERROR")
        raise HTTPException(status_code=500, detail=str(e)) from None

    bot.status = "running"
    bot.pid = pid
    db.commit()
    db.refresh(bot)
    _write_log(bot_id, "Bot 已重启")

    return BotStatusOut(id=bot.id, status=bot.status, pid=pid, message="Bot restarted")


# ── Language endpoint ─────────────────────────────────────


@router.put("/{bot_id}/language")
def set_bot_language(
    bot_id: int,
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
    db.commit()

    # Hot-apply to running instance (no restart)
    instance = bot_manager.get_instance(bot_id)
    if instance and hasattr(instance.bot, "config"):
        instance.bot.config.set_val("LANGUAGE", lang)

    _write_log(bot_id, f"语言切换为 {lang}")
    return {"id": bot.id, "language": lang}


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
    state_file = os.path.join("/data", "bots", str(bot_id), "bot_state.json")
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


@router.put("/{bot_id}/state")
def update_bot_state(
    bot_id: int,
    state: dict,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    import os

    bot = _get_user_bot(bot_id, user, db)

    # Admin cannot edit other admins' bot state
    if user.role == "admin" and bot.user_id != user.id:
        owner = db.get(User, bot.user_id)
        if owner and owner.role in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Cannot edit another admin's bot state")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Stop the bot before editing state")

    instance = bot_manager.get_instance(bot_id)
    if instance:
        instance.state.bot_state = state

    # Persist to state file so it survives restarts
    state_file = os.path.join("/data", "bots", str(bot_id), "bot_state.json")
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

    return {"message": "State updated"}


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
async def webhook(bot_id: int, request: Request, db: Session = Depends(get_db)):
    """
    IM callback endpoint.

    Each bot's callback URL is configured in the IM platform as:
        http://<platform_host>:8000/api/bots/webhook/{bot_id}

    No JWT auth required — the IM platform authenticates via signature.
    """
    # Parse request data (matching Flask's request interface)
    body = await request.body()
    form = {}
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        form_data = await request.form()
        form = dict(form_data)

    args = dict(request.query_params)

    instance = bot_manager.get_instance(bot_id)
    if not instance:
        # Bot not running — try to reply "not started" via IM
        return await _reply_bot_not_running(bot_id, form, args, body, db)

    try:
        text, code, meta = handle_webhook(instance.bot, raw_form=form, raw_args=args, raw_body=body)
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("Webhook handler crashed for bot %d", bot_id)
        _write_log(bot_id, f"Webhook 处理异常: {e}\n{tb}", level="ERROR")
        return PlainTextResponse(content="internal error", status_code=500)

    # URL verification: Ruliu expects raw echostr as plain text, not JSON
    if form.get("echostr"):
        return PlainTextResponse(content=text, status_code=code)

    # Update last_request_at
    bot = db.get(Bot, bot_id)
    if bot:
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
    db: Session,
) -> PlainTextResponse:
    """When a bot is not running, POST a 'not started' message back to the IM platform."""
    bot = db.get(Bot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    # URL verification must still work even when the bot is stopped
    echostr = form.get("echostr")
    if echostr:
        token = encryption.decrypt(bot.token)
        sig = form.get("signature")
        rn = form.get("rn")
        ts = form.get("timestamp")
        if check_signature(sig, rn, ts, token):
            return PlainTextResponse(content=echostr, status_code=200)
        return PlainTextResponse(content="check signature fail", status_code=401)

    # Build a lightweight adapter from DB credentials
    config_dict = {
        "WEBHOOK_URL": encryption.decrypt(bot.webhook_url),
        "AESKEY": encryption.decrypt(bot.aes_key),
        "TOKEN": encryption.decrypt(bot.token),
    }
    config = Config(config_dict)
    adapter = InfoflowAdapter(config=config)

    # Verify signature
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
    content = t("webhook.bot_not_running", lang=lang, bot_name=bot.name, owner_username=owner_username)
    reply = adapter.build_reply(content, [user_id], group_id=group_id)
    toid = msg_data["message"]["header"].get("toid")
    if toid:
        reply["message"]["header"]["toid"] = toid

    try:
        adapter.send(reply)
    except Exception:
        logger.exception("Failed to send 'bot not running' reply for bot %d", bot_id)

    return PlainTextResponse(content="bot not running", status_code=200)
