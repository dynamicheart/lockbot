"""A timed-out query sends only a busy notice without changing tracked query state."""

# ruff: noqa: F811 -- imported fixtures are injected by parameter name.
import copy
import threading

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu import query_panel, router
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuQueryPanel, FeishuReceivedMessage
from lockbot.backend.app.feishu.service import accept_sdk_event
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient

from backend.test_feishu_ingress import message
from backend.test_feishu_query_panels import feishu_setup, panel_env  # noqa: F401


def latest(factory, bot_id):
    with factory() as db:
        row = db.get(FeishuQueryPanel, bot_id)
        return row.message_id, row.generation, row.edit_count, row.status


def timed_out_lock(monkeypatch, factory, bot_id):
    waits = []

    class BusyLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            return False

        def release(self):
            pytest.fail("Timed-out caller must not release the active owner's lock")

    mutex = query_panel._Mutex()
    mutex.lock = BusyLock()
    monkeypatch.setitem(query_panel._locks, (factory.kw["bind"], bot_id), mutex)
    return mutex, waits  # Retain the weak-map value while the test runs.


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("command", ["", "query"])
def test_busy_query_notices_once_without_dispatch_snapshot_or_pointer_change(
    panel_env, client, db_session, monkeypatch, transport, command
):
    factory, bots, events, panel, reply, app_id, calls = panel_env
    panel().send(reply())
    bid = bots[0].config.get_val("BOT_ID")
    tracked, resources = latest(factory, bid), copy.deepcopy(bots[0].state.bot_state)
    events.clear()
    mutex, waits = timed_out_lock(monkeypatch, factory, bid)
    app = db_session.get(FeishuApplication, app_id)
    app.transport = transport
    db_session.commit()
    dispatched, execute = [], router.execute_command

    def capture(request, bot):
        dispatched.append(request["command"])
        return execute(request, bot)

    monkeypatch.setattr(router, "execute_command", capture)

    def submit(payload):
        if transport == "http":
            return client.post(f"/api/feishu/events/{app_id}", json=payload).json()
        return accept_sdk_event(db_session, app, payload)

    payload = message(message_id="om_busy_query", command=command)
    assert submit(payload) == {"accepted": True}
    assert submit(payload) == {"accepted": True, "duplicate": True}
    fn, args = calls.pop()
    fn(*args)
    assert waits == [15] and mutex.lock and not dispatched
    assert [row[0] for row in events] == ["normal"]
    assert "查询繁忙" in events[0][1]["content"] and "Full query" not in events[0][1]["content"]
    assert events[0][1]["receive_id"] == "oc_group0"
    assert latest(factory, bid) == tracked and bots[0].state.bot_state == resources
    db_session.expire_all()
    assert db_session.get(FeishuReceivedMessage, (app_id, "om_busy_query")).state == "completed"

    mutation = message(message_id="om_busy_lock", command="lock node_a 2m")
    assert submit(mutation) == {"accepted": True}
    assert submit(mutation) == {"accepted": True, "duplicate": True}
    fn, args = calls.pop()
    fn(*args)
    assert waits == [15] and dispatched == ["lock node_a 2m"]
    holders = bots[0].state.bot_state["node_a"]["current_users"]
    assert len(holders) == 1 and holders[0]["user_id"] == "ou_alice" and holders[0]["duration"] == 120
    assert latest(factory, bid) == tracked and [row[0] for row in events] == ["normal", "normal"]


def test_busy_delivery_uncertainty_does_not_retry_or_dispatch(panel_env, client, db_session, monkeypatch):
    factory, bots, events, panel, reply, app_id, calls = panel_env
    panel().send(reply())
    bid = bots[0].config.get_val("BOT_ID")
    tracked = latest(factory, bid)
    mutex, waits = timed_out_lock(monkeypatch, factory, bid)
    attempts = []

    def fail(*args):
        attempts.append(True)
        raise FeishuAPIError("synthetic response loss")

    monkeypatch.setattr(FeishuClient, "send", fail)
    monkeypatch.setattr(router, "execute_command", lambda *args: pytest.fail("Busy query was dispatched"))
    payload = message(message_id="om_busy_unknown", command="query")
    assert client.post(f"/api/feishu/events/{app_id}", json=payload).json() == {"accepted": True}
    fn, args = calls.pop()
    fn(*args)
    assert client.post(f"/api/feishu/events/{app_id}", json=payload).json() == {"accepted": True, "duplicate": True}
    assert waits == [15] and mutex.lock and len(attempts) == 1 and latest(factory, bid) == tracked
    assert not calls
    db_session.expire_all()
    assert db_session.get(FeishuReceivedMessage, (app_id, "om_busy_unknown")).state == "delivery_uncertain"


