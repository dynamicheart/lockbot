"""
Bot CRUD + lifecycle + webhook routes.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import traceback
import uuid as _uuid_mod
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session, sessionmaker

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import get_current_user, require_admin, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.datasource import serialized_datasource
from lockbot.backend.app.bots.manager import (
    AdmissionFull,
    BotNotRunning,
    InstanceRetiring,
    MayHaveExecuted,
    bot_manager,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.schemas import (
    BotCreate,
    BotDetail,
    BotOut,
    BotStatusOut,
    BotUpdate,
    LockedUsersResponse,
    OpkickRequest,
    OpkickResponse,
    ResourceAuthToggle,
)
from lockbot.backend.app.bots.webhook_handler import handle_webhook
from lockbot.backend.app.database import get_db
from lockbot.backend.app.managed_paths import get_managed_paths, sanitize_managed_overrides
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors as api_errors
from lockbot.backend.app.resource_api.sources import SOURCE_CHAT, SOURCE_SERVICE_KEY
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.state_migration import assert_bot_state_ready
from lockbot.core.config import Config
from lockbot.core.handler import redact_command_text
from lockbot.core.i18n import t
from lockbot.core.msg_utils import check_signature
from lockbot.core.platforms.registry import create_adapter

router = APIRouter(prefix="/api/bots", tags=["bots"])
logger = logging.getLogger(__name__)

VALID_BOT_TYPES = {"NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"}


def _get_log_dir(bot_id: int) -> str:
    """Return the log directory for a bot, creating it if needed."""
    d = str(get_managed_paths().bot_dir(bot_id))
    os.makedirs(d, exist_ok=True)
    return d


def _get_log_file(bot_id: int) -> str:
    _get_log_dir(bot_id)
    return str(get_managed_paths().bot_file(bot_id, "logs.jsonl"))


def _write_log(
    bot_id: int,
    message: str,
    level: str = "INFO",
    category: str = "system",
    source: str | None = None,
    credential_id: int | None = None,
):
    """Append a log entry to the bot's JSONL log file. Auto-prune to MAX_LOG_ENTRIES.

    ``source`` discriminates the transport ("chat" | "api" | "service_key") and
    ``credential_id`` identifies which agent credential drove an API call, so load can be
    attributed and a misbehaving agent can be found rather than guessed at. Both are
    omitted when unset, leaving pre-existing entries and readers unaffected.
    """
    MAX_LOG_ENTRIES = 1000
    log_path = _get_log_file(bot_id)
    entry = {
        "id": hash((bot_id, message, datetime.utcnow().microsecond)),
        "category": category,
        "level": level,
        "message": message,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    if source is not None:
        entry["source"] = source
    if credential_id is not None:
        entry["credential_id"] = credential_id
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
    data["has_api_key"] = bool(bot.api_key)
    data["api_key"] = bot.api_key or ""
    return data


def _get_user_bot(bot_id: int, user: User, db: Session) -> Bot:
    """Fetch a bot owned by the user (or any bot if admin), or raise 404."""
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted or (bot.user_id != user.id and user.role not in ("admin", "super_admin")):
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@contextlib.contextmanager
def _owned_bot(bot_id: int, user: User, db: Session):
    """Acquire the per-bot lifecycle transition owner, then re-read and re-validate the row.

    Every lifecycle transition decision (start / stop / restart / delete / offline edit) must be
    made on inputs read *after* ownership is held, not on a row loaded before the owner was free.
    Otherwise a request paused after its initial fetch could, once it finally acquires the owner,
    act on a stale object whose bot was deleted or reassigned in the meantime. We therefore end
    the current read transaction and re-load a fresh row under the owner, re-validating existence,
    deletion and permission before yielding. A competing owner is surfaced as the transient 503.
    """
    try:
        with bot_manager.transition(bot_id):
            # End any open read transaction so the fresh read sees other sessions' commits
            # (e.g. a delete that completed while we waited for ownership), not a snapshot.
            db.rollback()
            bot = db.get(Bot, bot_id)
            if bot is None or bot.is_deleted:
                raise HTTPException(status_code=404, detail="Bot not found")
            if user.role != "super_admin" and bot.user_id != user.id:
                raise HTTPException(status_code=403, detail="Cannot act on another user's bot")
            yield bot
    except InstanceRetiring:
        raise api_errors.instance_retiring(bot_id) from None


def _reset_row_to_stopped(db: Session, bot_id: int) -> None:
    """Best-effort reset the DB row to ``stopped``/pid None, so a failed start/restart never leaves
    a phantom ``running`` row with no live instance. The caller must hold the transition owner for
    ``bot_id`` and must have already stopped/cleaned up any in-process instance."""
    try:
        bot_manager.stop_bot(bot_id)
    except MayHaveExecuted:
        # The drain could not confirm the instance was cleaned up: an admitted worker may still be
        # mutating and persisting state. Claiming ``stopped`` here would be a lie, so leave the
        # lifecycle status untouched and let the caller surface the uncertainty. (R4: honest
        # cleanup — never report stopped before cleanup is confirmed.)
        logger.error("Bot %d: cannot confirm cleanup during compensation; not marking stopped", bot_id)
        return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        db.rollback()
        row = db.get(Bot, bot_id)
        if row is not None and not row.is_deleted and row.status != "recovery_required":
            row.status = "stopped"
            row.pid = None
            db.commit()


def _commit_initial_status_then_activate(db: Session, bot_id: int) -> None:
    """Commit the staged generation's initial status/audit (already set on the row) then activate
    it. On a commit OR activation failure, retire the staged generation (admission is refused while
    staged, so it is never left admitted-but-unscheduled) AND compensate the row back to
    ``stopped``/pid None, so a failed start never leaves a phantom ``running`` row with no live
    instance. The caller must hold the transition owner for ``bot_id``."""
    try:
        db.commit()
    except Exception:
        _reset_row_to_stopped(db, bot_id)
        raise
    try:
        bot_manager.activate_bot(bot_id)
    except Exception:
        _reset_row_to_stopped(db, bot_id)
        raise


def _normalize_cluster_configs(cc):
    """Normalize device model strings to lowercase."""
    if isinstance(cc, dict):
        return {k: [m.lower() for m in v] if isinstance(v, list) else v for k, v in cc.items()}
    if isinstance(cc, list):
        return [k.lower() for k in cc]
    return cc


def _get_node_keys(cc) -> set:
    """Extract node key set from cluster_configs (any format)."""
    if _is_device_array_format(cc):
        return {item["node_key"] for item in cc}
    if isinstance(cc, dict):
        return set(cc.keys())
    if isinstance(cc, list):
        return set(cc)
    return set()


def _validate_node_aliases(config_overrides, cluster_configs):
    """Raise HTTPException if NODE_ALIASES contains keys not in cluster_configs."""
    if not config_overrides:
        return
    aliases = config_overrides.get("NODE_ALIASES")
    if not aliases:
        return
    valid_keys = _get_node_keys(cluster_configs)
    invalid = set(aliases.keys()) - valid_keys
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"NODE_ALIASES contains keys not in cluster_configs: {sorted(invalid)}",
        )


def _device_dict_to_array(d: dict) -> list:
    """Convert DEVICE cluster_configs from dict to ordered array for API response.

    Why: JS JSON.parse auto-sorts pure-numeric keys ("0","1","2"), losing user-defined
    node ordering. Array format preserves insertion order in all environments.
    """
    return [{"node_key": k, "devices": v} for k, v in d.items()]


def _device_array_to_dict(arr: list) -> dict:
    """Convert DEVICE cluster_configs from ordered array to dict for DB storage.

    DB continues to store dict format; conversion happens at API boundary only.
    """
    return {item["node_key"]: item["devices"] for item in arr}


def _is_device_array_format(cc) -> bool:
    """Check if cluster_configs is in the new array-of-dicts DEVICE format."""
    return isinstance(cc, list) and bool(cc) and isinstance(cc[0], dict) and "node_key" in cc[0]


def _agent_skill_url(bot_id, expected_uuid):
    """Trusted deployment URL and current gate, independent of incoming Host headers."""
    from urllib.parse import urlsplit

    from lockbot.backend.app.database import SessionLocal
    from lockbot.backend.app.settings.router import get_all_settings

    with SessionLocal() as session:
        current = session.get(Bot, bot_id)
        if not current or current.is_deleted or current.uuid != expected_uuid or not current.resource_auth_enabled:
            return ""
        base = (get_all_settings(session).get("platform_url") or "").strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return base.split("#", 1)[0].rstrip("/") + "/api/agent-skill/SKILL.md"


def _build_config_dict(bot: Bot, db: Session | None = None) -> dict:
    """
    Build the full config dict from a DB Bot record.
    Decrypts sensitive fields and merges config_overrides + site settings.
    """
    paths = get_managed_paths()
    assert_bot_state_ready(bot, paths)
    config = {
        "BOT_ID": bot.id,
        "BOT_NAME": bot.name,
        "BOT_TYPE": bot.bot_type,
        "PLATFORM": bot.platform,
        "WEBHOOK_URL": encryption.decrypt(bot.webhook_url),
        "AESKEY": encryption.decrypt(bot.aes_key),
        "TOKEN": encryption.decrypt(bot.token),
        "CLUSTER_CONFIGS": _normalize_cluster_configs(json.loads(bot.cluster_configs)),
    }
    # Resolve owner username for help text display
    if db:
        owner = db.get(User, bot.user_id)
        if owner:
            config["BOT_OWNER"] = owner.username
    if bot.config_overrides:
        config.update(sanitize_managed_overrides(json.loads(bot.config_overrides), bot_id=bot.id))
    # Inject site-wide settings (lower priority than bot-level overrides)
    _inject_site_settings(config, db)
    bot_id = bot.id
    config["_AUTHORIZED_GROUP_IDS"] = [
        g.strip() for g in (getattr(bot, "group_id", None) or "").split(",") if g.strip()
    ]
    config["_AGENT_SKILL_URL"] = lambda bot_id=bot.id, uuid=getattr(bot, "uuid", ""): _agent_skill_url(bot_id, uuid)
    from lockbot.backend.app.resource_api.view_links import group_view_link

    config["_BOT_VIEW_LINK"] = lambda bot_id=bot.id, uuid=getattr(bot, "uuid", ""): group_view_link(bot_id, uuid)
    config["_IDENTITY_REVISION"] = getattr(bot, "identity_revision", 0)
    config["DATA_DIR"] = str(paths.bots_root)
    config["STATE_FILENAME"] = str(paths.state_file(bot_id))
    config["_MANAGED_BOT_DIR"] = lambda: paths.core_directory(bot_id)
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

    _validate_node_aliases(body.config_overrides, body.cluster_configs)

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
        uuid=str(_uuid_mod.uuid4()),
        user_id=user.id,
        name=body.name,
        bot_type=body.bot_type.upper(),
        platform=body.platform,
        group_id=body.group_id,
        webhook_url=encryption.encrypt(body.webhook_url),
        aes_key=encryption.encrypt(body.aes_key),
        token=encryption.encrypt(body.token),
        cluster_configs=json.dumps(
            _device_array_to_dict(body.cluster_configs)
            if _is_device_array_format(body.cluster_configs)
            else body.cluster_configs,
            ensure_ascii=False,
        ),
        config_overrides=json.dumps(body.config_overrides or {}, ensure_ascii=False),
        resource_auth_enabled=body.resource_auth_enabled,
        resource_api_notify_enabled=body.resource_api_notify_enabled,
        utilization_config=serialized_datasource(body.utilization_config),
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
        # Route auto-start through the same fresh-owner-held, staged protocol as explicit start
        # (B23/B24): the newly created row is already discoverable and could be deleted or
        # transferred before we acquire the owner, so re-validate it under the owner and stage
        # the generation before committing its status, then activate.
        new_bot_id = bot.id
        try:
            with _owned_bot(new_bot_id, user, db) as fresh:
                if fresh.status == "recovery_required":
                    raise HTTPException(status_code=409, detail="Bot needs recovery")
                config_dict = _build_config_dict(fresh, db)
                pid = bot_manager.start_bot(new_bot_id, config_dict, activate=False)
                fresh.status = "running"
                fresh.pid = pid
                fresh.consecutive_failures = 0
                _commit_initial_status_then_activate(db, new_bot_id)
            _write_log(new_bot_id, "Bot created and auto-started")
        except HTTPException:
            # The bot was deleted/transferred, or auto-start was refused; leave it created but
            # not running. The creation response stays truthful.
            db.rollback()
        except Exception:
            with contextlib.suppress(Exception):
                db.rollback()
            logger.exception("Failed to auto-start bot %d", new_bot_id)
        bot = db.get(Bot, new_bot_id)

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
        # Read live state under an admission lease + the state lock (one detached copy), so a
        # dashboard read can never observe a half-applied mutation. ``for_read`` lets a failed
        # (error) generation still be shown. Only a confirmed stopped bot falls back to the
        # persisted file; a retiring/full generation is omitted rather than shown as a stale
        # "stopped" snapshot.
        try:
            with bot_manager.admit(bot.id, for_read=True) as lease:
                _captured_at, result[bot.id] = lease.bot.state_snapshot()
            continue
        except BotNotRunning:
            pass
        except (InstanceRetiring, AdmissionFull, MayHaveExecuted):
            continue
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
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_user_bot(bot_id, user, db)
    from lockbot.backend.app.resource_api.view_links import view_link

    view = view_link(db, bot, str(request.base_url)) if bot.resource_auth_enabled else {}
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    return {**_mask_bot(bot, db), **view}


@router.put("/{bot_id}", response_model=BotOut)
def update_bot(
    bot_id: int,
    request: Request,
    body: BotUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Fast pre-owner existence/permission check; the authoritative change happens under the
    # transition owner below so a config/identity edit cannot commit while a lifecycle transition
    # owns the bot (a competing owner is refused 503), re-validated on a fresh row.
    _get_user_bot(bot_id, user, db)

    with _owned_bot(bot_id, user, db) as bot:
        identity_changed = (body.group_id is not None and body.group_id != bot.group_id) or any(
            getattr(body, key) is not None and getattr(body, key) != encryption.decrypt(getattr(bot, key))
            for key in ("webhook_url", "aes_key", "token")
        )
        if identity_changed:
            try:
                bot_manager.stop_bot(bot_id)
            except BotNotRunning:
                pass
            except MayHaveExecuted:
                raise api_errors.may_have_executed(bot_id) from None
            # End a stale read transaction only after all old-generation writes drained.
            db.rollback()
            bot = _get_user_bot(bot_id, user, db)
            if bot.status != "recovery_required":
                bot.status = "stopped"
            bot.pid = None
            bot.identity_revision += 1
            bot.binding_accept_after = datetime.utcnow()
            bot.observed_group_ids = None
            bot.last_user_id = None
            bot.last_request_at = None

        # Validate NODE_ALIASES keys against cluster_configs
        if body.config_overrides and body.config_overrides.get("NODE_ALIASES"):
            cc = body.cluster_configs if body.cluster_configs is not None else json.loads(bot.cluster_configs)
            _validate_node_aliases(body.config_overrides, cc)

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
            if body.name != bot.name:
                changes["name"] = {"from": bot.name, "to": body.name}
            bot.name = body.name
        if body.group_id is not None:
            bot.group_id = body.group_id
        if body.webhook_url is not None:
            old_val = encryption.decrypt(bot.webhook_url) if bot.webhook_url else ""
            if body.webhook_url != old_val:
                changes["webhook_url"] = "updated"
            bot.webhook_url = encryption.encrypt(body.webhook_url)
        if body.aes_key is not None:
            old_val = encryption.decrypt(bot.aes_key) if bot.aes_key else ""
            if body.aes_key != old_val:
                changes["aes_key"] = "updated"
            bot.aes_key = encryption.encrypt(body.aes_key)
        if body.token is not None:
            old_val = encryption.decrypt(bot.token) if bot.token else ""
            if body.token != old_val:
                changes["token"] = "updated"
            bot.token = encryption.encrypt(body.token)
        if body.cluster_configs is not None:
            # Convert array format to dict for storage
            cc_for_storage = body.cluster_configs
            if _is_device_array_format(cc_for_storage):
                cc_for_storage = _device_array_to_dict(cc_for_storage)
            new_json = json.dumps(cc_for_storage, ensure_ascii=False)
            if new_json != (bot.cluster_configs or ""):
                old_configs = json.loads(bot.cluster_configs) if bot.cluster_configs else None
                if isinstance(cc_for_storage, dict):
                    # DEVICE bot: {node: [devices...]} → summary with device counts
                    node_summary = {k: len(v) if isinstance(v, list) else v for k, v in cc_for_storage.items()}
                    changes["cluster_configs"] = node_summary
                else:
                    # NODE bot: list of node names → show from/to
                    changes["cluster_configs"] = {"from": old_configs, "to": cc_for_storage}
            bot.cluster_configs = new_json
        if body.config_overrides is not None:
            old_overrides = json.loads(bot.config_overrides) if bot.config_overrides else {}
            diff = {}
            for k, v in body.config_overrides.items():
                old_v = old_overrides.get(k)
                if old_v != v:
                    diff[k] = {"from": old_v, "to": v}
            if diff:
                changes["config_overrides"] = diff
            bot.config_overrides = json.dumps(body.config_overrides, ensure_ascii=False)

        if "utilization_config" in body.model_fields_set:
            new_source = serialized_datasource(body.utilization_config)
            if new_source != bot.utilization_config:
                # Do not copy internal source addresses/mappings into the audit detail.
                changes["utilization_config"] = "updated" if new_source else "removed"
                bot.utilization_config = new_source

        # Identity cutover: if the signing credentials change, the bot now authenticates a
        # DIFFERENT IM identity, so agent credentials bound to the old identity must not carry
        # over. Revoke them under the same transition owner and commit (R4). A plain config edit
        # that leaves webhook/aes/token untouched keeps durable credentials, as before.
        if identity_changed:
            from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot

            revoked = revoke_bindings_for_bot(db, bot_id)
            if revoked:
                changes["resource_bindings_revoked"] = revoked

        # Save configuration and the Agent gate atomically. The same authority
        # barrier as the dedicated gate endpoint orders this commit with admission.
        with authority_guard(bot_id):
            if (
                body.resource_api_notify_enabled is not None
                and body.resource_api_notify_enabled != bot.resource_api_notify_enabled
            ):
                changes["resource_api_notify_enabled"] = {
                    "from": bot.resource_api_notify_enabled,
                    "to": body.resource_api_notify_enabled,
                }
                bot.resource_api_notify_enabled = body.resource_api_notify_enabled
            access_changed = (
                body.resource_auth_enabled is not None and body.resource_auth_enabled != bot.resource_auth_enabled
            )
            if access_changed:
                changes["resource_auth_enabled"] = {"from": bot.resource_auth_enabled, "to": body.resource_auth_enabled}
                bot.resource_auth_enabled = body.resource_auth_enabled
            write_audit_log(
                db,
                user,
                "bot.update",
                target_type="bot",
                target_id=bot_id,
                target_name=bot.name,
                detail=changes if changes else {"no_change": True},
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
    # Fast pre-owner existence/permission check; re-validated under the owner below.
    _get_user_bot(bot_id, user, db)

    # Own the lifecycle transition across the stop AND the row/audit commit, and consult the
    # MANAGER, not the DB status: an unpublished start has a stopped row but a live (or
    # in-construction) instance, so gating the stop on ``bot.status`` let a deleted bot keep
    # running. ``_owned_bot`` also re-reads a fresh row under the owner, so a request paused
    # before acquiring ownership cannot delete a bot that was already deleted/reassigned.
    with _owned_bot(bot_id, user, db) as bot:
        bot_name = bot.name
        try:
            bot_manager.stop_bot(bot_id)
        except MayHaveExecuted:
            # An admitted worker may still be mutating and persisting state; deleting the
            # row now would be dishonest. Leave the bot as-is and report the uncertainty.
            raise api_errors.may_have_executed(bot_id) from None
        except RuntimeError:
            pass  # benign: the bot was not actually running in-process

        bot.status = "stopped"
        bot.pid = None
        bot.is_deleted = True
        bot.deleted_at = datetime.utcnow()
        # Identity discontinuity (AC-9): deleting a bot ends its identity, so its durable
        # credentials must stop authenticating immediately. Revoke them under the same owner and
        # transaction as the delete, so no post-delete request can still act as this bot.
        from lockbot.backend.app.resource_auth.service import revoke_bindings_for_bot

        revoke_bindings_for_bot(db, bot_id)
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
    target_username = (body.get("username") or "").strip()
    if not target_username:
        raise HTTPException(status_code=400, detail="username is required")

    # Enrol the ownership transfer in the transition owner: an identity change must not commit
    # while a lifecycle transition (start/stop/restart/delete/edit) owns the bot, or a restart
    # could publish a replacement for the new owner. A competing owner is refused with 503.
    with _owned_bot(bot_id, user, db) as bot:
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
    return {"id": bot_id, "owner": target_username}


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
    # Fast pre-owner existence/permission check (404/403). The authoritative decision is made
    # again under the transition owner below, on a freshly re-read row.
    _get_user_bot(bot_id, user, db)

    with _owned_bot(bot_id, user, db) as bot:
        # A bot fenced into recovery after a failed restore has a possibly-inconsistent config
        # and saved state; refuse every start path until an operator re-runs a clean restore
        # (R1). The status is persisted, so this guard holds across process restarts.
        if bot.status == "recovery_required":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bot needs recovery: a previous restore could not install a consistent "
                    "configuration and saved state. Re-run the restore for this bot before starting."
                ),
            )
        # Consult the MANAGER for liveness, never the DB row: an unpublished start has a stopped
        # row but a live instance, and a stale row could wrongly claim running.
        if bot_manager.is_running(bot_id):
            return BotStatusOut(
                id=bot.id,
                status="running",
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
            pid = bot_manager.start_bot(bot_id, config_dict, activate=False)
        except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
            # A competing lifecycle owner or transient admission refusal. This is NOT a start
            # failure: it must not increment consecutive_failures, write status/audit, or
            # construct anything. Map it to the transient 503 and leave the row untouched.
            raise api_errors.admission_http_error(exc, bot_id) from None
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

        # Commit the initial running status BEFORE activating the generation (B24): while the
        # generation is staged no sweep runs, so no fatal completion can commit 'error' ahead of
        # this status. A failed commit cleans up the staged generation.
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
        _commit_initial_status_then_activate(db, bot_id)
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
    # Fast pre-owner existence/permission check; the authoritative decision is re-made under the
    # transition owner on a freshly re-read row.
    _get_user_bot(bot_id, user, db)

    # Own the lifecycle transition across the stop AND the DB status + audit commit, so a
    # concurrent restart cannot publish a new generation between the manager stop and this
    # route's commit (which would leave the row 'stopped' while a live generation runs).
    with _owned_bot(bot_id, user, db) as bot:
        if bot.status not in ("running", "error"):
            raise HTTPException(status_code=409, detail=f"Bot is not running (status={bot.status})")
        try:
            bot_manager.stop_bot(bot_id)
        except MayHaveExecuted:
            # An admitted worker may still be mutating and persisting state. Preserve the DB
            # lifecycle status (do NOT report stopped) and return the uncertainty contract.
            raise api_errors.may_have_executed(bot_id) from None
        except RuntimeError:
            # Benign: the bot was not actually running in-process. (stop_bot re-enters the owner
            # we already hold, so it cannot raise InstanceRetiring here — only "not running".)
            pass

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
        return BotStatusOut(id=bot.id, status="stopped", message="Bot stopped")


@router.post("/{bot_id}/restart", response_model=BotStatusOut)
@limiter.limit("10/minute")
def restart_bot(
    request: Request,
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Fast pre-owner existence/permission check; re-validated under the owner below.
    _get_user_bot(bot_id, user, db)

    # Own the lifecycle transition across BOTH restart halves and the DB status + audit commit.
    # Crucially, DRAIN the old instance FIRST, then refresh the row and build the replacement
    # configuration: a live edit admitted before the drain (e.g. a language change under an
    # admission lease) must finish and be observed, so the new generation starts from post-drain
    # settings rather than a config snapshot captured before the edit committed.
    with _owned_bot(bot_id, user, db) as bot:
        try:
            bot_manager.stop_bot(bot_id)
        except MayHaveExecuted:
            # The stop half's drain timed out before any replacement was published: an admitted
            # worker may still be running. Report the uncertainty; no new generation started.
            _write_log(bot_id, "重启失败: 上一代操作结果未知，请检查 /resources", level="ERROR")
            raise api_errors.may_have_executed(bot_id) from None
        except RuntimeError:
            pass  # not running in-process; nothing to drain before the start half

        # Re-read the authoritative row AFTER the drain, so a language/state edit that committed
        # while draining is reflected in the replacement configuration, and RE-VALIDATE
        # authorization: an ownership transfer that committed during the drain must not let the
        # original requester publish a replacement for the new owner (B27).
        db.rollback()
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted:
            raise HTTPException(status_code=404, detail="Bot not found")
        if user.role != "super_admin" and bot.user_id != user.id:
            raise HTTPException(status_code=403, detail="Cannot restart another user's bot")
        if bot.status == "recovery_required":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bot needs recovery: a previous restore could not install a consistent "
                    "configuration and saved state. Re-run the restore for this bot before starting."
                ),
            )
        config_dict = _build_config_dict(bot, db)

        try:
            pid = bot_manager.start_bot(bot_id, config_dict, activate=False)
        except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
            raise api_errors.admission_http_error(exc, bot_id) from None
        except RuntimeError as e:
            # The stop half already drained and removed the old instance, so the bot genuinely is
            # not running. Reset the row to stopped/pid None under the owner before failing, so a
            # construction failure never leaves a phantom ``running`` row (B30).
            _reset_row_to_stopped(db, bot_id)
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
        _commit_initial_status_then_activate(db, bot_id)
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

    def _persist_override_and_audit():
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

    def _apply_to_live(lease):
        _persist_override_and_audit()
        if hasattr(lease.bot, "config"):
            lease.bot.config.set_val("LANGUAGE", lang)

    # A live generation applies the edit under an admission lease (counted, drain-aware). When
    # there is no live instance, own the lifecycle transition before persisting: a start in
    # construction owns the transition and refuses this edit up front (transient refusal, no
    # durable effect) rather than letting the edit miss it; a start that has since published is
    # applied to under a fresh lease; a truly stopped bot persists the override for the next
    # start to load. A refused edit (retiring/full) makes no config_overrides commit and no
    # audit record.
    try:
        with bot_manager.admit(bot_id) as lease:
            _apply_to_live(lease)
    except BotNotRunning:
        try:
            with bot_manager.transition(bot_id):
                # Re-validate under the owner on a fresh row: a delete/reassignment may have
                # committed while we waited for ownership, and the pre-owner object is stale.
                db.rollback()
                bot = db.get(Bot, bot_id)  # noqa: F841 — rebinds the cell the closures read
                if bot is None or bot.is_deleted:
                    raise HTTPException(status_code=404, detail="Bot not found")
                if user.role != "super_admin" and bot.user_id != user.id:
                    raise HTTPException(status_code=403, detail="Cannot act on another user's bot")
                try:
                    with bot_manager.admit(bot_id) as lease:
                        _apply_to_live(lease)
                except BotNotRunning:
                    _persist_override_and_audit()
        except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
            raise api_errors.admission_http_error(exc, bot_id) from None
    except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise api_errors.admission_http_error(exc, bot_id) from None

    _write_log(bot_id, f"语言切换为 {lang}")
    return {"id": bot_id, "language": lang}


def _get_bot_data_dir(bot: Bot) -> str:
    """Return the deployment-owned bot directory, never a per-bot override."""
    paths = get_managed_paths()
    paths.state_file(bot.id)
    return str(paths.bot_dir(bot.id))


# ── State endpoints ────────────────────────────────────────


@router.get("/{bot_id}/state")
def get_bot_state(
    bot_id: int,
    include_usage: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import os

    bot = _get_user_bot(bot_id, user, db)

    def view(state, usage=None):
        # Derived task data stays outside editable/persisted resource state.
        return {"state": state, "usage": usage or []} if include_usage else state

    # Prefer the live state, read under an admission lease + the state lock as one detached
    # copy (AC-7). ``for_read`` lets a failed (error) generation still be read. Only a confirmed
    # stopped bot falls back to the persisted file/default; a retiring/full generation returns
    # the structured transient error instead of masking it as a stopped bot.
    try:
        with bot_manager.admit(bot_id, for_read=True) as lease, authority_guard(bot_id):
            db.rollback()
            bot = _get_user_bot(bot_id, user, db)
            captured_at, snapshot = lease.bot.state_snapshot()
            usage = []
            if include_usage:
                from lockbot.backend.app.resource_api.usage import active_declarations, declarations_for_state

                usage = declarations_for_state(active_declarations(db, bot, datetime.utcnow()), snapshot, captured_at)
        return view(snapshot, usage)
    except BotNotRunning:
        pass
    except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise api_errors.admission_http_error(exc, bot_id) from None

    # Bot not running — try loading persisted state file first
    state_file = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                raw = json.load(f)
                state = raw.get("bot_state") or raw.get("cluster_state") or raw.get("cluster_status")
                if state:
                    return view(state)
        except Exception:
            pass

    # No state file — build initial state from cluster_configs
    try:
        cluster_configs = json.loads(bot.cluster_configs)
    except (json.JSONDecodeError, TypeError):
        return view({})

    if bot.bot_type in {"DEVICE", "QUEUE_DEVICE"}:
        return view(
            {
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
        )

    # NODE / QUEUE: default idle state
    return view({name: {"status": "idle", "current_users": [], "booking_list": []} for name in cluster_configs})


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
    if bot_type in {"DEVICE", "QUEUE_DEVICE"}:
        normalized, warnings = _validate_device_state(state, cluster_configs, warnings, config, bot_type)
        if bot_type == "QUEUE_DEVICE":
            for node, devices in normalized.items():
                for device in devices:
                    validated, notices = _validate_node_queue_state({node: device}, {node: "device"}, [], config)
                    device.update(validated[node])
                    warnings.extend(notices)
        return normalized, warnings
    # NODE and QUEUE share the same structure
    return _validate_node_queue_state(state, cluster_configs, warnings, config)


def _default_state_for(cluster_configs, bot_type):
    """Build a fresh default state matching cluster_configs."""
    if bot_type in {"DEVICE", "QUEUE_DEVICE"}:
        return {
            node_key: [
                {
                    "dev_id": i,
                    "dev_model": model,
                    "status": "idle",
                    "current_users": [],
                    **({"booking_list": []} if bot_type == "QUEUE_DEVICE" else {}),
                }
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


def _validate_device_state(state, cluster_configs, warnings, config, bot_type="DEVICE"):
    result = {}
    for node_key, devices in cluster_configs.items():
        if node_key not in state:
            warnings.append(t("state.node_missing", config=config, name=node_key))
            result[node_key] = [
                {
                    "dev_id": i,
                    "dev_model": model,
                    "status": "idle",
                    "current_users": [],
                    **({"booking_list": []} if bot_type == "QUEUE_DEVICE" else {}),
                }
                for i, model in enumerate(devices)
            ]
            continue
        node_state = state[node_key]
        if not isinstance(node_state, list):
            warnings.append(t("state.node_not_list", config=config, name=node_key))
            result[node_key] = [
                {
                    "dev_id": i,
                    "dev_model": model,
                    "status": "idle",
                    "current_users": [],
                    **({"booking_list": []} if bot_type == "QUEUE_DEVICE" else {}),
                }
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
    import copy
    import os

    bot = _get_user_bot(bot_id, user, db)

    # Admin can only edit own bot state, super_admin can edit any
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Cannot edit another user's bot state")

    if bot.status == "running":
        raise HTTPException(status_code=409, detail="Stop the bot before editing state")

    from lockbot.core.config import Config

    # Preserve the untouched request payload: the offline path re-derives validation, config and
    # the state-file target from the FRESH row under the owner (B25), because a config edit that
    # commits before we acquire ownership changes cluster keys and must not be lost.
    raw_state = state

    def _derive(bot):
        """Compute (aligned_state, warnings, state_file) for a bot row, from the raw payload.

        Deep-copies the raw payload because alignment mutates nested structures in place, so the
        raw request stays intact for this single derivation under the owner (or lease).
        """
        cluster_configs = _normalize_cluster_configs(json.loads(bot.cluster_configs))
        config_obj = Config(_build_config_dict(bot, db))
        aligned, warns = _validate_and_align_state(copy.deepcopy(raw_state), bot.bot_type, cluster_configs, config_obj)
        target = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
        return aligned, warns, target

    # Derived exactly once, inside whichever path runs (live lease or offline owner), so there is
    # no authoritative pre-owner derivation that a concurrent config writer could invalidate (B25).
    state = {}
    warnings = []
    state_file = ""

    import tempfile

    def _persist_state_file():
        """Atomically replace bot_state.json. Called while the admission lease + state lock are
        held for a live bot, so a lifecycle drain cannot remove the generation mid-write."""
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(state_file), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"bot_state": state}, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, state_file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _audit_and_commit():
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

    def _apply_live(lease):
        # Hold the bot state lock only across the in-memory assign + atomic file write, so the
        # snapshot/file stay consistent; the audit INSERT + commit then run under the lease but
        # OUTSIDE the state lock, so the DB round-trip does not widen the state-lock window the
        # expiry sweep and chat commands contend on. Both still run inside the lease, so the
        # audit/commit never trails it.
        with lease.bot._lock:
            lease.bot.state.bot_state = state
            _persist_state_file()
        _audit_and_commit()

    # This route edits persisted state and requires a non-running bot (running bots are 409'd
    # above). A live generation applies under a lease (assign + file write under the state lock,
    # audit/commit under the lease). A retained *failed* generation must NOT be edited on disk
    # underneath a still-held read lease (the retained snapshot and file would disagree); the
    # operator must stop/restart it first. When there is no live instance, the edit owns the
    # lifecycle transition before persisting: a start in construction owns the transition and
    # refuses this edit up front (transient refusal, no durable effect) rather than letting a
    # full-cap reconcile silently suppress AdmissionFull; a start that has since published is
    # applied to under a fresh lease; a truly stopped bot writes the file for the next start.
    try:
        with bot_manager.admit(bot_id) as lease:
            state, warnings, state_file = _derive(bot)  # noqa: F841 — rebinds the cells closures read
            _apply_live(lease)
    except BotNotRunning:
        if bot_manager.is_failed(bot_id):
            raise api_errors.instance_retiring(bot_id) from None
        try:
            with bot_manager.transition(bot_id):
                # Re-validate under the owner on a fresh row: a delete/reassignment or a config
                # edit may have committed while we waited for ownership.
                db.rollback()
                bot = db.get(Bot, bot_id)  # noqa: F841 — rebinds the cell the closures read
                if bot is None or bot.is_deleted:
                    raise HTTPException(status_code=404, detail="Bot not found")
                if user.role != "super_admin" and bot.user_id != user.id:
                    raise HTTPException(status_code=403, detail="Cannot act on another user's bot")
                # Recompute validation/alignment/warnings and the state-file target from the FRESH
                # row (B25): the enclosing names the closures read are rebound here.
                state, warnings, state_file = _derive(bot)  # noqa: F841
                try:
                    with bot_manager.admit(bot_id) as lease:
                        _apply_live(lease)
                except BotNotRunning:
                    if bot_manager.is_failed(bot_id):
                        raise api_errors.instance_retiring(bot_id) from None
                    _persist_state_file()
                    _audit_and_commit()
        except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
            raise api_errors.admission_http_error(exc, bot_id) from None
    except (InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise api_errors.admission_http_error(exc, bot_id) from None

    result = {"message": "State updated"}
    if warnings:
        result["warnings"] = warnings
    return result


# ── Logs endpoints ─────────────────────────────────────────


@router.get("/{bot_id}/logs")
def get_bot_logs(
    bot_id: int,
    page: int = 1,
    limit: int = 50,
    level: str | None = None,
    category: str | None = None,
    since: str | None = None,
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
    if since:
        entries = [e for e in entries if e.get("created_at", "") > since]

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
    # Parse request data (matching Flask's request interface)
    body = await request.body()
    form = {}
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        form_data = await request.form()
        form = dict(form_data)

    args = dict(request.query_params)

    # Dispatch under an admission lease so a lifecycle drain waits for this webhook and a
    # replacement instance is never handed stale work. The synchronous handler (signature
    # check, decrypt, command execution, reply send) runs off the event loop. A retiring,
    # stopped, or over-full bot falls back to the friendly "not running" reply.
    metadata_session = sessionmaker(bind=db.get_bind())

    def _dispatch():
        with bot_manager.admit(bot_id) as lease:
            text, code, meta = handle_webhook(lease.bot, raw_form=form, raw_args=args, raw_body=body)
            if code == 200 and not form.get("echostr") and bot_manager.lease_is_current(lease):
                with metadata_session() as worker_db:
                    row = worker_db.get(Bot, bot_id)
                    if row is not None and not row.is_deleted:
                        row.last_request_at = datetime.utcnow()
                        if meta.get("group_id"):
                            observed = set((row.observed_group_ids or "").split(",")) - {""}
                            observed.add(str(meta["group_id"]))
                            from lockbot.backend.app.broadcast.service import record_group_activity

                            record_group_activity(worker_db, row, meta["group_id"])
                            row.observed_group_ids = ",".join(sorted(observed))[:4096]
                        if meta.get("user_id"):
                            row.last_user_id = str(meta["user_id"])[:128]
                        worker_db.commit()
            return text, code, meta

    try:
        text, code, meta = await asyncio.to_thread(_dispatch)
    except (BotNotRunning, InstanceRetiring, AdmissionFull):
        return await _reply_bot_not_running(bot_id, form, args, body, db)
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("Webhook handler crashed for bot %d", bot_id)
        _write_log(bot_id, f"Webhook 处理异常: {e}\n{tb}", level="ERROR")
        return PlainTextResponse(content="internal error", status_code=500)

    # URL verification: Ruliu expects raw echostr as plain text, not JSON
    if form.get("echostr"):
        return PlainTextResponse(content=text, status_code=code)

    # Record the processed command (observability only; keyed by bot_id, not identity).
    uid = meta.get("user_id")
    if meta.get("command"):
        _write_log(
            bot_id,
            f"[{uid or 'unknown'}] {redact_command_text(meta['command'])}",
            category="command",
            source=SOURCE_CHAT,
        )

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
    if not bot or bot.is_deleted:
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
    adapter = create_adapter(bot.platform, config=config)

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
    if bot.status == "error":
        content = t("webhook.bot_error", lang=lang, bot_name=bot.name, bot_id=bot_id, owner_username=owner_username)
    else:
        content = t(
            "webhook.bot_not_running", lang=lang, bot_name=bot.name, bot_id=bot_id, owner_username=owner_username
        )
    reply = adapter.build_reply(content, [user_id], group_id=group_id)

    try:
        adapter.send(reply)
    except Exception:
        logger.exception("Failed to send 'bot not running' reply for bot %d", bot_id)

    return PlainTextResponse(content="bot not running", status_code=200)


# ── API Key management + locked-users endpoint ──────────────────────────


def _generate_api_key() -> str:
    """Generate a new API key."""
    return "lbk_" + secrets.token_hex(24)


def _verify_api_key(bot: Bot, token: str) -> bool:
    """Verify a Bearer token against the stored key."""
    if not bot.api_key or not token:
        return False
    return token == bot.api_key


@router.post("/{bot_id}/api-key")
def generate_api_key(
    bot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate/reset API key for a bot. Only owner or super_admin."""
    bot = _get_user_bot(bot_id, user, db)
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only owner can manage API key")
    raw_key = _generate_api_key()
    # Serialize the credential write with lifecycle transitions: own the bot, re-validate the
    # fresh row under the owner, then write. A competing transition surfaces as a transient 503.
    with _owned_bot(bot_id, user, db) as bot:
        bot.api_key = raw_key
        db.commit()
    return {"api_key": raw_key, "message": "Save this key now; it won't be shown again."}


