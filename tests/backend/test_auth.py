"""
Auth module tests.
"""


class TestRegister:
    def test_register_success(self, client):
        resp = client.post(
            "/api/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com",
                "password": "secret123",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "alice"
        assert data["role"] == "user"

    def test_register_duplicate_username(self, client):
        payload = {"username": "bob", "email": "bob@example.com", "password": "pass"}
        client.post("/api/auth/register", json=payload)
        resp = client.post(
            "/api/auth/register",
            json={
                "username": "bob",
                "email": "bob2@example.com",
                "password": "pass",
            },
        )
        assert resp.status_code == 409

    def test_register_duplicate_email(self, client):
        client.post(
            "/api/auth/register",
            json={
                "username": "u1",
                "email": "same@example.com",
                "password": "pass",
            },
        )
        resp = client.post(
            "/api/auth/register",
            json={
                "username": "u2",
                "email": "same@example.com",
                "password": "pass",
            },
        )
        assert resp.status_code == 409


class TestLogin:
    def test_login_success(self, client):
        client.post(
            "/api/auth/register",
            json={
                "username": "carol",
                "email": "carol@example.com",
                "password": "mypass",
            },
        )
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "carol",
                "password": "mypass",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client):
        client.post(
            "/api/auth/register",
            json={
                "username": "dave",
                "email": "dave@example.com",
                "password": "correct",
            },
        )
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "dave",
                "password": "wrong",
            },
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post(
            "/api/auth/login",
            json={
                "username": "nobody",
                "password": "pass",
            },
        )
        assert resp.status_code == 401


class TestMe:
    def test_me_authenticated(self, client, auth_header):
        resp = client.get("/api/auth/me", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["username"] == "testuser"

    def test_me_no_token(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client):
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401


def test_site_account_session_policy_preserves_existing_deadlines(client, auth_header, admin_header, db_session):
    from datetime import datetime, timezone

    import jwt
    from lockbot.backend.app.auth.dependencies import _PERMANENT_KEY_ID, _permanent_session_key
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.config import JWT_ALGORITHM, JWT_SECRET

    db_session.query(User).filter_by(username="adminuser").one().role = "super_admin"
    db_session.commit()

    def set_lifetime(days):
        return client.put("/api/admin/settings", headers=admin_header, json={"settings": {"login_session_days": days}})

    def login():
        response = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        permanent = jwt.get_unverified_header(token).get("kid") == _PERMANENT_KEY_ID
        key = _permanent_session_key() if permanent else JWT_SECRET
        return jwt.decode(token, key, algorithms=[JWT_ALGORITHM]), {"Authorization": "Bearer " + token}

    def check_expiry(claims, days):
        now = datetime.now(timezone.utc).timestamp()
        assert abs(claims["exp"] - now - days * 86400) < 10

    assert set_lifetime("30").status_code == 422
    assert (
        client.put(
            "/api/admin/settings", headers=auth_header, json={"settings": {"login_session_days": "0"}}
        ).status_code
        == 403
    )
    claims, old_session = login()
    check_expiry(claims, 90)
    assert set_lifetime("180").status_code == 200
    claims, finite_session = login()
    check_expiry(claims, 180)
    assert set_lifetime("0").status_code == 200
    claims, permanent_session = login()
    assert "exp" not in claims
    assert set_lifetime("0").status_code == 200
    assert client.get("/api/auth/me", headers=permanent_session).status_code == 200
    assert set_lifetime("90").status_code == 200
    assert client.get("/api/auth/me", headers=permanent_session).status_code == 401
    assert client.get("/api/auth/me", headers=old_session).status_code == 200
    assert client.get("/api/auth/me", headers=finite_session).status_code == 200
    assert set_lifetime("0").status_code == 200
    assert client.get("/api/auth/me", headers=permanent_session).status_code == 401
    _, current_session = login()
    response = client.put(
        "/api/auth/change-password",
        headers=current_session,
        json={"current_password": "testpass123", "new_password": "new-test-pass"},
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me", headers=current_session).status_code == 401
    assert (
        client.get("/api/auth/me", headers={"Authorization": "Bearer " + response.json()["access_token"]}).status_code
        == 200
    )
