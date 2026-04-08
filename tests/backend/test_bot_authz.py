"""
Tests for bot authorization: owner and admin access control.
"""

from unittest.mock import patch

BOT_PAYLOAD = {
    "name": "auth_bot",
    "bot_type": "NODE",
    "webhook_url": "https://example.com/hook",
    "cluster_configs": {"n1": "n1"},
}


def _create_bot(client, auth_header):
    resp = client.post("/api/bots", json=BOT_PAYLOAD, headers=auth_header)
    assert resp.status_code == 201
    return resp.json()["id"]


def _register_user(client, username, email, password):
    client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
        },
    )
    resp = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestOwnerAccess:
    """Owner can fully control their own bots."""

    def test_owner_can_get_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 200

    def test_owner_can_update_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.put(f"/api/bots/{bot_id}", json={"name": "new_name"}, headers=auth_header)
        assert resp.status_code == 200

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_owner_can_start_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 1000
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_owner_can_stop_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 1000
        client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        resp = client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)
        assert resp.status_code == 200

    def test_owner_can_delete_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.delete(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 204

    def test_owner_can_get_logs(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}/logs", headers=auth_header)
        assert resp.status_code == 200

    def test_owner_can_get_state(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}/state", headers=auth_header)
        assert resp.status_code == 200


class TestOtherUserDenied:
    """Other users cannot access or control someone else's bot."""

    def test_other_user_cannot_get_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.get(f"/api/bots/{bot_id}", headers=other)
        assert resp.status_code == 404

    def test_other_user_cannot_update_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.put(f"/api/bots/{bot_id}", json={"name": "hacked"}, headers=other)
        assert resp.status_code == 404

    def test_other_user_cannot_delete_bot(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.delete(f"/api/bots/{bot_id}", headers=other)
        assert resp.status_code == 404

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_other_user_cannot_start_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.post(f"/api/bots/{bot_id}/start", headers=other)
        assert resp.status_code == 404

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_other_user_cannot_stop_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 1000
        client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.post(f"/api/bots/{bot_id}/stop", headers=other)
        assert resp.status_code == 404

    def test_other_user_cannot_get_logs(self, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        other = _register_user(client, "other", "other@test.com", "pass123")
        resp = client.get(f"/api/bots/{bot_id}/logs", headers=other)
        assert resp.status_code == 404


class TestAdminAccess:
    """Admin can access and control any bot regardless of ownership."""

    def test_admin_can_get_other_bot(self, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}", headers=admin_header)
        assert resp.status_code == 200

    def test_admin_can_update_other_bot(self, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.put(f"/api/bots/{bot_id}", json={"name": "admin_renamed"}, headers=admin_header)
        assert resp.status_code == 200
        assert resp.json()["name"] == "admin_renamed"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_admin_can_start_other_bot(self, mock_mgr, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 2000
        resp = client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_admin_can_stop_other_bot(self, mock_mgr, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 2000
        client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
        resp = client.post(f"/api/bots/{bot_id}/stop", headers=admin_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    def test_admin_can_delete_other_bot(self, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        assert resp.status_code == 204

    def test_admin_can_get_other_bot_logs(self, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}/logs", headers=admin_header)
        assert resp.status_code == 200

    def test_admin_can_get_other_bot_state(self, client, auth_header, admin_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.get(f"/api/bots/{bot_id}/state", headers=admin_header)
        assert resp.status_code == 200

    def test_admin_cannot_get_nonexistent_bot(self, client, admin_header):
        resp = client.get("/api/bots/99999", headers=admin_header)
        assert resp.status_code == 404
