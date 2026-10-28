"""AC-8 end to end: a registered non-Infoflow platform completes the whole lifecycle.

One `fakeim` bot is driven through the *real* routes — the signed webhook confirms a
binding, the confirmed credential runs an allowlisted command, that command notifies the
group, and the resource snapshot reflects it — with no Infoflow payload shape and no
Infoflow field name anywhere in any response or anything the adapter sent.
"""

import json
import logging
from unittest.mock import patch

import pytest
from fake_adapter import VALID_SIGNATURE, FakeAdapter, encode_message
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.core.platforms.registry import register_adapter, unregister_adapter

ACTOR = "alice"
INFOFLOW_FIELDS = ("fromuserid", "atuserids", '"body"', '"header"', "toid", "message")


@pytest.fixture
def fakeim_api(client, auth_header, db_session, monkeypatch, session_factory, tmp_path):
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    register_adapter(FakeAdapter)

    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "fakeimbot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1", "n2": "m2"},
        },
    )
    assert created.status_code == 201, created.text
    bot_id = created.json()["id"]
    client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})

    bot_row = db_session.get(Bot, bot_id)
    bot_row.platform = FakeAdapter.platform_name
    db_session.commit()

    manager = BotManager(max_inflight=5, drain_timeout=2.0)
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": "NODE",
            "BOT_NAME": "fakeimbot",
            "BOT_ID": bot_id,
            "PLATFORM": FakeAdapter.platform_name,
            "_IDENTITY_REVISION": 0,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"},
        },
    )
    bot = manager.get_instance(bot_id).bot
    assert isinstance(bot.adapter, FakeAdapter)

    with (
        patch("lockbot.backend.app.bots.router.bot_manager", manager),
        patch("lockbot.backend.app.resource_api.commands.bot_manager", manager),
        patch("lockbot.backend.app.resource_api.resources.bot_manager", manager),
    ):
        yield {"bot_id": bot_id, "bot": bot, "manager": manager}
    manager.shutdown_all()
    unregister_adapter(FakeAdapter.platform_name)


def _confirm_via_webhook(client, bot_id, bind_code):
    return client.post(
        f"/api/bots/webhook/{bot_id}",
        params={"signature": VALID_SIGNATURE},
        content=encode_message(f"bind {bind_code}", actor=ACTOR),
    )


def test_fakeim_bind_command_notify_and_snapshot(client, fakeim_api, db_session):
    bot_id = fakeim_api["bot_id"]
    bot = fakeim_api["bot"]

    issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()

    # 1. The signed fakeim webhook confirms the binding durably.
    webhook_resp = _confirm_via_webhook(client, bot_id, issued["bind_code"])
    assert webhook_resp.status_code == 200, webhook_resp.text

    status = client.get(f"/api/resource-auth/bindings/{issued['binding_id']}").json()
    assert status["status"] == "confirmed"

    # 2. The credential, inert until that confirmation, now runs an allowlisted command.
    headers = {"Authorization": f"Bearer {issued['credential']}"}
    cmd = client.post(f"/api/bots/{bot_id}/commands", headers=headers, json={"command": "lock n1 1h"})
    assert cmd.status_code == 200, cmd.text
    assert cmd.json()["platform"] == FakeAdapter.platform_name
    holders = [u["user_id"] for u in bot.state.bot_state["n1"]["current_users"]]
    assert holders == [ACTOR], "the command acted as the webhook-verified identity"

    # 3. The command notified the group through the fakeim adapter (not Infoflow).
    assert bot.adapter.sent, "locking should have sent a group notification"

    # 4. The snapshot reflects the lock.
    resources = client.get(f"/api/bots/{bot_id}/resources", headers=headers)
    assert resources.status_code == 200, resources.text
    assert json.dumps(resources.json()).find("n1") != -1

    # 5. Nothing in the whole path carried an Infoflow payload shape.
    blobs = [webhook_resp.text, cmd.text, resources.text, json.dumps(bot.adapter.sent)]
    for blob in blobs:
        for field in INFOFLOW_FIELDS:
            assert field not in blob, f"{field!r} leaked into {blob[:80]!r}"


