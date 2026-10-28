"""
Tests for bot lifecycle API endpoints (start/stop/restart).
"""

from unittest.mock import patch

BOT_PAYLOAD = {
    "name": "lifecycle_bot",
    "bot_type": "NODE",
    "webhook_url": "https://example.com/hook",
    "cluster_configs": {"n1": "n1"},
}


def _create_bot(client, auth_header):
    """Helper: create a bot and return its id."""
    resp = client.post("/api/bots", json=BOT_PAYLOAD, headers=auth_header)
    assert resp.status_code == 201
    return resp.json()["id"]


class TestStartBot:
    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_start_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 9999

        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["pid"] == 9999
        assert data["consecutive_failures"] == 0
        assert data["message"] == "Bot started"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_start_already_running(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 1000

        # First start
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Bot started"

        # Second start — the manager now reports it running, so the route short-circuits on
        # manager ownership (not the DB row).
        mock_mgr.is_running.return_value = True
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Bot is already running"

    def test_start_bot_not_found(self, client, auth_header):
        resp = client.post("/api/bots/9999/start", headers=auth_header)
        assert resp.status_code == 404

    def test_start_bot_no_auth(self, client):
        resp = client.post("/api/bots/1/start")
        assert resp.status_code == 401

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_single_failure_not_error(self, mock_mgr, client, auth_header):
        """One failure should not set error status."""
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("init failed")

        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 409

        # Bot should still be stopped, not error
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["status"] == "stopped"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_three_failures_marks_error(self, mock_mgr, client, auth_header):
        """After 3 consecutive failures, bot should be marked as error."""
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("init failed")

        for _ in range(3):
            resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
            assert resp.status_code == 409

        # After 3 failures, status should be error
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["status"] == "error"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_failure_count_resets_on_success(self, mock_mgr, client, auth_header):
        """A successful start resets the failure counter."""
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("init failed")

        # Fail twice
        for _ in range(2):
            client.post(f"/api/bots/{bot_id}/start", headers=auth_header)

        # Succeed
        mock_mgr.start_bot.side_effect = None
        mock_mgr.start_bot.return_value = 5000
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["consecutive_failures"] == 0

        # Stop it so we can test failures again
        mock_mgr.is_running.return_value = True
        client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)

        # Now fail 3 more times — counter was reset so need 3 to reach error
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("fail again")
        for _i in range(3):
            resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
            assert resp.status_code == 409

        # 3 new failures since reset → error
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
        assert detail["status"] == "error"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_error_bot_can_be_started_after_fix(self, mock_mgr, client, auth_header):
        """An error bot can start again once the underlying issue is fixed."""
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("broken")

        # Fail 3 times → error
        for _ in range(3):
            client.post(f"/api/bots/{bot_id}/start", headers=auth_header)

        assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["status"] == "error"

        # Fix the issue — now start succeeds
        mock_mgr.start_bot.side_effect = None
        mock_mgr.start_bot.return_value = 6000
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        assert resp.json()["consecutive_failures"] == 0


class TestStopBot:
    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_stop_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 2000

        client.post(f"/api/bots/{bot_id}/start", headers=auth_header)

        resp = client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        assert data["pid"] is None

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_stop_not_running(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        resp = client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)
        assert resp.status_code == 409

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_stop_error_bot_resets_failures(self, mock_mgr, client, auth_header):
        """Stopping an error bot should reset its status and failure count."""
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = RuntimeError("broken")

        for _ in range(3):
            client.post(f"/api/bots/{bot_id}/start", headers=auth_header)

        assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["status"] == "error"

        # Stop the error bot
        resp = client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"


class TestRestartBot:
    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_restart_running_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        # The restart route owns the transition and orchestrates stop-then-start itself (draining
        # before it reads replacement config), so it is the start half that returns the new pid.
        mock_mgr.start_bot.return_value = 3001

        client.post(f"/api/bots/{bot_id}/start", headers=auth_header)

        resp = client.post(f"/api/bots/{bot_id}/restart", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["pid"] == 3001

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_restart_stopped_bot(self, mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.return_value = 4000

        resp = client.post(f"/api/bots/{bot_id}/restart", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["pid"] == 4000


class TestLifecycleMayHaveExecuted:
    """M5/AC-7: when a drain times out, lifecycle routes must not report a clean transition.
    They preserve the DB lifecycle status and return the machine-readable may_have_executed
    signal so the caller inspects /resources instead of assuming the operation finished."""

    @staticmethod
    def _start_running(mock_mgr, client, auth_header):
        bot_id = _create_bot(client, auth_header)
        mock_mgr.is_running.return_value = False
        mock_mgr.start_bot.side_effect = None
        mock_mgr.start_bot.return_value = 2000
        resp = client.post(f"/api/bots/{bot_id}/start", headers=auth_header)
        assert resp.status_code == 200
        return bot_id

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_stop_timeout_preserves_status(self, mock_mgr, client, auth_header):
        from lockbot.backend.app.bots.manager import MayHaveExecuted

        bot_id = self._start_running(mock_mgr, client, auth_header)
        mock_mgr.stop_bot.side_effect = MayHaveExecuted(bot_id)

        resp = client.post(f"/api/bots/{bot_id}/stop", headers=auth_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "may_have_executed"
        # The row must NOT have been marked stopped.
        assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["status"] == "running"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_delete_timeout_does_not_delete(self, mock_mgr, client, auth_header):
        from lockbot.backend.app.bots.manager import MayHaveExecuted

        bot_id = self._start_running(mock_mgr, client, auth_header)
        mock_mgr.stop_bot.side_effect = MayHaveExecuted(bot_id)

        resp = client.delete(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "may_have_executed"
        # The bot must still exist and not be stopped.
        detail = client.get(f"/api/bots/{bot_id}", headers=auth_header)
        assert detail.status_code == 200
        assert detail.json()["status"] == "running"

    @patch("lockbot.backend.app.bots.router.bot_manager")
    def test_restart_timeout_reports_uncertainty(self, mock_mgr, client, auth_header):
        from lockbot.backend.app.bots.manager import MayHaveExecuted

        bot_id = self._start_running(mock_mgr, client, auth_header)
        # The restart route drains (stop) before building replacement config, so a drain timeout
        # surfaces from the stop half.
        mock_mgr.stop_bot.side_effect = MayHaveExecuted(bot_id)

        resp = client.post(f"/api/bots/{bot_id}/restart", headers=auth_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "may_have_executed"
        # No replacement was published; the row stays as it was (running).
        assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["status"] == "running"
