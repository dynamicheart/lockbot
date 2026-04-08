"""
Tests for webhook group_id / last_user_id recording.

Verifies that a bot appearing in multiple group chats has all group IDs
correctly stored as comma-separated, sorted, deduplicated values.
"""

from unittest.mock import MagicMock, patch

BOT_PAYLOAD = {
    "name": "webhook_bot",
    "bot_type": "NODE",
    "webhook_url": "https://example.com/hook",
    "cluster_configs": {"n1": "n1"},
}


def _create_and_start_bot(client, auth_header):
    """Create a bot, start it (mocked), return bot_id."""
    resp = client.post("/api/bots", json=BOT_PAYLOAD, headers=auth_header)
    assert resp.status_code == 201
    bot_id = resp.json()["id"]
    return bot_id


def _mock_instance():
    """Create a mock bot_manager instance with a state attribute."""
    mock = MagicMock()
    mock.state.bot_state = {}
    return mock


class TestWebhookGroupIdRecording:
    """Test that group_id is accumulated correctly across multiple webhooks."""

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_single_group_id_recorded(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})

        resp = client.post(f"/api/bots/webhook/{bot_id}")
        assert resp.status_code == 200

        # Verify group_id stored in DB
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "1001"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_multiple_group_ids_accumulated(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # First webhook from group 1001
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        # Second webhook from group 2002
        mock_handler.return_value = ("ok", 200, {"user_id": "bob", "group_id": "2002", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        # Third webhook from group 1500
        mock_handler.return_value = ("ok", 200, {"user_id": "carol", "group_id": "1500", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        # group_id should be sorted, deduplicated, comma-separated
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "1001,1500,2002"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_duplicate_group_id_not_duplicated(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # Same group_id twice
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "1001"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_group_id_reappearance_keeps_sorted(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # Groups arrive out of order, with repeats
        for gid in ["5000", "1000", "3000", "1000", "5000", "2000"]:
            mock_handler.return_value = ("ok", 200, {"user_id": "user1", "group_id": gid, "command": ""})
            client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "1000,2000,3000,5000"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_empty_group_id_does_not_overwrite(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # First: valid group_id
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        # Second: empty group_id (private chat or missing header)
        mock_handler.return_value = ("ok", 200, {"user_id": "bob", "group_id": "", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        # group_id should remain "1001", not overwritten by empty
        assert detail["group_id"] == "1001"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_none_group_id_does_not_overwrite(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # First: valid group_id
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        # Second: no group_id in meta
        mock_handler.return_value = ("ok", 200, {"user_id": "bob", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["group_id"] == "1001"


class TestWebhookLastUserId:
    """Test that last_user_id is updated on each webhook."""

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_last_user_id_updated(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["last_user_id"] == "alice"

        # Second webhook from different user
        mock_handler.return_value = ("ok", 200, {"user_id": "bob", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["last_user_id"] == "bob"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_last_user_id_null_without_user(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # No user_id in meta
        mock_handler.return_value = ("ok", 200, {"group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["last_user_id"] is None


class TestWebhookCommandLog:
    """Test that command logs are written correctly."""

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_command_logged(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": "lock n1"})
        client.post(f"/api/bots/webhook/{bot_id}")

        logs = client.get(f"/api/bots/{bot_id}/logs", headers=auth_header).json()
        command_logs = [e for e in logs if e["category"] == "command"]
        assert len(command_logs) == 1
        assert "[alice] lock n1" in command_logs[0]["message"]

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_empty_command_not_logged(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # Empty command — should NOT create a command log
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        logs = client.get(f"/api/bots/{bot_id}/logs", headers=auth_header).json()
        command_logs = [e for e in logs if e["category"] == "command"]
        assert len(command_logs) == 0

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_unknown_user_in_command_log(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # No user_id but has command
        mock_handler.return_value = ("ok", 200, {"group_id": "1001", "command": "status"})
        client.post(f"/api/bots/webhook/{bot_id}")

        logs = client.get(f"/api/bots/{bot_id}/logs", headers=auth_header).json()
        command_logs = [e for e in logs if e["category"] == "command"]
        assert len(command_logs) == 1
        assert "[unknown] status" in command_logs[0]["message"]


class TestWebhookLastRequestAt:
    """Test that last_request_at is updated on each webhook."""

    @patch("lockbot.backend.app.bots.router.bot_manager")
    @patch("lockbot.backend.app.bots.router.handle_webhook")
    def test_last_request_at_updated(self, mock_handler, mock_mgr, client, auth_header):
        bot_id = _create_and_start_bot(client, auth_header)
        mock_mgr.get_instance.return_value = _mock_instance()

        # Before webhook, last_request_at should be None
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["last_request_at"] is None

        # After webhook
        mock_handler.return_value = ("ok", 200, {"user_id": "alice", "group_id": "1001", "command": ""})
        client.post(f"/api/bots/webhook/{bot_id}")

        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["last_request_at"] is not None
