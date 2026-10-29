"""Only verified, exactly addressed group events may schedule bounded association guidance."""

# ruff: noqa: F811
import json
from types import SimpleNamespace

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu import service, unbound
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute, FeishuReceivedMessage
from lockbot.core.platforms.feishu import FeishuClient
from lockbot.core.platforms.feishu_events import InvalidFeishuEvent
from sqlalchemy.orm import sessionmaker

from backend.test_feishu_ingress import message, setup  # noqa: F401

_ORIGINAL_SEND = FeishuClient.send


class Deferred:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)


@pytest.fixture
def hints(setup, db_session, monkeypatch):
    clock, sent, sessions, request_sessions = [100.0], [], [], []
    executor = Deferred()
    worker = unbound.UnboundHints(executor=executor, clock=lambda: clock[0])
    monkeypatch.setattr(unbound, "unbound_hints", worker)
    real_queue = unbound.queue_hint

    def queue(db, *args, **kwargs):
        result = real_queue(db, *args, **kwargs)
        assert not db.in_transaction()
        request_sessions.append(db)
        return result

    monkeypatch.setattr(unbound, "queue_hint", queue)
    original_factory = sessionmaker(bind=db_session.get_bind())

    def factory(**kwargs):
        def make():
            session = original_factory()
            sessions.append(session)
            return session

        return make

    monkeypatch.setattr(unbound, "sessionmaker", factory)

    def send(self, payload):
        assert all(not session.in_transaction() for session in request_sessions + sessions)
        sent.append(payload)
        return [(200, "platform_accepted")]

    monkeypatch.setattr(FeishuClient, "token", lambda self: "fixture-token")
    monkeypatch.setattr(FeishuClient, "send", send)
    return SimpleNamespace(
        request_sessions=request_sessions,
        worker_sessions=sessions,
        clock=clock,
        sent=sent,
        executor=executor,
        worker=worker,
        app_id=setup[0],
        calls=setup[2],
    )


def post(client, hints, value):
    return client.post(f"/api/feishu/events/{hints.app_id}", json=value)


def test_verified_http_hint_is_fixed_group_only_no_binding_or_execution(client, hints, db_session):
    event = message(group=99, command="bind FICTIONAL_PRIVATE_CODE")
    assert post(client, hints, event).json() == {"accepted": False}
    assert len(hints.executor.jobs) == 1 and not hints.calls
    assert all(not session.in_transaction() for session in hints.request_sessions)
    assert "FICTIONAL" not in repr(hints.executor.jobs)
    hints.executor.run()
    assert len(hints.sent) == 1
    assert hints.sent[0]["receive_id"] == "oc_group99" and hints.sent[0]["msg_type"] == "text"
    text = json.loads(hints.sent[0]["content"])["text"]
    assert "oc_group99" in text and "尚未关联" in text and "FICTIONAL" not in text and "node_a" not in text
    assert db_session.query(FeishuGroupRoute).count() == 2 and db_session.query(Bot).count() == 2
    assert db_session.query(FeishuReceivedMessage).count() == 0


@pytest.mark.parametrize(
    "case", ["dm", "no_mention", "other_bot", "wrong_app", "wrong_tenant", "sender_tenant", "stale", "wrong_token"]
)
def test_http_non_authorized_events_never_schedule_guidance(client, hints, case):
    event = message(group=99)
    body = event["event"]["message"]
    if case == "dm":
        body["chat_type"] = "p2p"
    elif case == "no_mention":
        body["content"] = json.dumps({"text": "query"})
    elif case == "other_bot":
        body["mentions"][0]["id"]["open_id"] = "ou_other"
    elif case == "wrong_app":
        event["header"]["app_id"] = "cli_other"
    elif case == "wrong_tenant":
        event["header"]["tenant_key"] = "other"
    elif case == "sender_tenant":
        event["event"]["sender"]["tenant_key"] = "other"
    elif case == "stale":
        body["create_time"] = "1"
    else:
        event["header"]["token"] = "invalid"
    response = post(client, hints, event)
    assert response.status_code in (200, 401) and not hints.executor.jobs and not hints.sent


@pytest.mark.parametrize("case", ["valid", "dm", "no_mention", "wrong_app", "sender_tenant", "stale"])
def test_sdk_unknown_tenant_can_only_hint_without_pinning_it(setup, hints, db_session, case):
    app = db_session.get(FeishuApplication, hints.app_id)
    app.transport, app.tenant_key = "websocket", ""
    db_session.commit()
    event = message(group=99)
    if case == "dm":
        event["event"]["message"]["chat_type"] = "p2p"
    elif case == "no_mention":
        event["event"]["message"]["mentions"] = []
    elif case == "wrong_app":
        event["header"]["app_id"] = "cli_other"
    elif case == "sender_tenant":
        event["event"]["sender"]["tenant_key"] = "foreign"
    elif case == "stale":
        event["event"]["message"]["create_time"] = "1"
    if case in {"wrong_app", "sender_tenant", "stale"}:
        with pytest.raises(InvalidFeishuEvent):
            service.accept_sdk_event(db_session, app, event)
    else:
        assert service.accept_sdk_event(db_session, app, event) == {"accepted": False}
    if case == "valid":
        assert not db_session.in_transaction()
        hints.executor.run()
        assert len(hints.sent) == 1
    else:
        assert not hints.executor.jobs
    db_session.expire_all()
    assert db_session.get(FeishuApplication, hints.app_id).tenant_key == ""


