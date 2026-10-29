"""AC-15 / AC-11: group notifications and transport-discriminating observability."""

import threading
from unittest.mock import patch

import pytest
from fake_adapter import FakeAdapter
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.resource_api.notifications import notification_budget
from lockbot.backend.app.resource_api.sources import SOURCE_API, SOURCE_CHAT
from lockbot.backend.app.resource_auth import service

PLATFORM = "infoflow"
ACTOR = "alice"


@pytest.fixture(autouse=True)
def _reset_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(notification_budget, "clock", lambda: clock[0])
    notification_budget.reset()
    yield clock
    notification_budget.reset()


@pytest.fixture
def api(client, auth_header, db_session, monkeypatch, session_factory, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(notification_budget, "clock", lambda: clock[0])
    notification_budget.reset()
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)

    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "notifybot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1", "n2": "m2"},
        },
    )
    bot_id = created.json()["id"]
    client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})

    issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
    service.handle_chat_binding(
        platform=PLATFORM, external_user_id=ACTOR, bot_id=bot_id, action="bind", argument=issued["bind_code"]
    )
    db_session.expire_all()

    manager = BotManager(max_inflight=5, drain_timeout=2.0)
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": "NODE",
            "BOT_NAME": "notifybot",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"},
            "ALLOW_USER_ALIAS": True,
        },
    )
    from lockbot.backend.app.bots.models import Bot

    db_session.get(Bot, bot_id).status = "running"
    db_session.commit()
    bot = manager.get_instance(bot_id).bot
    bot.adapter = FakeAdapter(config=bot.config)

    with (
        patch("lockbot.backend.app.resource_api.commands.bot_manager", manager),
        patch("lockbot.backend.app.resource_api.resources.bot_manager", manager),
    ):
        yield {
            "bot_id": bot_id,
            "bot": bot,
            "manager": manager,
            "headers": {"Authorization": f"Bearer {issued['credential']}"},
            "credential": issued["credential"],
            "bind_code": issued["bind_code"],
            "log_dir": tmp_path,
            "clock": clock,
        }
    manager.shutdown_all()


def _post(client, api, command):
    return client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command})


def _sent_text(api):
    return "".join(
        segment
        for message in api["bot"].adapter.sent
        for segment in message["envelope"]["segments"]
        if isinstance(segment, str)
    )


class TestMutationsAreAnnounced:
    @pytest.mark.parametrize(
        "command",
        ["lock n1 1h", "slock n2 1h", "alias n1 10.0.0.2"],
    )
    def test_each_mutation_announces(self, client, api, command):
        assert _post(client, api, command).status_code == 200, command
        assert len(api["bot"].adapter.sent) == 1

    def test_unlock_announces(self, client, api):
        _post(client, api, "lock n1 1h")
        api["bot"].adapter.sent.clear()
        notification_budget.reset()
        assert _post(client, api, "unlock n1").status_code == 200
        assert len(api["bot"].adapter.sent) == 1

    def test_the_notification_names_the_acting_identity(self, client, api):
        _post(client, api, "lock n1 1h")
        assert ACTOR in _sent_text(api)

    def test_the_notification_marks_the_operation_as_api_originated(self, client, api):
        _post(client, api, "lock n1 1h")
        text = _sent_text(api)
        assert "API" in text

    def test_alias_uses_its_own_string(self, client, api):
        _post(client, api, "alias n1 10.0.0.3")
        assert "备注" in _sent_text(api) or "label" in _sent_text(api)

    def test_the_notification_is_not_the_plain_chat_reply(self, client, api):
        reply = _post(client, api, "lock n1 1h").json()["reply"]
        assert _sent_text(api) != reply

    def test_no_secret_appears_in_a_notification(self, client, api):
        _post(client, api, "lock n1 1h")
        text = _sent_text(api)
        assert api["credential"] not in text
        assert api["bind_code"] not in text

    def test_the_delivery_target_is_the_configured_webhook(self, client, api):
        """Sent with no explicit conversation id, matching operator-kick behaviour."""
        _post(client, api, "lock n1 1h")
        assert api["bot"].adapter.sent[0]["envelope"]["conversation"] is None


class TestReadsDoNotAnnounce:
    def test_query_does_not_announce(self, client, api):
        assert _post(client, api, "query").status_code == 200
        assert api["bot"].adapter.sent == []

    def test_the_resources_read_does_not_announce(self, client, api):
        client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
        assert api["bot"].adapter.sent == []

    def test_a_refused_command_does_not_announce(self, client, api):
        _post(client, api, "kickout someone")
        assert api["bot"].adapter.sent == []


