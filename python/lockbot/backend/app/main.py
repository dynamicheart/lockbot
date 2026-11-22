"""
FastAPI application entry point
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from html import escape
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

# Import models to register them with Base.metadata
import lockbot.backend.app.audit.models  # noqa: F401
import lockbot.backend.app.auth.models  # noqa: F401
import lockbot.backend.app.bots.models  # noqa: F401
import lockbot.backend.app.broadcast.models  # noqa: F401
import lockbot.backend.app.feishu.models  # noqa: F401
import lockbot.backend.app.platform_applications.service  # noqa: F401
import lockbot.backend.app.resource_auth.models  # noqa: F401
import lockbot.backend.app.settings.models  # noqa: F401
from lockbot.backend.app.admin.router import router as admin_router
from lockbot.backend.app.audit.delegation import router as audit_delegation_router
from lockbot.backend.app.audit.router import router as audit_router
from lockbot.backend.app.auth.router import router as auth_router
from lockbot.backend.app.backup.router import router as backup_router
from lockbot.backend.app.bots.delegation import router as configuration_delegation_router
from lockbot.backend.app.bots.router import router as bots_router
from lockbot.backend.app.database import Base, SessionLocal, engine
from lockbot.backend.app.platform_applications.router import router as platform_applications_router
from lockbot.backend.app.resource_api.activity import install_activity_middleware
from lockbot.backend.app.resource_api.activity import router as activity_router
from lockbot.backend.app.resource_api.commands import agent_router as agent_commands_router
from lockbot.backend.app.resource_api.commands import router as resource_commands_router
from lockbot.backend.app.resource_api.operator import router as operator_router
from lockbot.backend.app.resource_api.resources import agent_router as agent_resources_router
from lockbot.backend.app.resource_api.resources import router as resources_router
from lockbot.backend.app.resource_api.skill import router as skill_router
from lockbot.backend.app.resource_api.usage import agent_router as agent_usage_router
from lockbot.backend.app.resource_api.usage import router as usage_router
from lockbot.backend.app.resource_auth.router import router as resource_auth_router
from lockbot.backend.app.settings.router import router as settings_router
from lockbot.backend.app.static import SPAStaticFiles

# Configure lockbot loggers to output alongside uvicorn logs
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s:%(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


def _migrate_bot_logs_category():
    """Add 'category' column to bot_logs if it doesn't exist (SQLite migration)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    columns = [c["name"] for c in insp.get_columns("bot_logs")]
    if "category" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bot_logs ADD COLUMN category VARCHAR(16) NOT NULL DEFAULT 'system'"))
        logger.info("Migrated bot_logs: added 'category' column")


def _migrate_bot_consecutive_failures():
    """Add 'consecutive_failures' column to bots if it doesn't exist (SQLite migration)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    columns = [c["name"] for c in insp.get_columns("bots")]
    if "consecutive_failures" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"))
        logger.info("Migrated bots: added 'consecutive_failures' column")


def _migrate_users_must_change_password():
    """Add 'must_change_password' column to users if it doesn't exist (SQLite migration)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    if "users" not in insp.get_table_names():
        return
    columns = [c["name"] for c in insp.get_columns("users")]
    if "must_change_password" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"))
        logger.info("Migrated users: added 'must_change_password' column")


def _migrate_bot_soft_delete():
    """Add 'is_deleted' and 'deleted_at' columns to bots if they don't exist (SQLite migration)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    if "bots" not in insp.get_table_names():
        return
    columns = [c["name"] for c in insp.get_columns("bots")]
    if "is_deleted" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0"))
        logger.info("Migrated bots: added 'is_deleted' column")
    if "deleted_at" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN deleted_at DATETIME"))
        logger.info("Migrated bots: added 'deleted_at' column")


def _migrate_users_token_version():
    """Add 'token_version' column to users if it doesn't exist (SQLite migration)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    if "users" not in insp.get_table_names():
        return
    columns = [c["name"] for c in insp.get_columns("users")]
    if "token_version" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"))
        logger.info("Migrated users: added 'token_version' column")


def _migrate_audit_logs():
    """Create audit_logs table if it doesn't exist (backward-compatible migration)."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if "audit_logs" not in insp.get_table_names():
        Base.metadata.tables["audit_logs"].create(bind=engine)
        logger.info("Created audit_logs table")


def _migrate_bots_api_key():
    """Add 'api_key' column to bots if it doesn't exist."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    columns = [c["name"] for c in insp.get_columns("bots")]
    if "api_key" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN api_key VARCHAR(128)"))
        logger.info("Migrated bots: added 'api_key' column")


