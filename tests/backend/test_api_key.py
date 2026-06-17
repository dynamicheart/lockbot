"""Tests for API Key management and GET /locked-users endpoint."""

from unittest.mock import MagicMock, patch


def _sample_bot(name="apibot"):
    return {
        "name": name,
        "bot_type": "NODE",
        "webhook_url": "https://example.com/webhook",
        "aes_key": "testaeskey",
        "token": "testtoken",
        "cluster_configs": ["gpu01", "gpu02"],
    }


def _create_bot(client, admin_header, bot_data=None):
    resp = client.post("/api/bots", json=bot_data or _sample_bot(), headers=admin_header)
    assert resp.status_code == 201
    return resp.json()["id"]


class TestApiKey:
    def test_generate_api_key(self, client, admin_header):
        bot_id = _create_bot(client, admin_header)
        resp = client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"].startswith("lbk_")
        assert len(data["api_key"]) == 4 + 48  # lbk_ + 24 bytes hex

    def test_revoke_api_key(self, client, admin_header):
        bot_id = _create_bot(client, admin_header)
        client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        resp = client.delete(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        assert resp.status_code == 204

    def test_regenerate_invalidates_old(self, client, admin_header):
        bot_id = _create_bot(client, admin_header)
        r1 = client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        old_key = r1.json()["api_key"]
        r2 = client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        new_key = r2.json()["api_key"]
        assert old_key != new_key


class TestLockedUsers:
    def _setup_bot_with_key(self, client, admin_header, bot_type="NODE", cluster=None):
        bot_data = _sample_bot()
        bot_data["bot_type"] = bot_type
        if cluster:
            bot_data["cluster_configs"] = cluster
        bot_id = _create_bot(client, admin_header, bot_data)
        resp = client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        api_key = resp.json()["api_key"]
        return bot_id, api_key

    def test_no_auth_returns_401(self, client, admin_header):
        bot_id = _create_bot(client, admin_header)
        resp = client.get(f"/api/bots/{bot_id}/locked-users")
        assert resp.status_code == 401

    def test_invalid_key_returns_401(self, client, admin_header):
        bot_id = _create_bot(client, admin_header)
        client.post(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        resp = client.get(
            f"/api/bots/{bot_id}/locked-users",
            headers={"Authorization": "Bearer lbk_invalid"},
        )
        assert resp.status_code == 401

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_node_bot_response_format(self, mock_mgr, client, admin_header):
        bot_id, api_key = self._setup_bot_with_key(client, admin_header)

        # Mock running bot with state
        mock_instance = MagicMock()
        mock_instance.bot.state.bot_state = {
            "gpu01": {
                "status": "exclusive",
                "current_users": [{"user_id": "zhangsan", "start_time": 1750000000, "duration": 7200}],
                "booking_list": [],
            },
            "gpu02": {
                "status": "idle",
                "current_users": [],
                "booking_list": [],
            },
        }
        mock_mgr.get_instance.return_value = mock_instance

        resp = client.get(
            f"/api/bots/{bot_id}/locked-users",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Verify top-level structure
        assert data["bot_id"] == bot_id
        assert data["bot_type"] == "NODE"
        assert "nodes" in data

        # Verify node structure matches docs
        assert "gpu01" in data["nodes"]
        gpu01 = data["nodes"]["gpu01"]
        assert gpu01["status"] == "exclusive"
        assert len(gpu01["current_users"]) == 1
        assert gpu01["current_users"][0] == {
            "user_id": "zhangsan",
            "start_time": 1750000000,
            "duration": 7200,
        }

        # idle node
        assert data["nodes"]["gpu02"]["status"] == "idle"
        assert data["nodes"]["gpu02"]["current_users"] == []

        # booking_list should NOT be exposed
        assert "booking_list" not in data["nodes"]["gpu01"]

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_device_bot_response_format(self, mock_mgr, client, admin_header):
        bot_id, api_key = self._setup_bot_with_key(
            client,
            admin_header,
            bot_type="DEVICE",
            cluster={"gpu01": ["A100", "A100"]},
        )

        mock_instance = MagicMock()
        mock_instance.bot.state.bot_state = {
            "gpu01": [
                {
                    "dev_id": 0,
                    "status": "exclusive",
                    "current_users": [{"user_id": "zhangsan", "start_time": 1750000000, "duration": 7200}],
                },
                {"dev_id": 1, "status": "idle", "current_users": []},
            ]
        }
        mock_mgr.get_instance.return_value = mock_instance

        resp = client.get(
            f"/api/bots/{bot_id}/locked-users",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["bot_type"] == "DEVICE"
        assert isinstance(data["nodes"]["gpu01"], list)
        assert data["nodes"]["gpu01"][0]["dev_id"] == 0
        assert data["nodes"]["gpu01"][0]["status"] == "exclusive"
        assert data["nodes"]["gpu01"][0]["current_users"][0]["user_id"] == "zhangsan"
        assert data["nodes"]["gpu01"][1]["dev_id"] == 1
        assert data["nodes"]["gpu01"][1]["status"] == "idle"
        assert data["nodes"]["gpu01"][1]["current_users"] == []

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_bot_not_running_returns_503(self, mock_mgr, client, admin_header):
        bot_id, api_key = self._setup_bot_with_key(client, admin_header)
        mock_mgr.get_instance.return_value = None

        resp = client.get(
            f"/api/bots/{bot_id}/locked-users",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 503

    def test_revoked_key_returns_401(self, client, admin_header):
        bot_id, api_key = self._setup_bot_with_key(client, admin_header)
        client.delete(f"/api/bots/{bot_id}/api-key", headers=admin_header)
        resp = client.get(
            f"/api/bots/{bot_id}/locked-users",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 401
