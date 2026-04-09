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