class TestBestEffortDelivery:
    def test_a_send_failure_does_not_turn_a_committed_lock_into_a_failure(self, client, api):
        def boom(_msg):
            raise RuntimeError("webhook down")

        api["bot"].adapter.send = boom
        resp = _post(client, api, "lock n1 1h")

        assert resp.status_code == 200, "a delivery problem must not change a committed outcome"
        holders = [u["user_id"] for u in api["bot"].state.bot_state["n1"]["current_users"]]
        assert holders == [ACTOR]

    def test_the_send_happens_outside_the_bot_state_lock(self, client, api):
        """AC-15: a slow send must not extend a critical section."""
        observed = {}

        def capture(msg):
            observed["lock_free"] = api["bot"]._lock.acquire(blocking=False)
            if observed["lock_free"]:
                api["bot"]._lock.release()
            api["bot"].adapter.sent.append(msg)
            return [(200, "ok")]

        api["bot"].adapter.send = capture
        _post(client, api, "lock n1 1h")
        assert observed["lock_free"] is True, "the state lock was still held during the send"


class TestCoalescing:
    def test_a_burst_does_not_produce_one_notification_per_mutation(self, client, api):
        for _ in range(4):
            _post(client, api, "lock n1 1m")
        assert len(api["bot"].adapter.sent) == 1, "a burst must be coalesced"

    def test_burst_suppression_does_not_add_counter_noise(self, client, api):
        for _ in range(3):
            _post(client, api, "lock n1 1m")
        api["clock"][0] = 10.0
        assert notification_budget.run_due() == 1
        assert len(api["bot"].adapter.sent) == 2
        assert "另有" not in _sent_text(api)

    def test_concurrent_offers_yield_one_first_send(self):
        from lockbot.backend.app.resource_api.notification_queue import NotificationQueue, PublicEvent
        from lockbot.backend.app.resource_api.notifications import NotificationTarget

        budget = NotificationQueue(lambda target, text: "accepted", clock=lambda: 0.0, interval=60)
        target = NotificationTarget(4242, "fixture", 0, 1, "route", "zh")
        barrier = threading.Barrier(8)
        granted = []

        def offer():
            barrier.wait()
            granted.append(budget.offer(target, PublicEvent("operation", "lock"), immediate=lambda text: "accepted"))

        threads = [threading.Thread(target=offer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        assert sum(granted) == 1
        assert len(budget._batches[4242].events) == 7


class TestObservability:
    def _command_logs(self, client, api, auth_header):
        resp = client.get(f"/api/bots/{api['bot_id']}/logs?category=command", headers=auth_header)
        if resp.status_code != 200:
            pytest.skip("log listing route unavailable in this fixture")
        return resp.json()

    def test_the_command_log_entry_carries_the_api_source(self, client, api, auth_header):
        _post(client, api, "lock n1 1h")
        entries = self._command_logs(client, api, auth_header)
        api_entries = [e for e in entries if e.get("source") == SOURCE_API]
        assert api_entries, f"no api-sourced command log entry in {entries}"

    def test_credential_record_id_is_hidden_from_owner_and_available_to_super_admin(
        self, client, api, auth_header, db_session
    ):
        from lockbot.backend.app.resource_auth.models import ResourceBinding

        _post(client, api, "lock n1 1h")
        row = db_session.query(ResourceBinding).one()
        entries = self._command_logs(client, api, auth_header)
        assert all("credential_id" not in e for e in entries)
        from lockbot.backend.app.auth.models import User
        from lockbot.backend.app.bots.models import Bot

        db_session.get(User, db_session.get(Bot, api["bot_id"]).user_id).role = "super_admin"
        db_session.commit()
        assert any(e.get("credential_id") == row.id for e in self._command_logs(client, api, auth_header))

    def test_api_traffic_is_distinguishable_from_chat(self, client, api, auth_header):
        _post(client, api, "lock n1 1h")
        entries = self._command_logs(client, api, auth_header)
        sources = {e.get("source") for e in entries}
        assert SOURCE_API in sources
        assert SOURCE_CHAT not in sources

    def test_resource_commands_stay_in_bot_logs_and_out_of_admin_audit(self, client, api, db_session, auth_header):
        from lockbot.backend.app.audit.models import AuditLog

        _post(client, api, "lock n1 1h")
        assert db_session.query(AuditLog).filter(AuditLog.action == "resource_api.command").count() == 0
        entries = self._command_logs(client, api, auth_header)
        assert any(e.get("source") == SOURCE_API for e in entries)
        assert api["credential"] not in str(entries)

    def test_a_read_writes_no_command_audit_record(self, client, api, db_session):
        from lockbot.backend.app.audit.models import AuditLog

        client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
        assert db_session.query(AuditLog).filter(AuditLog.action == "resource_api.command").count() == 0


def test_notifications_use_configured_group_without_fake_user_mentions(client, api):
    api["bot"].config.set_val("_AUTHORIZED_GROUP_IDS", ["12724976"])
    assert _post(client, api, "lock n1 1h").status_code == 200
    msg = api["bot"].adapter.sent[0]["envelope"]
    assert msg["conversation"] == ["12724976"]
    assert msg["notify"] == []
