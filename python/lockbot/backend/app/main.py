"""
FastAPI application entry point
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import models to register them with Base.metadata
import lockbot.backend.app.auth.models  # noqa: F401
import lockbot.backend.app.bots.models  # noqa: F401
from lockbot.backend.app.admin.router import router as admin_router
from lockbot.backend.app.auth.router import router as auth_router
from lockbot.backend.app.bots.router import router as bots_router
from lockbot.backend.app.database import Base, SessionLocal, engine

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
            logger.info("Dev admin '%s' already exists, skipping seed.", DEV_ADMIN_USERNAME)
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
                logger.info("Dev user '%s' already exists, skipping.", username)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_bot_logs_category()
    _migrate_bot_consecutive_failures()
    _migrate_users_must_change_password()
    _seed_dev_admin()
    _seed_dev_users()
    _reset_running_bots()
    yield
    # Shutdown: cancel all bot timers and clean up
    from lockbot.backend.app.bots.manager import bot_manager

    logger.info("Shutting down all bots…")
    bot_manager.shutdown_all()


def _reset_running_bots():
    """On process startup, try to auto-recover bots that were previously running.

    For each bot that was 'running' before the process died, attempt to restart
    it in-process. If restart fails, reset to 'stopped'.
    Bots already in 'error' status are always reset to 'stopped'.
    """
    from lockbot.backend.app.bots.manager import bot_manager
    from lockbot.backend.app.bots.models import Bot

    db = SessionLocal()
    try:
        # Reset error bots unconditionally
        error_count = (
            db.query(Bot)
            .filter(Bot.status == "error")
            .update({Bot.status: "stopped", Bot.pid: None}, synchronize_session="fetch")
        )
        if error_count:
            db.commit()
            logger.info("Reset %d error bot(s) to stopped on startup", error_count)

        # Try to auto-recover previously running bots
        running_bots = db.query(Bot).filter(Bot.status == "running").all()
        if not running_bots:
            return

        logger.info("Attempting to auto-recover %d previously running bot(s)…", len(running_bots))
        for bot in running_bots:
            try:
                from lockbot.backend.app.bots.router import _build_config_dict

                config_dict = _build_config_dict(bot)
                pid = bot_manager.start_bot(bot.id, config_dict)
                bot.pid = pid
                bot.consecutive_failures = 0
                db.commit()
                logger.info("Auto-recovered bot %d (%s)", bot.id, bot.name)
            except Exception:
                logger.exception("Failed to auto-recover bot %d (%s), resetting to stopped", bot.id, bot.name)
                bot.status = "stopped"
                bot.pid = None
                db.commit()
    finally:
        db.close()


def create_app() -> FastAPI:
    from importlib.metadata import version as _pkg_version

    try:
        _ver = _pkg_version("lockbot")
    except Exception:
        _ver = "unknown"
    app = FastAPI(title="LockBot Platform", version=_ver, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(bots_router)
    app.include_router(admin_router)
    return app


app = create_app()