def _migrate_bots_uuid():
    """Add, backfill, and uniquely index the stable uuid column for bots.

    The uuid is the identity a restore matches on, so it must be unique across the WHOLE table
    (tombstones included). The index is created only after backfilling NULLs and AFTER verifying
    there are no pre-existing duplicates — which this migration must never resolve by deleting,
    overwriting, or reassigning a uuid. Duplicates are surfaced for manual repair instead. The
    whole function is idempotent: a second run finds the column present, no NULLs, and the index
    already created.
    """
    import uuid

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    columns = [c["name"] for c in insp.get_columns("bots")]
    if "uuid" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN uuid VARCHAR(36)"))
        logger.info("Migrated bots: added 'uuid' column")

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id FROM bots WHERE uuid IS NULL OR uuid = ''")).fetchall()
        for row in rows:
            conn.execute(
                text("UPDATE bots SET uuid = :uuid WHERE id = :id"),
                {"uuid": str(uuid.uuid4()), "id": row.id},
            )
        if rows:
            logger.info("Migrated bots: backfilled uuid for %d bot(s)", len(rows))

    # Already indexed? Nothing more to do (idempotent).
    insp = sa_inspect(engine)
    indexes = {ix["name"]: ix for ix in insp.get_indexes("bots")}
    if "ix_bots_uuid_unique" in indexes:
        index = indexes["ix_bots_uuid_unique"]
        if (
            not index.get("unique")
            or index.get("column_names") != ["uuid"]
            or index.get("dialect_options", {}).get("sqlite_where") is not None
        ):
            raise RuntimeError("Invalid ix_bots_uuid_unique definition; operator repair required")
        return

    # Refuse to build a unique index over existing duplicates. Do NOT pick a winner, delete a
    # tombstone, or reassign a uuid: that would silently rewrite identity. Report and stop so an
    # operator resolves it deliberately. (Historical data predating this constraint.)
    with engine.begin() as conn:
        dupes = conn.execute(
            text(
                "SELECT uuid, COUNT(*) AS c FROM bots WHERE uuid IS NOT NULL AND uuid != '' GROUP BY uuid HAVING c > 1"
            )
        ).fetchall()
    if dupes:
        detail = ", ".join(f"{row.uuid} (x{row.c})" for row in dupes)
        raise RuntimeError(
            "Cannot enforce bot uuid uniqueness: duplicate uuids already exist "
            f"[{detail}]. Resolve them manually (purge or re-key the extra rows) and restart; "
            "this migration will not delete, overwrite, or reassign any uuid automatically."
        )
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX ix_bots_uuid_unique ON bots(uuid)"))
    logger.info("Migrated bots: created unique index on uuid")


def _migrate_bots_resource_auth_enabled():
    """Add 'resource_auth_enabled' column to bots if it doesn't exist.

    Defaults to enabled on schemas predating the setting; existing explicit values
    are never overwritten.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    insp = sa_inspect(engine)
    columns = [c["name"] for c in insp.get_columns("bots")]
    if "resource_auth_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN resource_auth_enabled BOOLEAN NOT NULL DEFAULT 1"))
        logger.info("Migrated bots: added 'resource_auth_enabled' column")


def _migrate_bots_agent_notifications():
    """Preserve current notification behavior when adding the optional switch."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    if "resource_api_notify_enabled" not in {c["name"] for c in sa_inspect(engine).get_columns("bots")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN resource_api_notify_enabled BOOLEAN NOT NULL DEFAULT 1"))


