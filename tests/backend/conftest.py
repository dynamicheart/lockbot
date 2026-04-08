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
def _setup_db(tmp_path):
    """Create tables before each test, drop after. Use tmp_path for log files."""
    import lockbot.backend.app.auth.models  # noqa: F401
    import lockbot.backend.app.bots.models  # noqa: F401

    # Redirect bot logs to a temp directory so tests don't pollute /data
    log_base = str(tmp_path / "bots")
    original_get_log_dir = None
    try:
        from lockbot.backend.app.bots import router as _router

        original_get_log_dir = _router._get_log_dir
        _router._get_log_dir = lambda bot_id: (
            os.makedirs(os.path.join(log_base, str(bot_id)), exist_ok=True) or os.path.join(log_base, str(bot_id))
        )
    except Exception:
        pass

    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)

    if original_get_log_dir is not None:
        import contextlib

        with contextlib.suppress(Exception):
            _router._get_log_dir = original_get_log_dir


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
