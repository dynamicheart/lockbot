"""M2/B17: live edits hold the admission lease through durable persistence, and a retiring or
full live generation returns the structured transient error instead of silently succeeding."""

import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lockbot.backend.app.bots.manager import AdmissionFull, InstanceRetiring
from lockbot.core.config import Config


def _create_node_bot(client, admin_header, tmp_path):
    resp = client.post(
        "/api/bots",
        json={
            "name": "editbot",
            "bot_type": "NODE",
            "webhook_url": "https://example.com/webhook",
            "cluster_configs": ["n1", "n2"],
        },
        headers=admin_header,
    )
    assert resp.status_code == 201, resp.text
    bot_id = resp.json()["id"]
    return bot_id


_VALID_STATE = {
    "n1": {"status": "idle", "current_users": [], "booking_list": []},
    "n2": {"status": "idle", "current_users": [], "booking_list": []},
}


class TestUpdateStateHoldsLeaseThroughPersistence:
    def test_state_file_is_written_before_the_lease_is_released(self, client, admin_header, tmp_path):
        bot_id = _create_node_bot(client, admin_header, tmp_path)
        state_file = os.path.join(str(tmp_path), "bots", str(bot_id), "bot_state.json")
        observed = {}

        @contextmanager
        def admit(_bot_id, enforce_cap=True, for_read=False):
            lease = MagicMock()
            lease.bot._lock = threading.Lock()
            lease.bot.state = SimpleNamespace(bot_state={})
            # Persistence uses the admitted generation's real config, as in production.
            # A MagicMock config invents an observer and is not a valid bot generation.
            lease.bot.config = Config({"BOT_ID": bot_id, "DATA_DIR": str(tmp_path / "bots")})
            try:
                yield lease
            finally:
                # If the route released the lease before persisting (the B17 bug), the file
                # would not exist yet. The fix writes it while the lease is still held.
                observed["file_present_at_lease_exit"] = os.path.exists(state_file)

        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.admit.side_effect = admit
            resp = client.put(f"/api/bots/{bot_id}/state", json=_VALID_STATE, headers=admin_header)

        assert resp.status_code == 200, resp.text
        assert observed["file_present_at_lease_exit"] is True


class TestLiveEditRefusesRetiringOrFull:
    def test_update_state_retiring_returns_transient_error(self, client, admin_header, tmp_path):
        bot_id = _create_node_bot(client, admin_header, tmp_path)
        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.admit.side_effect = InstanceRetiring("retiring")
            resp = client.put(f"/api/bots/{bot_id}/state", json=_VALID_STATE, headers=admin_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "instance_retiring"

    def test_update_state_full_returns_transient_error(self, client, admin_header, tmp_path):
        bot_id = _create_node_bot(client, admin_header, tmp_path)
        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.admit.side_effect = AdmissionFull("full")
            resp = client.put(f"/api/bots/{bot_id}/state", json=_VALID_STATE, headers=admin_header)
        assert resp.status_code == 429
        assert resp.json()["detail"]["code"] == "admission_full"

    def test_set_language_retiring_returns_transient_error(self, client, admin_header, tmp_path):
        bot_id = _create_node_bot(client, admin_header, tmp_path)
        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.admit.side_effect = InstanceRetiring("retiring")
            resp = client.put(f"/api/bots/{bot_id}/language", json={"language": "en"}, headers=admin_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "instance_retiring"

    def test_get_state_retiring_returns_transient_error(self, client, admin_header, tmp_path):
        bot_id = _create_node_bot(client, admin_header, tmp_path)
        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.admit.side_effect = InstanceRetiring("retiring")
            resp = client.get(f"/api/bots/{bot_id}/state", headers=admin_header)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "instance_retiring"