@pytest.mark.parametrize("stage", ["dispatch", "send"])
def test_busy_catch_is_limited_to_context_entry(panel_env, client, db_session, monkeypatch, stage):
    _, _, events, _, _, app_id, calls = panel_env

    def fail(*args):
        raise query_panel.QueryPanelBusy("synthetic later failure")

    if stage == "dispatch":
        monkeypatch.setattr(router, "execute_command", fail)
    else:
        monkeypatch.setattr(query_panel.QueryPanel, "send", fail)
    assert client.post(f"/api/feishu/events/{app_id}", json=message(command="query")).json() == {"accepted": True}
    fn, args = calls.pop()
    fn(*args)
    assert not events  # A later failure must never be relabeled with a busy reply.
    db_session.expire_all()
    expected = "uncertain" if stage == "dispatch" else "delivery_uncertain"
    assert db_session.get(FeishuReceivedMessage, (app_id, "om_first")).state == expected


def test_inflight_panel_remains_latest_when_later_query_only_receives_busy(panel_env, client, monkeypatch):
    factory, bots, events, panel, reply, app_id, calls = panel_env
    bid = bots[0].config.get_val("BOT_ID")
    entered, complete = threading.Event(), threading.Event()
    failures = []

    class ImmediateTimeoutLock:
        def __init__(self):
            self.inner = threading.Lock()

        def acquire(self, *, timeout):
            assert timeout == 15
            return self.inner.acquire(blocking=False)

        def release(self):
            self.inner.release()

    mutex = query_panel._Mutex()
    mutex.lock = ImmediateTimeoutLock()
    monkeypatch.setitem(query_panel._locks, (factory.kw["bind"], bid), mutex)

    def slow_create(*args):
        events.append(("create",))
        entered.set()
        assert complete.wait(3)
        return "om_original_query"

    monkeypatch.setattr(FeishuClient, "create_query_panel", slow_create)
    with factory() as db:
        row = db.get(Bot, bid)
        identity = row.uuid, row.identity_revision

    def first():
        try:
            with query_panel.query_delivery(
                factory, bid, app_id, {"command": "query", "group_id": "oc_group0"}, *identity, bots[0]
            ) as sender:
                sender.send(reply(text="original query snapshot\n\n"))
        except Exception as error:
            failures.append(type(error).__name__)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(2)
    monkeypatch.setattr(router, "execute_command", lambda *args: pytest.fail("No newer snapshot may be dispatched"))
    try:
        assert client.post(f"/api/feishu/events/{app_id}", json=message(command="query")).json() == {"accepted": True}
        fn, args = calls.pop()
        fn(*args)
        assert [event[0] for event in events] == ["create", "normal"]
        assert "查询繁忙" in events[-1][1]["content"]
    finally:
        complete.set()
        worker.join(3)
    assert not failures and not worker.is_alive()
    assert latest(factory, bid)[0] == "om_original_query"  # Still the only full-query snapshot.


def test_busy_notice_uses_bot_language(panel_env, client, monkeypatch):
    factory, bots, events, _, _, app_id, calls = panel_env
    bots[0].config.set_val("LANGUAGE", "en")
    mutex, _ = timed_out_lock(monkeypatch, factory, bots[0].config.get_val("BOT_ID"))
    assert client.post(f"/api/feishu/events/{app_id}", json=message(command="query")).json() == {"accepted": True}
    fn, args = calls.pop()
    fn(*args)
    assert mutex.lock and len(events) == 1 and "Query is busy" in events[0][1]["content"]
