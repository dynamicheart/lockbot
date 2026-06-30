"""
FastAPI application entry point
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Import models to register them with Base.metadata
import lockbot.backend.app.audit.models  # noqa: F401
import lockbot.backend.app.auth.models  # noqa: F401
import lockbot.backend.app.bots.models  # noqa: F401
import lockbot.backend.app.settings.models  # noqa: F401
from lockbot.backend.app.admin.router import router as admin_router
from lockbot.backend.app.audit.router import router as audit_router
from lockbot.backend.app.auth.router import router as auth_router
from lockbot.backend.app.backup.router import router as backup_router
from lockbot.backend.app.bots.router import router as bots_router
from lockbot.backend.app.database import Base, SessionLocal, engine
from lockbot.backend.app.settings.router import router as settings_router

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
    """Add and backfill stable uuid column for bots."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_bot_logs_category()
    _migrate_bot_consecutive_failures()
    _migrate_users_must_change_password()
    _migrate_users_token_version()
    _migrate_bot_soft_delete()
    _migrate_audit_logs()
    _migrate_bots_api_key()
    _migrate_bots_uuid()
    _seed_dev_admin()
    _seed_dev_users()
    from lockbot.backend.app.bots.manager import bot_manager

    bot_manager.start_scheduler()
    _reset_running_bots()

    from lockbot.backend.app.backup.scheduler import backup_scheduler

    backup_scheduler.start()

    yield
    # Shutdown: stop scheduler and clean up all bots
    logger.info("Shutting down all bots…")
    backup_scheduler.stop()
    bot_manager.shutdown_all()


def _reset_running_bots():
    """On process startup, try to auto-recover bots that were previously running.

    For each bot that was 'running' or 'error' before the process died, attempt to
    restart it in-process. If restart fails, reset to 'stopped' and clear error state.
    """

    def _try_recover(bot, db):
        """Try to start a single bot. Returns True on success."""
        try:
            from lockbot.backend.app.bots.router import _build_config_dict

            config_dict = _build_config_dict(bot, db)
            pid = bot_manager.start_bot(bot.id, config_dict)
            bot.pid = pid
            bot.consecutive_failures = 0
            db.commit()
            logger.info("Auto-recovered bot %d (%s)", bot.id, bot.name)
            return True
        except Exception:
            logger.exception("Failed to auto-recover bot %d (%s), resetting to stopped", bot.id, bot.name)
            bot.status = "stopped"
            bot.pid = None
            db.commit()
            return False

    from lockbot.backend.app.bots.manager import bot_manager
    from lockbot.backend.app.bots.models import Bot

    db = SessionLocal()
    try:
        # Collect all bots that need recovery (running + error)
        recover_bots = (
            db.query(Bot)
            .filter(
                Bot.status.in_(["running", "error"]),
                Bot.is_deleted.is_(False),
            )
            .all()
        )
        if not recover_bots:
            return

        logger.info(
            "Attempting to auto-recover %d bot(s) (%d running, %d error)…",
            len(recover_bots),
            sum(1 for b in recover_bots if b.status == "running"),
            sum(1 for b in recover_bots if b.status == "error"),
        )
        for bot in recover_bots:
            _try_recover(bot, db)
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

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
    app.include_router(settings_router)
    app.include_router(audit_router)
    app.include_router(backup_router)

    # ── Public API docs (always available, only shows locked-users + opkick) ──
    @app.get("/api/public-openapi.json", include_in_schema=False)
    def public_openapi():
        import json as _json

        schema = _json.loads(_json.dumps(app.openapi()))
        public_paths = {}
        for path, methods in schema.get("paths", {}).items():
            for method, detail in methods.items():
                if "Public API" in detail.get("tags", []):
                    detail["tags"] = ["Public API"]
                    public_paths.setdefault(path, {})[method] = detail
        # Only include schemas referenced by public paths
        raw = _json.dumps(public_paths)
        public_schemas = {}
        for name, val in schema.get("components", {}).get("schemas", {}).items():
            if name in raw:
                public_schemas[name] = val
        return {
            "openapi": schema["openapi"],
            "info": {"title": "LockBot Public API", "version": schema["info"]["version"]},
            "paths": public_paths,
            "components": {"schemas": public_schemas} if public_schemas else {},
        }

    @app.get("/api/public-docs", include_in_schema=False)
    def public_docs():
        from fastapi.responses import HTMLResponse

        return HTMLResponse("""<!DOCTYPE html><html><head><title>LockBot Public API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:"/api/public-openapi.json",dom_id:"#swagger-ui",defaultModelsExpandDepth:-1})</script>
</body></html>""")

    # Serve frontend static files (built by vite)
    # In Docker: /app/frontend/dist — locally: project_root/frontend/dist
    _frontend_dist = os.environ.get("FRONTEND_DIST", "")
    if not _frontend_dist:
        _frontend_dist = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "frontend", "dist")
        )
    if os.path.isdir(_frontend_dist):
        app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="static")

    return app


app = create_app()