def _migrate_bots_utilization_config():
    """Add optional browser data-source configuration without changing existing bots."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    if "utilization_config" not in {c["name"] for c in sa_inspect(engine).get_columns("bots")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bots ADD COLUMN utilization_config TEXT"))


def _migrate_bots_node_metadata():
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    columns = {column["name"] for column in sa_inspect(engine).get_columns("bots")}
    for name in ("node_access_config", "node_hardware_config"):
        if name not in columns:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE bots ADD COLUMN {name} TEXT"))


def _migrate_configuration_credential_deletion():
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    if "deleted_at" not in {c["name"] for c in sa_inspect(engine).get_columns("configuration_credentials")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE configuration_credentials ADD COLUMN deleted_at DATETIME"))


def _migrate_usage_agent_label():
    """Separate a per-resource Agent declaration from the shared authorization label."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    inspector = sa_inspect(engine)
    if "resource_usage_declarations" not in inspector.get_table_names():
        return
    if "agent_label" not in {c["name"] for c in inspector.get_columns("resource_usage_declarations")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE resource_usage_declarations ADD COLUMN agent_label VARCHAR(64)"))


def _migrate_usage_task_category():
    """Add a nullable public category; legacy free text remains private and untouched."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    inspector = sa_inspect(engine)
    if "resource_usage_declarations" in inspector.get_table_names() and "task_category" not in {
        col["name"] for col in inspector.get_columns("resource_usage_declarations")
    }:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE resource_usage_declarations ADD COLUMN task_category VARCHAR(48)"))


def _migrate_resource_bindings():
    """Create resource_bindings table if it doesn't exist (backward-compatible migration)."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if "resource_bindings" not in insp.get_table_names():
        Base.metadata.tables["resource_bindings"].create(bind=engine)
        logger.info("Created resource_bindings table")


def _migrate_binding_uniqueness():
    """Enforce one unrevoked credential per verified user and bot, without choosing winners."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    name = "uq_resource_binding_live_principal_bot"
    columns = ["platform", "external_user_id", "bound_bot_id"]
    indexes = {ix["name"]: ix for ix in sa_inspect(engine).get_indexes("resource_bindings")}
    if name in indexes:
        ix = indexes[name]
        predicate = str(ix.get("dialect_options", {}).get("sqlite_where", ""))
        if (
            not ix.get("unique")
            or ix.get("column_names") != columns
            or predicate.strip().lower() != "revoked_at is null"
        ):
            raise RuntimeError(f"Invalid {name} definition; operator repair required")
        return
    with engine.begin() as conn:
        duplicates = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM resource_bindings WHERE revoked_at IS NULL "
                "GROUP BY platform, external_user_id, bound_bot_id HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if duplicates:
            raise RuntimeError(
                "Duplicate active user/bot authorizations exist. Explicitly revoke the obsolete "
                "bindings before restarting; migration will not choose or revoke credentials automatically."
            )
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX {name} ON resource_bindings(platform, external_user_id, bound_bot_id) "
                "WHERE revoked_at IS NULL"
            )
        )


def _migrate_identity_boundaries():
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    additions = {
        "bots": {
            "identity_revision": "INTEGER NOT NULL DEFAULT 0",
            "binding_accept_after": "DATETIME",
            "observed_group_ids": "TEXT",
        },
        "resource_bindings": {
            "bound_identity_revision": "INTEGER NOT NULL DEFAULT 0",
            "credential_encrypted": "TEXT",
            "credential_generation": "INTEGER NOT NULL DEFAULT 0",
        },
        "bot_migrations": {"metrics_wait_seconds": "INTEGER"},
    }
    inspector = sa_inspect(engine)
    for table, fields in additions.items():
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl in fields.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _seed_dev_admin():
    """Create admin user in dev mode if it doesn't exist."""
    from lockbot.backend.app.config import (
        DEV_ADMIN_EMAIL,
        DEV_ADMIN_PASSWORD,
        DEV_ADMIN_USERNAME,
        DEV_MODE,
    )

    if not DEV_MODE:
        return

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == DEV_ADMIN_USERNAME).first()
        if existing:
            logger.debug("Dev admin '%s' already exists, skipping seed.", DEV_ADMIN_USERNAME)
            return
        admin = User(
            username=DEV_ADMIN_USERNAME,
            email=DEV_ADMIN_EMAIL,
            password_hash=_hash_password(DEV_ADMIN_PASSWORD),
            role="super_admin",
        )
        db.add(admin)
        db.commit()
        logger.info(
            "Dev admin created: username='%s', password='%s'",
            DEV_ADMIN_USERNAME,
            DEV_ADMIN_PASSWORD,
        )
    finally:
        db.close()


def _seed_dev_users():
    """Create test users in dev mode if they don't exist."""
    from lockbot.backend.app.config import DEV_MODE

    if not DEV_MODE:
        return

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password

    test_users = [
        ("admin", "admin@lockbot.dev", "super_admin"),
        ("manager", "manager@lockbot.dev", "admin"),
        ("user1", "user1@lockbot.dev", "user"),
        ("user2", "user2@lockbot.dev", "user"),
    ]

    db = SessionLocal()
    try:
        for username, email, role in test_users:
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                logger.debug("Dev user '%s' already exists, skipping.", username)
                continue
            user = User(
                username=username,
                email=email,
                password_hash=_hash_password(username),
                role=role,
            )
            db.add(user)
            logger.info("Dev user created: username='%s', password='%s', role='%s'", username, username, role)
        db.commit()
    finally:
        db.close()


