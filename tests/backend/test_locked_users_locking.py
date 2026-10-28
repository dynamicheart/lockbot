"""AC-6.1: the legacy `locked-users` endpoint must read under the state lock.

It previously read `instance.bot.state.bot_state` live. These tests pin both halves of the
fix: the response shape and the legacy service-key auth are unchanged, and the endpoint can
no longer observe a multi-resource mutation half-applied.
"""

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from lockbot.core.bot_instance import BotInstance

TIMEOUT = 5.0


@pytest.fixture
def running_bot(client, auth_header, tmp_path):
    """A bot that exists in the database, is 'running', and has a legacy API key."""
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "lockedbot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1", "n2": "m2"},
        },
    )
    assert created.status_code == 201, created.text
    bot_id = created.json()["id"]

    key = client.post(f"/api/bots/{bot_id}/api-key", headers=auth_header).json()["api_key"]

    instance = BotInstance(
        "NODE",
        {
            "BOT_NAME": "lockedbot",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "state"),
            "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"},
        },
        auto_start=False,
    )
    return {"bot_id": bot_id, "key": key, "instance": instance}


@pytest.fixture
def served(running_bot):
    """Point the route's bot_manager at the real instance."""
    instance = running_bot["instance"]
    with patch("lockbot.backend.app.bots.router.bot_manager") as manager:
        manager.get_instance.return_value = instance
        manager.is_running.return_value = True

        @contextmanager
        def _admit(bot_id, enforce_cap=True, for_read=False):
            # The endpoint reads under a lease now (AC-7); expose the real instance so the
            # real state_snapshot() still runs under the real bot._lock.
            yield SimpleNamespace(bot=instance.bot, instance=instance, bot_id=bot_id, generation=0)

        manager.admit.side_effect = _admit
        yield running_bot


def _get(client, served):
    return client.get(
        f"/api/bots/{served['bot_id']}/locked-users",
        headers={"Authorization": f"Bearer {served['key']}"},
    )


class TestUnchangedContract:
    def test_response_shape_is_preserved(self, client, served):
        resp = _get(client, served)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"bot_id", "bot_type", "nodes"}
        assert body["bot_type"] == "NODE"
        assert set(body["nodes"]) == {"n1", "n2"}
        assert set(body["nodes"]["n1"]) == {"status", "current_users"}

    def test_holders_are_reported(self, client, served):
        served["instance"].bot.state.bot_state["n1"]["status"] = "exclusive"
        served["instance"].bot.state.bot_state["n1"]["current_users"].append(
            {"user_id": "alice", "start_time": 1700000000, "duration": 3600, "is_notified": False}
        )
        body = _get(client, served).json()
        holder = body["nodes"]["n1"]["current_users"][0]
        assert holder == {"user_id": "alice", "start_time": 1700000000, "duration": 3600}

    def test_legacy_service_key_is_still_required(self, client, served):
        assert client.get(f"/api/bots/{served['bot_id']}/locked-users").status_code == 401

    def test_a_wrong_service_key_is_rejected(self, client, served):
        resp = client.get(
            f"/api/bots/{served['bot_id']}/locked-users",
            headers={"Authorization": "Bearer lbk_wrong"},
        )
        assert resp.status_code == 401


class TestNoTornRead:
    def test_endpoint_cannot_observe_a_half_applied_change(self, client, served):
        """Deterministic interleaving: the reader must wait out the whole mutation."""
        bot = served["instance"].bot
        inside = threading.Event()
        may_finish = threading.Event()
        writer_done = threading.Event()

        def writer():
            with bot._lock:
                bot.state.bot_state["n1"]["status"] = "exclusive"
                inside.set()
                may_finish.wait(TIMEOUT)
                bot.state.bot_state["n2"]["status"] = "exclusive"
            writer_done.set()

        response = {}

        def reader():
            response["resp"] = _get(client, served)

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        assert inside.wait(TIMEOUT), "writer never entered its critical section"

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        reader_thread.join(0.5)

        assert reader_thread.is_alive(), "the endpoint read without waiting for the state lock"
        assert not writer_done.is_set()

        may_finish.set()
        writer_thread.join(TIMEOUT)
        reader_thread.join(TIMEOUT)

        nodes = response["resp"].json()["nodes"]
        assert nodes["n1"]["status"] == "exclusive"
        assert nodes["n2"]["status"] == "exclusive", "endpoint returned a torn state"

    def test_response_is_built_from_a_detached_copy(self, client, served):
        """Mutating live state after the call must not retroactively change the response."""
        body = _get(client, served).json()
        served["instance"].bot.state.bot_state["n1"]["status"] = "exclusive"
        assert body["nodes"]["n1"]["status"] == "idle"
