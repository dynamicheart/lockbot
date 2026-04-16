"""
Audit log tests.

Covers:
  - Operations write audit records to the DB
  - Visibility: super_admin sees all, admin sees only their managed users
  - Filters: action, result
  - Authorization: regular user cannot access audit logs
"""

# Import audit models so Base registers the table before _setup_db runs create_all
import lockbot.backend.app.audit.models  # noqa: F401

import importlib.util
import os
import pytest
from lockbot.backend.app.audit.models import AuditLog

# Load _TestSession directly from conftest.py without relying on package path
_conftest_spec = importlib.util.spec_from_file_location(
    "_test_conftest",
    os.path.join(os.path.dirname(__file__), "conftest.py"),
)
_conftest_mod = importlib.util.module_from_spec(_conftest_spec)
_TestSession = None  # populated lazily below to avoid double-import side-effects


def _get_test_session():
    """Return the _TestSession factory from conftest, importing it lazily."""
    global _TestSession
    if _TestSession is None:
        # conftest is already loaded by pytest; find it in sys.modules
        import sys
        for _key, _mod in sys.modules.items():
            if "conftest" in _key and hasattr(_mod, "_TestSession"):
                _TestSession = _mod._TestSession
                break
        if _TestSession is None:
            # Fallback: load directly from file
            _conftest_spec.loader.exec_module(_conftest_mod)
            _TestSession = _conftest_mod._TestSession
    return _TestSession


