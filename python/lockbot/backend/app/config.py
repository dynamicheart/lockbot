"""
Backend application configuration.
"""

import os
from pathlib import Path

from lockbot.backend.app.managed_paths import ManagedPaths
from lockbot.backend.app.sqlite_paths import canonical_sqlite_url

# Project root directory (lockbot/)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

MANAGED_PATHS = ManagedPaths(os.environ.get("DATA_DIR", str(BASE_DIR.parent / "data")))
LEGACY_WORKING_DIR = Path.cwd()
LEGACY_CORE_ROOT = Path(os.environ.get("DATA_DIR", "/data")).resolve()
DATA_DIR = str(MANAGED_PATHS.root)
os.makedirs(DATA_DIR, exist_ok=True)

# Database
DATABASE_URL = canonical_sqlite_url(os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'lockbot.db')}"))

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "lockbot-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 90  # 90 days (~3 months)

# Fernet encryption key (for sensitive fields)
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# Activity monitoring
INACTIVE_THRESHOLD_DAYS = 7

# Rate limiting — Redis storage URI (optional, falls back to in-memory)
# Example: redis://localhost:6379
REDIS_URL = os.environ.get("REDIS_URL", "")

# Feature flags
ALLOW_REGISTER = os.environ.get("ALLOW_REGISTER", "false").lower() in ("true", "1", "yes")

# Dev mode: auto-create admin user on startup
DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")
DEV_ADMIN_USERNAME = os.environ.get("DEV_ADMIN_USERNAME", "admin")
DEV_ADMIN_EMAIL = os.environ.get("DEV_ADMIN_EMAIL", "admin@lockbot.dev")
DEV_ADMIN_PASSWORD = os.environ.get("DEV_ADMIN_PASSWORD", "admin123")

# ── Resource auth (agent-facing API identity binding) ──
# How long a bind code stays claimable. Only the binding PROCESS expires; the credential
# it issues does not. Enforced at claim time, never by a background sweep.
RESOURCE_AUTH_BIND_WINDOW_SECONDS = int(os.environ.get("RESOURCE_AUTH_BIND_WINDOW_SECONDS", 10 * 60))

# Caps on outstanding unconfirmed bindings. Because unconfirmed bindings live in memory
# rather than in the database, these caps are what bound the flood target.
RESOURCE_AUTH_MAX_PENDING_TOTAL = int(os.environ.get("RESOURCE_AUTH_MAX_PENDING_TOTAL", 200))
RESOURCE_AUTH_MAX_PENDING_PER_SOURCE = int(os.environ.get("RESOURCE_AUTH_MAX_PENDING_PER_SOURCE", 5))

# Optional bounded credential lifetime. 0 means "no expiry", which is the default:
# revocation is then the only invalidation path.
RESOURCE_AUTH_CREDENTIAL_TTL_SECONDS = int(os.environ.get("RESOURCE_AUTH_CREDENTIAL_TTL_SECONDS", 0))

# Throttle on failed chat `bind` attempts per (platform, external_user_id, bot). The HTTP
# creation limit does not bound chat attempts, so this is enforced separately.
RESOURCE_AUTH_MAX_BIND_FAILURES = int(os.environ.get("RESOURCE_AUTH_MAX_BIND_FAILURES", 5))
RESOURCE_AUTH_BIND_FAILURE_WINDOW_SECONDS = int(os.environ.get("RESOURCE_AUTH_BIND_FAILURE_WINDOW_SECONDS", 10 * 60))

# ── Agent API per-credential traffic budget ──
# Per-credential (per durable binding) request budgets, enforced in-process before admission.
# Separate from the per-IP limiter and the per-bot admission cap. Conservative defaults; tune
# against real load (the plan's capacity audit is not a substitute for measurement here).
RESOURCE_API_READ_BUDGET_PER_MINUTE = int(os.environ.get("RESOURCE_API_READ_BUDGET_PER_MINUTE", 120))
RESOURCE_API_WRITE_BUDGET_PER_MINUTE = int(os.environ.get("RESOURCE_API_WRITE_BUDGET_PER_MINUTE", 6))

# Per-resource poll tier: the minimum interval between /resources polls for one credential,
# derived server-side from the caller's current stake. A holder or head-of-queue (active) may
# poll fastest; a waiting booker slower; a bystander discovering state slowest. A compliant
# agent stays under its budget automatically; busy-polling is shed with a precise Retry-After.
RESOURCE_API_TIER_ACTIVE_SECONDS = int(os.environ.get("RESOURCE_API_TIER_ACTIVE_SECONDS", 10))
RESOURCE_API_TIER_BOOKING_SECONDS = int(os.environ.get("RESOURCE_API_TIER_BOOKING_SECONDS", 20))
RESOURCE_API_TIER_DISCOVERY_SECONDS = int(os.environ.get("RESOURCE_API_TIER_DISCOVERY_SECONDS", 40))

# Per-identity concurrent-holdings cap for API acquisitions (lock/slock/book). Bounds how many
# resources one verified identity can hold+book through the API at once. Checked atomically
# with acquisition under the core state lock, across all credentials of the same identity.
RESOURCE_API_MAX_HELD_PER_IDENTITY = int(os.environ.get("RESOURCE_API_MAX_HELD_PER_IDENTITY", 8))
