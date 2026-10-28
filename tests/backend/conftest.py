"""
Test fixtures.
"""

import os

import lockbot.backend.app.config as _config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Enable registration for tests
_config.ALLOW_REGISTER = True
# Use in-memory SQLite for tests (must be set before database module import)
_config.DATABASE_URL = "sqlite://"
# The state-store ownership lock is a single-process production guard. The suite builds many
# apps in one process, so it would self-conflict; AC-12's coverage lives in
# tests/backend/test_state_ownership.py, which exercises the lock directly.
os.environ.setdefault("LOCKBOT_SKIP_STATE_OWNERSHIP", "1")

from lockbot.backend.app.database import Base, get_db  # noqa: E402
from lockbot.backend.app.main import create_app  # noqa: E402

# StaticPool ensures all connections share a single underlying DBAPI connection,
# so SQLite :memory: database is visible across all sessions.
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _managed_deployment(tmp_path, monkeypatch):
    """Substitute the deployment root, never per-bot production path controls."""
    from lockbot.backend.app.managed_paths import ManagedPaths

    paths = ManagedPaths(tmp_path)
    monkeypatch.setattr(_config, "MANAGED_PATHS", paths)
    monkeypatch.setattr(_config, "LEGACY_CORE_ROOT", tmp_path)
    monkeypatch.setattr(_config, "LEGACY_WORKING_DIR", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return paths


@pytest.fixture(autouse=True)
def _setup_db(tmp_path, _managed_deployment):
    """Create tables before each test, drop after; all managed IO uses the test root."""
    from unittest.mock import patch

    import lockbot.backend.app.audit.models  # noqa: F401
    import lockbot.backend.app.auth.models  # noqa: F401
    import lockbot.backend.app.bots.models  # noqa: F401
    import lockbot.backend.app.resource_auth.models  # noqa: F401

    _broadcast_patcher = patch("lockbot.backend.app.broadcast.service.broadcast_worker.start")
    _broadcast_patcher.start()

    # Disable rate limiting in tests — the in-memory limiter state persists
    # across fixture calls within a session and causes spurious 429 errors.
    _limiter_patcher = patch("lockbot.backend.app.rate_limit.limiter.limit", return_value=lambda f: f)
    _limiter_patcher.start()
    # Also reset any existing storage counters
    try:
        from lockbot.backend.app.rate_limit import limiter as _limiter

        _limiter._storage.reset()
    except Exception:
        pass

    # Patch bot_manager so auto-start on bot creation is safely caught.
    # Tests that explicitly test start/stop use their own @patch which overrides this.
    from lockbot.backend.app.bots.manager import BotNotRunning

    _patcher = patch("lockbot.backend.app.bots.router.bot_manager")
    _mock = _patcher.start()
    _mock.start_bot.side_effect = RuntimeError("no-auto-start")
    _mock.is_running.return_value = False
    _mock.is_failed.return_value = False
    _mock.stop_bot.return_value = None
    _mock.restart_bot.return_value = 9999
    # No bot is actually running under this default mock, so admission must refuse. Routes that
    # take a lease then fall back to their not-running path (file read / 503). Tests that need a
    # live instance wire their own bot_manager (admit yielding a lease).
    _mock.admit.side_effect = BotNotRunning("no bot running under the default test mock")

    Base.metadata.create_all(bind=_engine)
    yield
    _broadcast_patcher.stop()
    _limiter_patcher.stop()
    _patcher.stop()
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def client():
    app = create_app()

    def _override_get_db():
        session = _TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_header(client):
    """Register and login a test user, return Authorization header."""
    client.post(
        "/api/auth/register",
        json={
            "username": "testuser",
            "email": "test@test.com",
            "password": "testpass123",
        },
    )
    resp = client.post(
        "/api/auth/login",
        json={
            "username": "testuser",
            "password": "testpass123",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db_session():
    """Provide a direct DB session for test assertions (not via FastAPI deps)."""
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def session_factory():
    """The test session factory, for code that opens its own session (e.g. the chat hook)."""
    return _TestSession


@pytest.fixture(autouse=True)
def _reset_resource_auth():
    """Keep the process-wide pending store and bind throttle from leaking across tests."""
    from lockbot.backend.app.resource_auth import service as _resource_auth_service
    from lockbot.backend.app.resource_auth.pending import pending_store

    pending_store.purge_all()
    _resource_auth_service.reset_bind_throttle()
    yield
    pending_store.purge_all()
    _resource_auth_service.reset_bind_throttle()


@pytest.fixture(autouse=True)
def _relax_credential_budget():
    """The per-credential traffic budget and the per-resource poll tier are production flood
    guards; most tests issue bursts that would trip them. Raise the budget limits and zero the
    tier floors, resetting counters around each test (mirroring the disabled rate limiter).
    Budget/tier-specific tests set their own low limits on the singletons explicitly."""
    from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate

    saved = (credential_budget.read_limit, credential_budget.write_limit)
    saved_tiers = (resource_tier_gate.active, resource_tier_gate.booking, resource_tier_gate.discovery)
    credential_budget.read_limit = 10_000
    credential_budget.write_limit = 10_000
    credential_budget.reset()
    resource_tier_gate.active = resource_tier_gate.booking = resource_tier_gate.discovery = 0
    resource_tier_gate.reset()
    yield
    credential_budget.read_limit, credential_budget.write_limit = saved
    credential_budget.reset()
    resource_tier_gate.active, resource_tier_gate.booking, resource_tier_gate.discovery = saved_tiers
    resource_tier_gate.reset()


@pytest.fixture()
def admin_header(client):
    """Register and login an admin user, return Authorization header."""
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password

    with _TestSession() as session:
        admin = User(
            username="adminuser",
            email="admin@test.com",
            password_hash=_hash_password("adminpass123"),
            role="admin",
        )
        session.add(admin)
        session.commit()

    resp = client.post(
        "/api/auth/login",
        json={
            "username": "adminuser",
            "password": "adminpass123",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