def _acquire_state_store_ownership():
    """Fail fast if another process already owns the bot state store.

    Skipped when LOCKBOT_SKIP_STATE_OWNERSHIP is set, which the test suite does because it
    builds many apps in one process against temporary data directories.
    """
    import os

    from lockbot.backend.app.config import DATABASE_URL
    from lockbot.backend.app.managed_paths import get_managed_paths
    from lockbot.backend.app.state_ownership import acquire_database_ownership, acquire_state_store_ownership

    if os.environ.get("LOCKBOT_SKIP_STATE_OWNERSHIP"):
        logger.warning("Skipping bot state store ownership lock (LOCKBOT_SKIP_STATE_OWNERSHIP set)")
        return
    acquire_state_store_ownership(str(get_managed_paths().root))
    acquire_database_ownership(DATABASE_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Take exclusive ownership of the bot state store before touching any state. This is a
    # hard precondition, not a nicety: lock state lives in this process's memory and the
    # pending-binding store is in-process, so a second process would corrupt both.
    _acquire_state_store_ownership()

    Base.metadata.create_all(bind=engine)
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id

    with SessionLocal() as identity_db:
        ensure_instance_id(identity_db)
    _migrate_bot_logs_category()
    _migrate_bot_consecutive_failures()
    _migrate_users_must_change_password()
    _migrate_users_token_version()
    _migrate_bot_soft_delete()
    _migrate_audit_logs()
    from lockbot.backend.app.bot_audit.service import migrate_binding_audit

    migrate_binding_audit(engine)
    _migrate_bots_api_key()
    _migrate_bots_uuid()
    _migrate_bots_resource_auth_enabled()
    _migrate_bots_agent_notifications()
    _migrate_bots_utilization_config()
    _migrate_bots_node_metadata()
    _migrate_configuration_credential_deletion()
    _migrate_usage_agent_label()
    _migrate_usage_task_category()
    _migrate_resource_bindings()
    _migrate_identity_boundaries()
    _migrate_binding_uniqueness()
    from lockbot.backend.app.platform_applications.service import backfill

    backfill(SessionLocal, strict=False)
    from lockbot.backend.app.managed_paths import get_managed_paths
    from lockbot.backend.app.state_migration import assert_state_store_ready, migrate_legacy_state_files

    with SessionLocal() as db:
        migrated = migrate_legacy_state_files(db, get_managed_paths())
        if migrated:
            logger.info("Migrated legacy state filenames for bots %s", migrated)
        assert_state_store_ready(db, get_managed_paths())
    _seed_dev_admin()
    _seed_dev_users()
    from lockbot.backend.app.bots.manager import bot_manager
    from lockbot.backend.app.feishu.proxy_settings import restore_proxy

    with SessionLocal() as db:
        restore_proxy(db)

    from lockbot.backend.app.resource_api.idle_observation import configured_observer

    observer = configured_observer(bot_manager)
    app.state.idle_observer = observer
    bot_manager.idle_confirmation_hook = getattr(observer, "confirm_chat", None)
    bot_manager.start_scheduler()
    _reset_running_bots()

    from lockbot.backend.app.feishu.connectors import connectors

    connectors.start(factory=SessionLocal)

    from lockbot.backend.app.backup.scheduler import backup_scheduler

    backup_scheduler.start()

    from lockbot.backend.app.broadcast.service import broadcast_worker
    from lockbot.backend.app.resource_api.notifications import notification_budget
    from lockbot.backend.app.resource_api.usage_cleanup import usage_cleanup_loop

    notification_budget.start()
    broadcast_worker.start()
    usage_cleanup = asyncio.create_task(usage_cleanup_loop())
    idle_observation = asyncio.create_task(observer.run()) if observer else None
    app.state.service_history.start()
    try:
        yield
    finally:
        if idle_observation is not None:
            idle_observation.cancel()
            with suppress(asyncio.CancelledError):
                await idle_observation
        usage_cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await usage_cleanup
        await asyncio.to_thread(connectors.stop)
        logger.info("Shutting down all bots…")
        notification_budget.stop()
        broadcast_worker.stop()
        backup_scheduler.stop()
        bot_manager.shutdown_all()
        bot_manager.idle_confirmation_hook = None
        await asyncio.to_thread(app.state.service_history.stop)
        reports = getattr(app.state, "agent_personal_reports", None)
        if reports is not None:
            reports.close()


def _reset_running_bots():
    """On process startup, try to auto-recover bots that were previously running.

    For each bot that was 'running' or 'error' before the process died, attempt to
    restart it in-process. If restart fails, reset to 'stopped' and clear error state.
    """
    import contextlib

    from lockbot.backend.app.bots.manager import InstanceRetiring, bot_manager
    from lockbot.backend.app.bots.migration_journal import migration_fence
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.bots.router import _build_config_dict

    def _try_recover(bot_id, db):
        """Recover a single bot by id. Returns True on success.

        Uses the same fresh-input, staged-start protocol as the routes: under the transition
        owner, re-read the row, skip a candidate that was deleted or is no longer eligible, commit
        the initial running status (even for an ``error`` candidate) BEFORE activating the sweep,
        and clean up on failure. A competing owner leaves no durable recovery effect.
        """
        try:
            with bot_manager.transition(bot_id):
                db.rollback()
                bot = db.get(Bot, bot_id)
                if bot is None or bot.is_deleted or bot.status not in ("running", "error"):
                    return False  # deleted / no longer eligible since selection
                if migration_fence(db, bot_id) is not None:
                    logger.info("Auto-recovery skipped for migration-fenced bot %d", bot_id)
                    return False
                config_dict = _build_config_dict(bot, db)
                pid = bot_manager.start_bot(bot_id, config_dict, activate=False)
                bot.status = "running"
                bot.pid = pid
                bot.consecutive_failures = 0
                try:
                    db.commit()
                except Exception:
                    with contextlib.suppress(Exception):
                        bot_manager.stop_bot(bot_id)
                    raise
                try:
                    bot_manager.activate_bot(bot_id)
                except Exception:
                    with contextlib.suppress(Exception):
                        bot_manager.stop_bot(bot_id)
                    raise
            logger.info("Auto-recovered bot %d", bot_id)
            return True
        except InstanceRetiring:
            # Another lifecycle transition owns the bot; leave it untouched, no durable effect.
            db.rollback()
            return False
        except Exception:
            logger.exception("Failed to auto-recover bot %d, resetting to stopped", bot_id)
            # Compensate under a re-acquired owner, and ONLY if nothing else has (re)started the
            # bot in the meantime: a competing start that published while our construct was failing
            # must not be overwritten back to stopped (B29). "Published" covers both a live
            # generation (is_running) AND a newer generation that started and then failed
            # (is_failed) — the latter must keep its ``error`` status, not be reset to stopped. A
            # competing owner => no durable effect.
            try:
                with bot_manager.transition(bot_id):
                    if not bot_manager.is_running(bot_id) and not bot_manager.is_failed(bot_id):
                        db.rollback()
                        bot = db.get(Bot, bot_id)
                        if bot is not None and not bot.is_deleted:
                            bot.status = "stopped"
                            bot.pid = None
                            db.commit()
            except InstanceRetiring:
                db.rollback()
            return False

    db = SessionLocal()
    try:
        # Collect candidate IDs (not trusted ORM snapshots): each is re-read fresh under its owner.
        candidate_ids = [
            b.id for b in db.query(Bot).filter(Bot.status.in_(["running", "error"]), Bot.is_deleted.is_(False)).all()
        ]
        if not candidate_ids:
            return
        logger.info("Attempting to auto-recover %d bot(s)…", len(candidate_ids))
        for bot_id in candidate_ids:
            # Isolate each bot: an unexpected failure in one candidate (e.g. a compensation
            # commit that itself raises) must not abort recovery of the others (B31). The shared
            # session is reset so the next candidate's fresh read under its owner is clean.
            try:
                _try_recover(bot_id, db)
            except Exception:
                logger.exception("Auto-recovery of bot %d aborted; continuing with others", bot_id)
                try:
                    db.rollback()
                except Exception:
                    # The session is poisoned and even rollback failed. Discard it and give the
                    # next candidate a clean session, so one bad session cannot silently abort
                    # every remaining recovery attempt (R4).
                    logger.exception("Auto-recovery session unusable; replacing it for remaining bots")
                    with contextlib.suppress(Exception):
                        db.close()
                    db = SessionLocal()
    finally:
        db.close()


def create_app() -> FastAPI:
    from importlib.metadata import version as _pkg_version

    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from lockbot.backend.app.rate_limit import limiter

    try:
        _ver = _pkg_version("lockbot")
    except Exception:
        _ver = "unknown"
    _dev_mode = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")
    app = FastAPI(
        title="LockBot Platform",
        version=_ver,
        lifespan=lifespan,
        docs_url="/docs" if _dev_mode else None,
        redoc_url="/redoc" if _dev_mode else None,
    )

    from lockbot.backend.app.deployment import install_health_endpoint

    install_activity_middleware(app)
    from lockbot.backend.app.resource_api.ingress import AgentIngressMiddleware

    app.add_middleware(AgentIngressMiddleware)
    install_health_endpoint(app)
    from lockbot.backend.app.resource_api.errors import install_agent_validation_errors
    from lockbot.backend.app.resource_api.versioning import AgentVersionMiddleware
    from lockbot.backend.app.resource_auth.http_errors import install_resource_auth_errors

    app.add_middleware(AgentVersionMiddleware)
    install_resource_auth_errors(app)
    install_agent_validation_errors(app)

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    from fastapi.responses import JSONResponse
    from sqlalchemy.orm.exc import StaleDataError

    from lockbot.backend.app.platform_applications.service import CredentialUnavailable

    @app.exception_handler(CredentialUnavailable)
    async def unavailable_credential(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})

    @app.exception_handler(StaleDataError)
    async def concurrent_credential_change(request, exc):
        return JSONResponse(
            status_code=409,
            content={"detail": "Record changed; refresh before retrying"},
            headers={"Cache-Control": "no-store"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from lockbot.backend.app.managed_paths import get_managed_paths
    from lockbot.backend.app.service_history import ServiceHistory
    from lockbot.backend.app.service_traffic import ServiceTrafficMiddleware, TrafficStats
    from lockbot.backend.app.service_traffic import bot_router as bot_service_traffic_router
    from lockbot.backend.app.service_traffic import router as service_traffic_router

    app.state.service_history = ServiceHistory(get_managed_paths().root / "monitor-history")
    app.state.service_traffic = TrafficStats(history=app.state.service_history)
    from lockbot.core import command_context

    command_context.operation_observer = app.state.service_traffic.record_operation
    from lockbot.backend.app.resource_api.cache_control import ResourceSnapshotCacheMiddleware

    app.add_middleware(ResourceSnapshotCacheMiddleware)
    app.add_middleware(ServiceTrafficMiddleware, stats=app.state.service_traffic)
    app.include_router(service_traffic_router)
    app.include_router(bot_service_traffic_router)

    from lockbot.backend.app.bot_audit.router import router as bot_audit_router

    app.include_router(bot_audit_router)
    app.include_router(auth_router)
    app.include_router(bots_router)
    from lockbot.backend.app.feishu.router import router as feishu_router

    app.include_router(feishu_router)
    from lockbot.backend.app.feishu.display_names import router as display_names_router

    app.include_router(display_names_router)
    app.include_router(admin_router)
    app.include_router(settings_router)
    from lockbot.backend.app.broadcast.router import router as broadcast_router

    app.include_router(broadcast_router)
    app.include_router(audit_router)
    app.include_router(platform_applications_router)
    # Dedicated note-delegation contracts belong to administrator documentation.
    # Keep the authenticated routes unchanged, but omit them from anonymous schemas.
    app.include_router(audit_delegation_router, include_in_schema=False)
    app.include_router(configuration_delegation_router)
    app.include_router(backup_router)
    from lockbot.backend.app.resource_api.discovery import router as discovery_router

    app.include_router(discovery_router)
    app.include_router(resource_auth_router)
    app.include_router(agent_resources_router)
    app.include_router(agent_commands_router)
    app.include_router(agent_usage_router)
    app.include_router(resource_commands_router)
    app.include_router(resources_router)
    app.include_router(usage_router)
    app.include_router(activity_router)
    app.include_router(skill_router)
    app.include_router(operator_router)
    from lockbot.backend.app.resource_api.views import router as view_router

    app.include_router(view_router)

    # ── Integration API docs: personal Agent, operator, enrollment and downloads ──
    public_doc_sections = ("agent", "op", "configuration")

    def public_docs_language(request: Request):
        language = request.headers.get("accept-language", "").split(",", 1)[0].split(";", 1)[0].strip().lower()
        return "en" if language == "en" or language.startswith("en-") else "zh"

    def localized_doc_headers(lang: str):
        return {"Content-Language": lang, "Vary": "Accept-Language"}

    @app.get("/api/public-openapi.json", include_in_schema=False)
    def public_openapi(
        request: Request, response: Response, scope: Literal["agent", "op", "configuration"] | None = None
    ):
        import json as _json
        import re as _re

        from lockbot.core.i18n import t

        lang = public_docs_language(request)
        localized_groups = (
            {
                "Agent API": "agent",
                "Agent 绑定": "binding",
                "OP API": "op",
                "Configuration API": "configuration",
                "Skill 下载": "agent_download",
            }
            if scope is not None
            else {}
        )
        localized_titles = {
            name: t(f"public_docs.{group}.title", lang=lang) for name, group in localized_groups.items()
        }
        schema = _json.loads(_json.dumps(app.openapi()))
        public_paths = {}
        used_schemes = set()
        used_tags = set()
        for path, methods in schema.get("paths", {}).items():
            for method, detail in methods.items():
                if "Public API" in detail.get("tags", []) and not detail.get("deprecated", False):
                    if "/configuration-access" in path or path.startswith("/api/bot-provisioning/"):
                        section, group, scheme = "configuration", "Configuration API", "ConfigurationKey"
                    elif path.endswith(("/locked-users", "/opkick", "/kicklock")) or (
                        path.startswith("/api/bots/") and "/op/" in path
                    ):
                        section, group, scheme = "op", "OP API", "OpApiKey"
                    elif path.startswith("/api/resource-auth/"):
                        section, group, scheme = "agent", "Agent 绑定", None
                    elif path.startswith("/api/agent-skill"):
                        section, group, scheme = "agent", "Skill 下载", None
                    else:
                        section, group, scheme = "agent", "Agent API", "AgentKey"
                    if scope is not None and section != scope:
                        continue
                    used_tags.add(group)
                    if scheme:
                        used_schemes.add(scheme)
                    detail["tags"] = [localized_titles.get(group, group)]
                    detail["security"] = [{scheme: []}] if scheme else []
                    if path == "/api/agent/resources" and method == "get":
                        commands = (
                            "```sh\n"
                            'curl -H "Authorization: Bearer $LOCKBOT_AGENT_KEY" "$LOCKBOT_ORIGIN/api/agent/context"\n'
                            'curl -H "Authorization: Bearer $LOCKBOT_AGENT_KEY" "$LOCKBOT_ORIGIN/api/agent/resources"\n'
                            "```"
                        )
                        detail["description"] = t("public_docs.resource_query_example", lang=lang, commands=commands)
                    public_paths.setdefault(path, {})[method] = detail

        # Include EVERY schema reachable from the public paths by following ``$ref`` transitively,
        # not just top-level names that happen to appear as substrings. A nested model (e.g.
        # CommandResponse -> CommandActor) would otherwise leave a dangling $ref. Seeding only from
        # the public paths also guarantees no management-only schema leaks in.
        all_schemas = schema.get("components", {}).get("schemas", {})
        ref_re = _re.compile(r'"#/components/schemas/([^"/]+)"')

        def _refs(obj) -> set:
            return set(ref_re.findall(_json.dumps(obj)))

        needed = _refs(public_paths)
        pending = list(needed)
        while pending:
            name = pending.pop()
            for ref in _refs(all_schemas.get(name, {})):
                if ref not in needed:
                    needed.add(ref)
                    pending.append(ref)
        public_schemas = {name: all_schemas[name] for name in sorted(needed) if name in all_schemas}
        document = {
            "openapi": schema["openapi"],
            "info": {
                "title": "LockBot API",
                "version": schema["info"]["version"],
                "description": (
                    "对外 API 按身份与用途分组。Agent 个人 key 与 OP API Key 不能互换；公开文档不表示接口无需鉴权。"
                ),
            },
            "tags": [
                {
                    "name": "Agent API",
                    "description": t("public_docs.agent_api_intro", lang=lang)
                    + "\n\n"
                    + t("public_docs.resource_queries", lang=lang),
                },
                {
                    "name": "Agent 绑定",
                    "description": (
                        "申请绑定码、查询绑定状态无需已有 key。绑定码 10 分"
                        "钟有效，必须由用户在机器人群内确认后才能使用个人 key。"
                    ),
                },
                {
                    "name": "OP API",
                    "description": (
                        "运维接口：Authorization: Bearer lbk_…。使用后台生成的 OP AP"
                        "I Key，按机器人授权，不代表某位群成员。可查询占用和强制释放，请勿交给普通 Agent。"
                    ),
                },
                {
                    "name": "Configuration API",
                    "description": (
                        "Agent 配置接口。使用 API 凭证页签发的配置凭证，"
                        "按选定的机器人和权限读取、预览并提交节点连接或数据源配置。"
                    ),
                },
                {
                    "name": "Skill 下载",
                    "description": ("公开下载入口，无需 API key。先把 skill 链接交给 Agent，再由用户完成绑定。"),
                },
            ],
            "paths": public_paths,
            "components": {
                "schemas": public_schemas,
                "securitySchemes": {
                    "AgentKey": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "lbr_…",
                        "description": "用户在群中确认绑定的个人 key；自己的多个 Agent 共用。",
                    },
                    "OpApiKey": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "lbk_…",
                        "description": "机器人后台的 OP API Key，供受信任运维使用。",
                    },
                    "ConfigurationKey": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "lbc1.…",
                        "description": "配置凭证，按机器人和配置权限签发。",
                    },
                },
            },
        }
        if scope is not None:
            response.headers.update(localized_doc_headers(lang))
            document["info"]["title"] = t(f"public_docs.{scope}.title", lang=lang)
            document["info"]["description"] = t(f"public_docs.{scope}.description", lang=lang)
            document["tags"] = [
                {
                    "name": localized_titles[name],
                    "description": (
                        t(f"public_docs.{localized_groups[name]}.description", lang=lang)
                        + ("\n\n" + t("public_docs.resource_queries", lang=lang) if name == "Agent API" else "")
                    ),
                }
                for name in localized_groups
                if name in used_tags
            ]
            document["components"]["securitySchemes"] = {
                name: {**value, "description": t(f"public_docs.{scope}.credential", lang=lang)}
                for name, value in document["components"]["securitySchemes"].items()
                if name in used_schemes
            }
        return document

    @app.get("/api/public-docs", include_in_schema=False)
    def public_docs(request: Request):
        from lockbot.core.i18n import t

        lang = public_docs_language(request)
        title = escape(t("public_docs.title", lang=lang))
        entries = []
        for scope in public_doc_sections:
            label = escape(t(f"public_docs.{scope}.title", lang=lang))
            description = escape(t(f"public_docs.{scope}.description", lang=lang))
            entries.append(f'<li><a href="/api/public-docs/{scope}">{label}</a><p>{description}</p></li>')
        schema_label = escape(t("public_docs.schema", lang=lang))
        return HTMLResponse(
            f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
            "<style>body{font:16px/1.55 system-ui,sans-serif;margin:0 auto;padding:2rem 1rem;max-width:42rem}"
            "li{margin-block:1.25rem}li p{margin:.25rem 0}a{font-weight:600}</style></head>"
            f"<body><main><h1>{title}</h1><ul>{''.join(entries)}</ul>"
            f'<a href="/api/public-openapi.json">{schema_label}</a></main></body></html>',
            headers=localized_doc_headers(lang),
        )

    @app.get("/api/public-docs/{scope}", include_in_schema=False)
    def public_docs_section(request: Request, scope: str):
        from lockbot.core.i18n import t

        if scope not in public_doc_sections:
            raise HTTPException(status_code=404)
        lang = public_docs_language(request)
        page = get_swagger_ui_html(
            openapi_url=f"/api/public-openapi.json?scope={scope}",
            title=t(f"public_docs.{scope}.title", lang=lang),
            swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        )
        compact_style = """
        <style>
          #swagger-ui .info { margin: 20px 0 12px; }
          #swagger-ui .info .title { font-size: 28px; }
          #swagger-ui .info p { margin: 8px 0 0; }
          #swagger-ui .scheme-container {
            margin: 0 0 12px;
            padding: 0;
            background: transparent;
            box-shadow: none;
          }
          #swagger-ui .auth-wrapper .authorize { margin: 0; }
          #swagger-ui .opblock-tag > small p { margin: 4px 0; }
        </style>
        """
        return HTMLResponse(
            page.body.decode("utf-8").replace("</head>", compact_style + "</head>", 1),
            headers=localized_doc_headers(lang),
        )

    from lockbot.backend.app.personal_panel.setup import mount_if_configured

    mount_if_configured(app)

    trace_review_directory = os.environ.get("LOCKBOT_TRACE_REVIEW_DIR")
    if trace_review_directory:
        from pathlib import Path

        from lockbot.backend.app.trace_review import trace_review_router

        app.include_router(trace_review_router(Path(trace_review_directory)))

    # Serve frontend static files (built by vite)
    # In Docker: /app/frontend/dist — locally: project_root/frontend/dist
    _frontend_dist = os.environ.get("FRONTEND_DIST", "")
    if not _frontend_dist:
        _frontend_dist = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "frontend", "dist")
        )
    if os.path.isdir(_frontend_dist):
        app.mount("/", SPAStaticFiles(directory=_frontend_dist, html=True), name="static")

    return app


app = create_app()