def test_fakeim_credential_is_inert_until_the_webhook_confirms(client, fakeim_api):
    bot_id = fakeim_api["bot_id"]
    issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
    headers = {"Authorization": f"Bearer {issued['credential']}"}

    before = client.post(f"/api/bots/{bot_id}/commands", headers=headers, json={"command": "query"})
    assert before.status_code == 401, "an unconfirmed credential must not work"

    _confirm_via_webhook(client, bot_id, issued["bind_code"])
    after = client.post(f"/api/bots/{bot_id}/commands", headers=headers, json={"command": "query"})
    assert after.status_code == 200, after.text


def _command_log(tmp_path, bot_id):
    path = tmp_path / "bots" / str(bot_id) / "logs.jsonl"
    return path.read_text() if path.exists() else ""


def _all_sinks(resp, bot, tmp_path, bot_id, caplog, db_session):
    from lockbot.backend.app.audit.models import AuditLog

    audit = "".join(f"{r.action}{r.detail}{r.target_name}" for r in db_session.query(AuditLog).all())
    return [resp.text, _command_log(tmp_path, bot_id), caplog.text, json.dumps(bot.adapter.sent), audit]


# PLACEHOLDER_REDACTION


class TestBindCodeNeverLeaksThroughTheRealRoute:
    """AC-10 at the real webhook route: no bind argument in any sink, for every outcome."""

    def test_normal_bind(self, client, fakeim_api, tmp_path, caplog, db_session):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
        code = issued["bind_code"]
        with caplog.at_level(logging.DEBUG):
            resp = _confirm_via_webhook(client, bot_id, code)
        assert resp.status_code == 200
        for sink in _all_sinks(resp, bot, tmp_path, bot_id, caplog, db_session):
            assert code not in sink
        assert "[redacted]" in _command_log(tmp_path, bot_id), "the command was logged, but redacted"

    def test_enrollment_disabled_bind(self, client, fakeim_api, auth_header, tmp_path, caplog, db_session):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": False})
        issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
        code = issued["bind_code"]
        with caplog.at_level(logging.DEBUG):
            resp = _confirm_via_webhook(client, bot_id, code)
        assert resp.status_code == 200
        for sink in _all_sinks(resp, bot, tmp_path, bot_id, caplog, db_session):
            assert code not in sink

    def test_malformed_bind(self, client, fakeim_api, tmp_path, caplog, db_session):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        bogus = "NOTACODE9"
        with caplog.at_level(logging.DEBUG):
            resp = _confirm_via_webhook(client, bot_id, bogus)
        assert resp.status_code == 200
        for sink in _all_sinks(resp, bot, tmp_path, bot_id, caplog, db_session):
            assert bogus not in sink

    def test_throttled_bind(self, client, fakeim_api, tmp_path, caplog, db_session):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        bogus = "THROTTLE1"
        with caplog.at_level(logging.DEBUG):
            for _ in range(8):  # enough repeated failures to trip the per-sender throttle
                resp = _confirm_via_webhook(client, bot_id, bogus)
        assert resp.status_code == 200
        for sink in _all_sinks(resp, bot, tmp_path, bot_id, caplog, db_session):
            assert bogus not in sink


class TestRouteRefusesUnprovenCallbacks:
    """A bad signature or an undecryptable body must claim nothing at the real route."""

    def test_invalid_signature_claims_nothing(self, client, fakeim_api):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
        bot.adapter.sent.clear()

        resp = client.post(
            f"/api/bots/webhook/{bot_id}",
            params={"signature": "wrong"},
            content=encode_message(f"bind {issued['bind_code']}", actor=ACTOR),
        )
        assert resp.status_code == 401
        assert bot.adapter.sent == []
        assert client.get(f"/api/resource-auth/bindings/{issued['binding_id']}").json()["status"] == "pending"
        # The code was never consumed: a proper callback still confirms it.
        assert _confirm_via_webhook(client, bot_id, issued["bind_code"]).status_code == 200
        assert client.get(f"/api/resource-auth/bindings/{issued['binding_id']}").json()["status"] == "confirmed"

    def test_undecryptable_body_claims_nothing(self, client, fakeim_api):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
        bot.adapter.sent.clear()

        resp = client.post(
            f"/api/bots/webhook/{bot_id}",
            params={"signature": VALID_SIGNATURE},
            content=b"not-json",
        )
        assert resp.status_code == 400
        assert bot.adapter.sent == []
        assert client.get(f"/api/resource-auth/bindings/{issued['binding_id']}").json()["status"] == "pending"
        assert _confirm_via_webhook(client, bot_id, issued["bind_code"]).status_code == 200


