"""A blocked first notice must not hold resource authorization serialization locks."""

# ruff: noqa: F811
import threading

import pytest
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.authority import authority_guard

from backend.test_resource_api_notifications import api  # noqa: F401


def _blocked_first_command(client, api):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    responses, failures = [], []

    def slow_send(message):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release notification")
        api["bot"].adapter.sent.append(message)
        return [(200, "accepted")]

    api["bot"].adapter.send = slow_send

    def command():
        try:
            responses.append(
                client.post(
                    f"/api/bots/{api['bot_id']}/commands",
                    headers=api["headers"],
                    json={"command": "lock n1 1h"},
                )
            )
        except Exception as error:
            failures.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=command)
    worker.start()
    assert entered.wait(3), failures
    return worker, release, done, responses, failures


def test_slow_first_notice_allows_other_user_read_and_command(client, api):
    issued = client.post("/api/resource-auth/bindings", json={"label": "other-agent"}).json()
    service.handle_chat_binding(
        platform="infoflow", external_user_id="bob", bot_id=api["bot_id"], action="bind", argument=issued["bind_code"]
    )
    headers = {"Authorization": f"Bearer {issued['credential']}"}
    worker, release, done, responses, failures = _blocked_first_command(client, api)
    other_done, other_responses = threading.Event(), []

    def other_request():
        try:
            other_responses.append(client.get(f"/api/bots/{api['bot_id']}/resources", headers=headers))
            other_responses.append(
                client.post(f"/api/bots/{api['bot_id']}/commands", headers=headers, json={"command": "lock n2 1h"})
            )
        except Exception as error:
            failures.append(error)
        finally:
            other_done.set()

    other = threading.Thread(target=other_request)
    try:
        other.start()
        assert other_done.wait(3), "a webhook held the bot authorization guard across network IO"
        assert not failures
        assert [response.status_code for response in other_responses] == [200, 200]
        assert api["bot"].state.bot_state["n2"]["current_users"][0]["user_id"] == "bob"
        assert api["manager"].inflight(api["bot_id"]) == 1, "first delivery must retain its lifecycle lease"
        assert not done.is_set(), "the first notification should still be blocked"
    finally:
        release.set()
        worker.join(3)
        other.join(3)
    assert not worker.is_alive() and not other.is_alive()
    assert not failures
    assert responses[0].status_code == 200


def test_slow_first_notice_does_not_serialize_same_principal_on_another_bot(client, api):
    worker, release, done, responses, failures = _blocked_first_command(client, api)
    acquired = threading.Event()

    def other_bot():
        with authority_guard(api["bot_id"] + 1, platform="infoflow", external_user_id="alice"):
            acquired.set()

    checker = threading.Thread(target=other_bot)
    try:
        checker.start()
        assert acquired.wait(3), "a webhook held the cross-bot principal guard across network IO"
        assert api["manager"].inflight(api["bot_id"]) == 1
        assert not done.is_set()
    finally:
        release.set()
        worker.join(3)
        checker.join(3)
    assert not worker.is_alive() and not checker.is_alive()
    assert not failures
    assert responses[0].status_code == 200


def test_first_notice_uses_the_command_lease_at_single_slot_capacity(client, api):
    api["manager"]._max_inflight = 1
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": "lock n1 1h"}
    )
    assert response.status_code == 200
    assert len(api["bot"].adapter.sent) == 1
    assert api["manager"].inflight(api["bot_id"]) == 0


def test_lifecycle_drain_still_waits_for_blocked_first_notice(client, api, monkeypatch):
    manager = api["manager"]
    waiting, stopped = threading.Event(), threading.Event()
    await_drained = manager._await_drained_locked

    def observe_drain(bot_id, deadline):
        waiting.set()
        return await_drained(bot_id, deadline)

    monkeypatch.setattr(manager, "_await_drained_locked", observe_drain)
    worker, release, _done, responses, failures = _blocked_first_command(client, api)

    def stop():
        try:
            manager.stop_bot(api["bot_id"], drain_timeout=5)
            stopped.set()
        except Exception as error:
            failures.append(error)

    stopper = threading.Thread(target=stop)
    try:
        stopper.start()
        assert waiting.wait(3)
        assert manager.is_retiring(api["bot_id"])
        assert manager.inflight(api["bot_id"]) == 1
        assert not stopped.is_set(), "releasing authorization must not release lifecycle admission"
    finally:
        release.set()
        worker.join(3)
        stopper.join(3)
    assert not worker.is_alive() and not stopper.is_alive()
    assert not failures
    assert stopped.is_set()
    assert responses[0].status_code == 200
    assert manager.inflight(api["bot_id"]) == 0


@pytest.mark.parametrize("change", ["disabled", "deleted", "revision", "route", "stopped"])
def test_first_notice_revalidates_changes_before_handoff(client, api, db_session, monkeypatch, change):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.resource_api import commands

    original = commands.notify_group

    def changed_before_offer(*args, **kwargs):
        row = db_session.get(Bot, api["bot_id"])
        if change == "disabled":
            row.resource_api_notify_enabled = False
        elif change == "deleted":
            row.is_deleted = True
        elif change == "revision":
            row.identity_revision += 1
        elif change == "route":
            row.group_id = "different-group"
        else:
            row.status = "stopped"
        db_session.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(commands, "notify_group", changed_before_offer)
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": "lock n1 1h"}
    )
    assert response.status_code == 200
    assert api["bot"].state.bot_state["n1"]["current_users"][0]["user_id"] == "alice"
    assert api["bot"].adapter.sent == [], "a changed target must not receive the already-committed command notice"