@pytest.mark.parametrize("change", ["route", "secret", "bot_identity", "tenant", "transport", "stale_queue"])
def test_worker_rechecks_identity_route_and_age_before_network(client, hints, db_session, change):
    assert post(client, hints, message(group=99)).status_code == 200
    app = db_session.get(FeishuApplication, hints.app_id)
    if change == "route":
        # Move the other route into this group after enqueue; do not send a stale hint.
        route = db_session.query(FeishuGroupRoute).first()
        route.chat_id = "oc_group99"
    elif change == "secret":
        app.app_secret = "rotated_ciphertext"
    elif change == "bot_identity":
        app.bot_open_id = "ou_changed"
    elif change == "tenant":
        app.tenant_key = "changed"
    elif change == "transport":
        app.transport = "websocket"
    else:
        hints.clock[0] += 31
    db_session.commit()
    hints.executor.run()
    assert not hints.sent and not hints.calls


def test_duplicate_and_group_app_global_budgets_are_bounded(client, hints, db_session):
    event = message(group=99)
    post(client, hints, event)
    hints.clock[0] += 61
    post(client, hints, event)  # Message-ID retry still deduplicated after group cooldown.
    assert len(hints.executor.jobs) == 1
    post(client, hints, message(group=99, message_id="om_second"))
    assert len(hints.executor.jobs) == 2
    post(client, hints, message(group=100, message_id="om_third"))  # App/global cooldown.
    assert len(hints.executor.jobs) == 2
    for index in range(25):
        hints.clock[0] += 11
        post(client, hints, message(group=200 + index, message_id=f"om_extra{index}"))
    # Pending queue reaches capacity but cannot grow. All caches have fixed hard limits.
    assert len(hints.executor.jobs) == 16
    assert len(hints.worker.groups) <= 512 and len(hints.worker.apps) <= 128 and len(hints.worker.messages) <= 2048
    hints.clock[0] += 31
    hints.executor.run()
    assert not hints.sent


def test_uncertain_send_is_not_retried_and_capacity_is_released(client, hints, monkeypatch, caplog):
    attempts = []

    def fail(*args):
        attempts.append(1)
        raise TimeoutError("never log this private transport body")

    monkeypatch.setattr(FeishuClient, "send", fail)
    event = message(group=99)
    post(client, hints, event)
    hints.executor.run()
    hints.clock[0] += 61
    post(client, hints, event)
    hints.executor.run()
    assert len(attempts) == 1
    assert "never log this private transport body" not in caplog.text
    assert hints.worker.capacity.acquire(blocking=False)
    hints.worker.capacity.release()


def test_hint_uses_the_fixed_feishu_proxy_after_all_owned_transactions_close(client, hints, monkeypatch):
    from lockbot.core.platforms import feishu_proxy

    calls = []
    monkeypatch.setattr(feishu_proxy, "_mode", "custom")
    monkeypatch.setattr(feishu_proxy, "_configured", "http://proxy.example:8188")
    monkeypatch.setattr(FeishuClient, "send", _ORIGINAL_SEND)
    monkeypatch.setattr(FeishuClient, "token", lambda self: "fixture-token")

    def network(url, **kwargs):
        assert all(not session.in_transaction() for session in hints.request_sessions + hints.worker_sessions)
        assert kwargs["proxies"] == {"http": "http://proxy.example:8188", "https": "http://proxy.example:8188"}
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (3, 8)
        calls.append(kwargs["json"])
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0})

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.post", network)
    post(client, hints, message(group=99))
    hints.executor.run()
    assert len(calls) == 1 and calls[0]["receive_id"] == "oc_group99"


def test_rate_gate_caches_have_hard_limits_without_network_or_database_writes():
    class Inline:
        def submit(self, fn, *args):
            fn(*args)

    clock = [0.0]
    worker = unbound.UnboundHints(executor=Inline(), clock=lambda: clock[0])
    worker._deliver = lambda *_args: worker.capacity.release()
    db = SimpleNamespace(get_bind=lambda: None, rollback=lambda: None)
    for index in range(2100):
        clock[0] += 11
        app = SimpleNamespace(
            id=index,
            app_id="cli_fixture",
            tenant_key="tenant",
            bot_open_id="ou_bot",
            app_secret="cipher",
            verification_token="cipher",
            encrypt_key="",
            transport="http",
        )
        assert worker.enqueue(db, app, {"group_id": f"oc_{index}", "message_id": f"om_{index}"}, tenant="tenant")
    assert len(worker.messages) == 2048 and len(worker.groups) == 512 and len(worker.apps) == 128
    # A separate app cannot evade the global send interval.
    app.id += 1
    assert not worker.enqueue(db, app, {"group_id": "oc_fresh", "message_id": "om_fresh"}, tenant="tenant")


def test_overlong_group_identity_is_not_queued(client, hints):
    event = message(group="x" * 130)
    assert post(client, hints, event).json() == {"accepted": False}
    assert not hints.executor.jobs


def test_route_becoming_bound_during_token_refresh_cancels_the_hint(client, hints, db_session, monkeypatch):
    def warm(self):
        assert all(not session.in_transaction() for session in hints.request_sessions + hints.worker_sessions)
        route = db_session.query(FeishuGroupRoute).first()
        route.chat_id = "oc_group99"
        db_session.commit()
        return "fixture-token"

    monkeypatch.setattr(FeishuClient, "token", warm)
    post(client, hints, message(group=99))
    hints.executor.run()
    assert not hints.sent