class TestWebhookRunsUnderAdmission:
    """AC-7: a webhook on a retiring bot must not dispatch to a draining instance."""

    def test_a_retiring_bot_does_not_execute_a_webhook_command(self, client, fakeim_api):
        bot_id, bot, manager = fakeim_api["bot_id"], fakeim_api["bot"], fakeim_api["manager"]
        manager._begin_retiring(bot_id)

        resp = client.post(
            f"/api/bots/webhook/{bot_id}",
            params={"signature": VALID_SIGNATURE},
            content=encode_message("lock n1 1h", actor=ACTOR),
        )

        assert resp.status_code == 200  # the friendly "not running" reply, not a dispatch
        holders = [u["user_id"] for u in bot.state.bot_state["n1"]["current_users"]]
        assert holders == [], "a retiring bot must not have executed the lock"

    def test_a_running_bot_still_executes_a_webhook_command(self, client, fakeim_api):
        bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
        resp = client.post(
            f"/api/bots/webhook/{bot_id}",
            params={"signature": VALID_SIGNATURE},
            content=encode_message("lock n1 1h", actor=ACTOR),
        )
        assert resp.status_code == 200
        holders = [u["user_id"] for u in bot.state.bot_state["n1"]["current_users"]]
        assert holders == [ACTOR]


class TestLegacyConsumersRunUnderAdmission:
    """M3/AC-7: the legacy service-key routes read/mutate under the admission lease, so a
    retiring bot is refused rather than touching a draining instance."""

    def _api_key(self, client, auth_header, bot_id):
        return client.post(f"/api/bots/{bot_id}/api-key", headers=auth_header).json()["api_key"]

    def test_locked_users_refuses_a_retiring_bot(self, client, fakeim_api, auth_header):
        bot_id, manager = fakeim_api["bot_id"], fakeim_api["manager"]
        key = self._api_key(client, auth_header, bot_id)

        ok = client.get(f"/api/bots/{bot_id}/locked-users", headers={"Authorization": f"Bearer {key}"})
        assert ok.status_code == 200  # running: a real snapshot

        manager._begin_retiring(bot_id)
        retiring = client.get(f"/api/bots/{bot_id}/locked-users", headers={"Authorization": f"Bearer {key}"})
        assert retiring.status_code == 503
        assert retiring.json()["detail"]["code"] == "instance_retiring"

    def test_opkick_refuses_a_retiring_bot_before_mutating(self, client, fakeim_api, auth_header):
        bot_id, bot, manager = fakeim_api["bot_id"], fakeim_api["bot"], fakeim_api["manager"]
        key = self._api_key(client, auth_header, bot_id)
        # Put a holder on n1 so a kick would have something to do if it were (wrongly) executed.
        bot.state.bot_state["n1"]["status"] = "exclusive"
        bot.state.bot_state["n1"]["current_users"].append(
            {"user_id": ACTOR, "start_time": 1700000000, "duration": 3600, "is_notified": False}
        )

        manager._begin_retiring(bot_id)
        resp = client.post(
            f"/api/bots/{bot_id}/opkick",
            headers={"Authorization": f"Bearer {key}"},
            json={"target_user": ACTOR},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "instance_retiring"
        holders = [u["user_id"] for u in bot.state.bot_state["n1"]["current_users"]]
        assert holders == [ACTOR], "a retiring bot must not have executed the kick"