# ---------------------------------------------------------------------------
# Extra fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def super_admin_header(client):
    """Create a super_admin user and return its auth header."""
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.auth.router import _hash_password

    with _get_test_session()() as session:
        sa = User(
            username="superadmin",
            email="sa@test.com",
            password_hash=_hash_password("sapass123"),
            role="super_admin",
        )
        session.add(sa)
        session.commit()

    resp = client.post("/api/auth/login", json={"username": "superadmin", "password": "sapass123"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sample_bot(name="testbot"):
    return {
        "name": name,
        "bot_type": "NODE",
        "webhook_url": "https://example.com/webhook",
        "aes_key": "key",
        "token": "tok",
        "cluster_configs": ["node1"],
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _audit_records(db_session, action=None):
    q = db_session.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    return q.all()


# ---------------------------------------------------------------------------
# Auth events
# ---------------------------------------------------------------------------


class TestAuthAudit:
    def test_login_success_is_logged(self, client, auth_header, db_session):
        # auth_header fixture already triggered a login
        records = _audit_records(db_session, "auth.login")
        assert len(records) >= 1
        rec = records[-1]
        assert rec.result == "success"
        assert rec.operator_username == "testuser"

    def test_login_failure_is_logged(self, client, db_session):
        client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
        records = _audit_records(db_session, "auth.login")
        failures = [r for r in records if r.result == "failure"]
        assert len(failures) >= 1
        assert failures[-1].operator_username == "nobody"

    def test_change_password_is_logged(self, client, auth_header, db_session):
        client.put(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "newpass456"},
            headers=auth_header,
        )
        records = _audit_records(db_session, "auth.change_password")
        assert len(records) == 1
        assert records[0].result == "success"
        assert records[0].operator_username == "testuser"

    def test_change_email_is_logged(self, client, auth_header, db_session):
        client.put(
            "/api/auth/change-email",
            json={"new_email": "newemail@test.com"},
            headers=auth_header,
        )
        records = _audit_records(db_session, "auth.change_email")
        assert len(records) == 1
        assert records[0].result == "success"


# ---------------------------------------------------------------------------
# Admin / user management events
# ---------------------------------------------------------------------------


class TestAdminAudit:
    def test_create_user_is_logged(self, client, admin_header, db_session):
        client.post(
            "/api/admin/users",
            json={"username": "newu", "email": "newu@test.com", "role": "user", "max_running_bots": 5},
            headers=admin_header,
        )
        records = _audit_records(db_session, "user.create")
        assert len(records) == 1
        rec = records[0]
        assert rec.result == "success"
        assert rec.target_name == "newu"
        assert rec.target_type == "user"
        assert rec.operator_username == "adminuser"

    def test_reset_password_is_logged(self, client, admin_header, db_session):
        # Create a target user first
        resp = client.post(
            "/api/admin/users",
            json={"username": "target", "email": "target@test.com", "role": "user", "max_running_bots": 5},
            headers=admin_header,
        )
        user_id = resp.json()["id"]
        client.post(f"/api/admin/users/{user_id}/reset-password", headers=admin_header)
        records = _audit_records(db_session, "user.reset_password")
        assert len(records) == 1
        assert records[0].target_id == user_id

    def test_force_logout_is_logged(self, client, admin_header, db_session):
        resp = client.post(
            "/api/admin/users",
            json={"username": "logout_target", "email": "lt@test.com", "role": "user", "max_running_bots": 5},
            headers=admin_header,
        )
        user_id = resp.json()["id"]
        client.post(f"/api/admin/users/{user_id}/force-logout", headers=admin_header)
        records = _audit_records(db_session, "user.force_logout")
        assert len(records) == 1
        assert records[0].target_id == user_id


# ---------------------------------------------------------------------------
# Bot lifecycle events
# ---------------------------------------------------------------------------


class TestBotAudit:
    def test_create_bot_is_logged(self, client, admin_header, db_session):
        client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        records = _audit_records(db_session, "bot.create")
        assert len(records) == 1
        rec = records[0]
        assert rec.result == "success"
        assert rec.target_name == "testbot"
        assert rec.target_type == "bot"

    def test_delete_bot_is_logged(self, client, admin_header, db_session):
        resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = resp.json()["id"]
        client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        records = _audit_records(db_session, "bot.delete")
        assert len(records) == 1
        assert records[0].target_id == bot_id

    def test_start_bot_is_logged(self, client, admin_header, db_session):
        from unittest.mock import patch

        resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = resp.json()["id"]
        with patch("lockbot.backend.app.bots.router.bot_manager") as m:
            m.start_bot.return_value = 1234
            client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
        records = _audit_records(db_session, "bot.start")
        assert len(records) == 1
        assert records[0].target_id == bot_id

    def test_stop_bot_is_logged(self, client, admin_header, db_session):
        from unittest.mock import patch

        resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = resp.json()["id"]

        # Force status to running via DB so stop is allowed
        import sys
        from lockbot.backend.app.bots.models import Bot

        with _get_test_session()() as s:
            b = s.get(Bot, bot_id)
            b.status = "running"
            s.commit()

        with patch("lockbot.backend.app.bots.router.bot_manager"):
            client.post(f"/api/bots/{bot_id}/stop", headers=admin_header)
        records = _audit_records(db_session, "bot.stop")
        assert len(records) == 1


# ---------------------------------------------------------------------------
# Visibility rules
# ---------------------------------------------------------------------------


class TestAuditVisibility:
    def test_super_admin_sees_all(self, client, auth_header, admin_header, super_admin_header, db_session):
        # Trigger one login per role to generate audit records from different operators
        # auth_header already logged in as 'testuser' (user)
        # admin_header already logged in as 'adminuser' (admin)
        # super_admin_header already logged in as 'superadmin' (super_admin)
        resp = client.get("/api/audit/logs", headers=super_admin_header)
        assert resp.status_code == 200
        data = resp.json()
        # Should see all 3 login records
        usernames = {item["operator_username"] for item in data["items"]}
        assert "testuser" in usernames
        assert "adminuser" in usernames
        assert "superadmin" in usernames

    def test_admin_cannot_see_super_admin_actions(
        self, client, super_admin_header, admin_header, db_session
    ):
        # super_admin performed login — admin should NOT see it
        resp = client.get("/api/audit/logs", headers=admin_header)
        assert resp.status_code == 200
        items = resp.json()["items"]
        sa_items = [i for i in items if i["operator_username"] == "superadmin"]
        assert len(sa_items) == 0

    def test_admin_sees_own_actions(self, client, admin_header, db_session):
        resp = client.get("/api/audit/logs", headers=admin_header)
        assert resp.status_code == 200
        items = resp.json()["items"]
        own = [i for i in items if i["operator_username"] == "adminuser"]
        assert len(own) >= 1

    def test_regular_user_sees_only_own_records(self, client, auth_header, db_session):
        # Regular users can access audit logs but only see their own records
        resp = client.get("/api/audit/logs", headers=auth_header)
        assert resp.status_code == 200
        items = resp.json()["items"]
        # All returned items must belong to the regular user
        for item in items:
            assert item["operator_username"] == "testuser"

    def test_unauthenticated_cannot_access_audit_logs(self, client):
        resp = client.get("/api/audit/logs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestAuditFilters:
    def test_filter_by_action(self, client, admin_header, db_session):
        # Create a user to generate a user.create record
        client.post(
            "/api/admin/users",
            json={"username": "fu", "email": "fu@test.com", "role": "user", "max_running_bots": 5},
            headers=admin_header,
        )
        resp = client.get("/api/audit/logs", params={"action": "user.create"}, headers=admin_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert all(item["action"] == "user.create" for item in data["items"])

    def test_filter_by_result_failure(self, client, admin_header, db_session):
        # Trigger a failed login
        client.post("/api/auth/login", json={"username": "noone", "password": "bad"})
        resp = client.get(
            "/api/audit/logs",
            params={"result": "failure", "action": "auth.login"},
            headers=admin_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert all(item["result"] == "failure" for item in data["items"])

    def test_pagination(self, client, admin_header, db_session):
        resp = client.get("/api/audit/logs", params={"page": 1, "limit": 1}, headers=admin_header)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 1