@router.delete("/{bot_id}/api-key", status_code=204)
def revoke_api_key(
    bot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke API key for a bot."""
    bot = _get_user_bot(bot_id, user, db)
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only owner can manage API key")
    with _owned_bot(bot_id, user, db) as bot:
        bot.api_key = None
        db.commit()


# ── Agent API enrollment (resource auth) ───────────────────────────────────


def _require_bot_owner(bot_id: int, user: User, db: Session, action: str) -> Bot:
    """Fetch a bot the caller owns, or is super_admin over. Otherwise 403/404."""
    bot = _get_user_bot(bot_id, user, db)
    if user.role != "super_admin" and bot.user_id != user.id:
        raise HTTPException(status_code=403, detail=f"Only the bot owner can {action}")
    return bot


@router.put("/{bot_id}/resource-auth")
def set_resource_auth_enabled(
    bot_id: int,
    body: ResourceAuthToggle,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Control Agent access and new enrollment. Disabling suspends, without revoking keys."""
    _require_bot_owner(bot_id, user, db, "manage Agent access")
    with _owned_bot(bot_id, user, db) as bot, authority_guard(bot_id):
        db.rollback()
        bot = _require_bot_owner(bot_id, user, db, "manage Agent access")
        if bot.resource_auth_enabled == bool(body.enabled):
            return {"bot_id": bot_id, "resource_auth_enabled": bot.resource_auth_enabled}
        previous = bot.resource_auth_enabled
        bot.resource_auth_enabled = bool(body.enabled)
        write_audit_log(
            db,
            user,
            "bot.update",
            target_type="bot",
            target_id=bot.id,
            target_name=bot.name,
            detail={"resource_auth_enabled": {"from": previous, "to": bot.resource_auth_enabled}},
            ip=request.client.host if request.client else None,
        )
        db.commit()
        enabled = bot.resource_auth_enabled
    return {"bot_id": bot_id, "resource_auth_enabled": enabled}


@router.delete("/{bot_id}/resource-auth/bindings")
def revoke_all_resource_bindings(
    bot_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke every agent credential scoped to this bot. Takes effect on the next request."""
    from lockbot.backend.app.resource_auth import service as resource_auth_service

    _require_bot_owner(bot_id, user, db, "revoke agent credentials")
    with _owned_bot(bot_id, user, db) as bot, authority_guard(bot_id):
        db.rollback()
        bot = _require_bot_owner(bot_id, user, db, "revoke agent credentials")
        revoked = resource_auth_service.revoke_bindings_for_bot(db, bot.id)
        write_audit_log(
            db,
            user,
            "bot.resource_auth_revoke_all",
            target_type="bot",
            target_id=bot.id,
            target_name=bot.name,
            detail={"revoked": revoked},
            ip=request.client.host if request.client else None,
        )
        db.commit()
    return {"bot_id": bot_id, "revoked": revoked}


@router.get("/{bot_id}/locked-users", tags=["Public API"], response_model=LockedUsersResponse)
@limiter.limit("60/minute")
def get_locked_users(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Query current lock holders. Requires `Authorization: Bearer lbk_xxx`."""
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Verify API key from Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key")
    token = auth_header[7:]
    if not _verify_api_key(bot, token):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Read state under an admission lease + the state lock, as one detached copy. ``for_read``
    # lets a failed (error) generation still be read; the lease means this read cannot observe a
    # retiring/replaced generation; reading the live dict without the lock could observe a
    # multi-resource mutation half-applied.
    try:
        with bot_manager.admit(bot_id, for_read=True) as lease:
            _captured_at, state = lease.bot.state_snapshot()
    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise api_errors.admission_http_error(exc, bot_id, legacy_not_running=True) from None
    bot_type = bot.bot_type

    # Return state organized by node -> current_users
    nodes = {}
    if bot_type in {"DEVICE", "QUEUE_DEVICE"}:
        for node_key, devices in state.items():
            node_devs = []
            for dev in devices:
                node_devs.append(
                    {
                        "dev_id": dev.get("dev_id"),
                        "status": dev.get("status", "idle"),
                        "current_users": [
                            {"user_id": u["user_id"], "start_time": u["start_time"], "duration": u["duration"]}
                            for u in dev.get("current_users", [])
                        ],
                    }
                )
            nodes[node_key] = node_devs
    else:
        for node_key, node in state.items():
            nodes[node_key] = {
                "status": node.get("status", "idle"),
                "current_users": [
                    {"user_id": u["user_id"], "start_time": u["start_time"], "duration": u["duration"]}
                    for u in node.get("current_users", [])
                ],
            }

    return {"bot_id": bot_id, "bot_type": bot_type, "nodes": nodes}


@router.post("/{bot_id}/opkick", tags=["Public API"], response_model=OpkickResponse)
@limiter.limit("30/minute")
def api_opkick(
    bot_id: int,
    request: Request,
    body: OpkickRequest,
    db: Session = Depends(get_db),
):
    """Operator kick via API. Requires Bearer API key.

    - target_user: required, the user to kick
    - node_key: optional, if omitted kicks from all nodes
    - dev: optional, only for DEVICE bots (device indices)
    - reason: optional, shown in notification to kicked user
    """
    bot = db.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key")
    if not _verify_api_key(bot, auth_header[7:]):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Run the kick under an admission lease, held across the whole mutation (which persists
    # state and may notify), so it cannot land on a retiring/replaced generation and a drain
    # waits for it.
    try:
        with bot_manager.admit(bot_id) as lease:
            result = lease.bot.do_opkick(body.target_user, node_key=body.node_key, dev_ids=body.dev, reason=body.reason)
    except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as exc:
        raise api_errors.admission_http_error(exc, bot_id, legacy_not_running=True) from None
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None

    freed_str = ", ".join(result["freed"])
    _write_log(
        bot_id,
        f"[API] opkick {body.target_user} -> {freed_str}",
        category="command",
        source=SOURCE_SERVICE_KEY,
    )
    return result
