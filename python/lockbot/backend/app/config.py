"""
Backend application configuration.
"""

import os
from pathlib import Path

# Project root directory (lockbot/)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

DATA_DIR = os.environ.get("DATA_DIR", "/data")

# Database
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'lockbot.db')}",
)

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "lockbot-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# Fernet encryption key (for sensitive fields)
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# Activity monitoring
INACTIVE_THRESHOLD_DAYS = 7

# Feature flags
ALLOW_REGISTER = os.environ.get("ALLOW_REGISTER", "false").lower() in ("true", "1", "yes")

# Dev mode: auto-create admin user on startup
DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")
DEV_ADMIN_USERNAME = os.environ.get("DEV_ADMIN_USERNAME", "admin")
DEV_ADMIN_EMAIL = os.environ.get("DEV_ADMIN_EMAIL", "admin@lockbot.dev")
DEV_ADMIN_PASSWORD = os.environ.get("DEV_ADMIN_PASSWORD", "admin123")
