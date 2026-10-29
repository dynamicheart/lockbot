# ruff: noqa: F811
"""Deterministic deadlines, bounded work, privacy and lifecycle checks."""

import threading

import pytest
from lockbot.backend.app.resource_api.notification_queue import NotificationQueue, PublicEvent
from lockbot.backend.app.resource_api.notifications import NotificationTarget, notification_budget

from backend.test_resource_api_notifications import api  # noqa: F401


def target(bot_id=1, generation=1):
    return NotificationTarget(bot_id, "fixture", 0, generation, "route", "zh")


@pytest.fixture
def queue():
    clock, sent = [0.0], []

    def deliver(where, text):
        sent.append((where.bot_id, text))
        return "accepted"

    q = NotificationQueue(deliver, clock=lambda: clock[0])
    q.first = lambda text: deliver(target(), text)
    q.now, q.sent = clock, sent
    return q


def event(text, kind="lock"):
    return PublicEvent(text, kind)


def test_alias_alias_lock_flushes_without_fourth_request(queue):
    queue.offer(target(), event("alias node-a", "alias"), immediate=queue.first)
    queue.offer(target(), event("alias node-b", "alias"), immediate=queue.first)
    queue.offer(target(), event("lock node-a,node-b 2h"), immediate=queue.first)
    queue.now[0] = 9.99
    assert queue.run_due() == 0
    queue.now[0] = 10
    assert queue.run_due() == 1
    assert queue.sent == [(1, "alias node-a"), (1, "alias node-b\n\nlock node-a,node-b 2h")]


def test_new_arrivals_do_not_postpone_deadline_and_transitions_stay_ordered(queue):
    queue.offer(target(), event("lock a"), immediate=queue.first)
    for when, text in [(1, "user A book b"), (5, "user B lock c"), (9, "user A unlock b")]:
        queue.now[0] = when
        queue.offer(target(), event(text), immediate=queue.first)
    queue.now[0] = 10
    queue.run_due()
    assert queue.sent[-1][1] == "user A book b\n\nuser B lock c\n\nuser A unlock b"
    queue.offer(target(), event("lock d"), immediate=queue.first)
    queue.now[0] = 20
    queue.run_due()
    assert queue.sent[-1][1] == "lock d"


def test_bots_have_separate_deadlines_and_batches(queue):
    queue.offer(target(1), event("a"), immediate=queue.first)
    queue.offer(target(2), event("b"), immediate=lambda text: "accepted")
    queue.offer(target(1), event("actor A unlock a"))
    queue.offer(target(2), event("actor B book b"))
    queue.now[0] = 10
    assert queue.run_due() == 2
    assert queue.sent[-2:] == [(1, "actor A unlock a"), (2, "actor B book b")]


def test_overflow_keeps_resource_change_instead_of_alias_and_reports_loss(queue, caplog):
    queue.max_events = 2
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("alias a", "alias"))
    queue.offer(target(), event("lock b"))
    queue.offer(target(), event("unlock c", "unlock"))
    queue.offer(target(), event("book d", "book"))
    queue.now[0] = 10
    queue.run_due()
    text = queue.sent[-1][1]
    assert "lock b" in text and "unlock c" in text and "alias a" not in text
    assert "2 条" in text and queue.counts["overflow_dropped"] == 2
    assert "overflow" in caplog.text


def test_oversized_events_and_bot_capacity_are_bounded(queue):
    queue.max_bots = 1
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("pending"))
    assert queue.offer(target(2), event("other")) is False
    queue.offer(target(), event("s" * 100000))
    assert queue.counts["capacity_dropped"] == 1
    assert queue.counts["overflow_dropped"] == 1
    assert queue._batches[1].size < queue.max_bytes


def test_long_batch_is_split_by_message_limit_without_reordering(queue):
    queue.max_chars = 250
    queue.offer(target(), event("first"), immediate=queue.first)
    for index in range(4):
        queue.offer(target(), event(str(index) * 200))
    for now in (10, 20, 30, 40):
        queue.now[0] = now
        queue.run_due()
    assert [text for _, text in queue.sent[1:]] == [str(index) * 200 for index in range(4)]
    assert all(len(text) <= 250 for _, text in queue.sent)


def test_unknown_delivery_is_never_retried(queue):
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("pending"))
    calls = []

    def uncertain(where, text):
        calls.append(text)
        raise TimeoutError("must not leak this text")

    queue.deliver = uncertain
    queue.now[0] = 10
    assert queue.run_due() == 1
    queue.now[0] = 100
    assert queue.run_due() == 0
    assert calls == ["pending"] and queue.counts["unknown"] == 1


@pytest.mark.parametrize("action", ["cancel", "reset", "stop"])
def test_cleanup_discards_pending_tasks(queue, action):
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("pending"))
    getattr(queue, action)(1) if action == "cancel" else getattr(queue, action)()
    queue.now[0] = 100
    assert queue.run_due() == 0
    assert queue.sent == [(1, "first")]


def test_old_generation_retirement_cannot_cancel_new_generation(queue):
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("old pending"))
    queue.offer(target(generation=2), event("new"), immediate=queue.first)
    queue.offer(target(generation=2), event("new pending"))
    queue.cancel(1, 1)
    queue.now[0] = 10
    queue.run_due()
    assert "new pending" in queue.sent[-1][1]
    assert all("old pending" not in text for _, text in queue.sent)


def test_sends_are_bounded_and_new_calls_do_not_wait_for_slow_send(queue):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def send(index):
        def blocked(text):
            entered[index].set()
            assert release.wait(2)
            return "accepted"

        queue.offer(target(index + 1), event("slow"), immediate=blocked)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    assert all(signal.wait(2) for signal in entered)
    assert queue.offer(target(3), event("queued immediately"), immediate=lambda text: "accepted") is False
    assert queue.run_due() == 0
    release.set()
    for thread in threads:
        thread.join(2)
    assert queue.run_due() == 1
    assert queue.sent[-1] == (3, "queued immediately")


def test_real_coordinator_sends_without_a_later_offer():
    delivered = threading.Event()
    queue = NotificationQueue(lambda where, text: delivered.set() or "accepted")
    queue.offer(target(), event("already due"))
    queue.start()
    try:
        assert delivered.wait(2)
    finally:
        queue.stop()


def test_api_trailing_batch_keeps_operational_facts_but_not_private_text(client, api):
    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("alias n1 secret-alias-a", "alias n2 secret-alias-b", "lock n1,n2 2h"):
        response = client.post(
            path,
            headers=api["headers"],
            json={
                "command": command,
                "agent_label": "private-agent",
                "purpose": "private-project",
                "task_category": "functional_testing",
            },
        )
        assert response.status_code == 200
    assert len(api["bot"].adapter.sent) == 1
    api["clock"][0] = 10
    assert notification_budget.run_due() == 1
    text = "".join(m["envelope"]["segments"][0] for m in api["bot"].adapter.sent)
    assert "alias n2" in text and "lock n1,n2 2h" in text and "功能测试" in text
    assert all(
        secret not in text for secret in ["secret-alias-a", "secret-alias-b", "private-agent", "private-project"]
    )
    for name in ("n1", "n2"):
        assert len(api["bot"].state.bot_state[name]["current_users"]) == 1


@pytest.mark.parametrize("change", ["disabled", "deleted", "revision", "route", "stopped", "generation"])
def test_flush_revalidates_current_database_and_generation(client, api, db_session, change):
    from lockbot.backend.app.bots.models import Bot

    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("lock n1 1m", "unlock n1"):
        assert client.post(path, headers=api["headers"], json={"command": command}).status_code == 200
    row = db_session.get(Bot, api["bot_id"])
    if change == "disabled":
        row.resource_api_notify_enabled = False
    elif change == "deleted":
        row.is_deleted = True
    elif change == "revision":
        row.identity_revision += 1
    elif change == "route":
        row.group_id = "999999"
    elif change == "stopped":
        row.status = "stopped"
    else:
        api["manager"].stop_bot(api["bot_id"])
    db_session.commit()
    api["clock"][0] = 10
    notification_budget.run_due()
    assert len(api["bot"].adapter.sent) == 1


def test_notification_disable_then_enable_does_not_revive_old_batch(client, api, auth_header):
    path = f"/api/bots/{api['bot_id']}"
    for command in ("lock n1 1m", "unlock n1"):
        assert client.post(path + "/commands", headers=api["headers"], json={"command": command}).status_code == 200
    assert client.put(path, headers=auth_header, json={"resource_api_notify_enabled": False}).status_code == 200
    assert client.put(path, headers=auth_header, json={"resource_api_notify_enabled": True}).status_code == 200
    api["clock"][0] = 10
    notification_budget.run_due()
    assert len(api["bot"].adapter.sent) == 1


def test_chinese_batches_include_separators_and_overflow_in_encoding_budget(queue):
    import json

    from lockbot.core.config import Config
    from lockbot.core.platforms.infoflow import InfoflowAdapter

    queue.max_events = 30
    queue.offer(target(), event("first"), immediate=queue.first)
    texts = [f"用户甲{i}：lock 节点-{i} 2h\n用途：" + "功能测试" * 28 for i in range(30)]
    for text in texts:
        queue.offer(target(), event(text))
    queue.offer(target(), event("extra"))
    adapter = InfoflowAdapter(Config({"WEBHOOK_URL": "https://example.invalid/notification"}))
    for now in range(10, 200, 10):
        queue.now[0] = now
        queue.run_due()
    sent = [text for _, text in queue.sent[1:]]
    assert len(sent) > 1
    assert all(len(text) <= 1800 and len(text.encode()) <= 4000 for text in sent)
    assert any("1 条" in text for text in sent)
    combined = "\n\n".join(sent)
    assert all(text in combined for text in texts)
    assert all(len(json.dumps(adapter.build_reply(text, []), ensure_ascii=True).encode()) <= 16384 for text in sent)


def test_temporary_admission_pressure_defers_only_before_platform_handoff(client, api):
    from contextlib import ExitStack

    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("lock n1 1m", "unlock n1"):
        assert client.post(path, headers=api["headers"], json={"command": command}).status_code == 200
    api["clock"][0] = 10
    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(api["manager"].admit(api["bot_id"]))
        assert notification_budget.run_due() == 1
        assert notification_budget.counts["deferred"] == 1
        assert len(api["bot"].adapter.sent) == 1
    api["clock"][0] = 11
    assert notification_budget.run_due() == 1
    assert len(api["bot"].adapter.sent) == 2


def test_admission_deferral_has_a_finite_expiry(queue):
    queue.offer(target(), event("first"), immediate=queue.first)
    queue.offer(target(), event("pending"))
    queue.deliver = lambda where, text: "deferred"
    queue.now[0] = 10
    assert queue.run_due() == 1
    queue.now[0] = 131
    assert queue.run_due() == 0
    assert queue.counts["admission_expired"] == 1


def test_background_network_does_not_hold_authority_or_resource_lock(client, api):
    from lockbot.backend.app.resource_auth.authority import authority_guard

    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("lock n1 1m", "unlock n1"):
        assert client.post(path, headers=api["headers"], json={"command": command}).status_code == 200
    entered, released, authority_free = threading.Event(), threading.Event(), threading.Event()

    def slow_send(message):
        assert api["bot"]._lock.acquire(blocking=False)
        api["bot"]._lock.release()
        entered.set()
        assert released.wait(2)
        return [(200, "accepted")]

    api["bot"].adapter.send = slow_send
    api["clock"][0] = 10
    worker = threading.Thread(target=notification_budget.run_due)

    def acquire_authority():
        with authority_guard(api["bot_id"]):
            authority_free.set()

    checker = threading.Thread(target=acquire_authority)
    worker.start()
    try:
        assert entered.wait(2)
        checker.start()
        assert authority_free.wait(2)
    finally:
        released.set()
        worker.join(2)
        if checker.ident:
            checker.join(2)


def test_cancel_after_dispatch_before_database_check_cannot_revive_batch(client, api):
    from dataclasses import replace

    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("lock n1 1m", "unlock n1"):
        assert client.post(path, headers=api["headers"], json={"command": command}).status_code == 200
    batch = notification_budget._batches[api["bot_id"]]
    original_factory = batch.target.factory

    def cancel_before_read():
        notification_budget.cancel(api["bot_id"])
        return original_factory()

    batch.target = replace(batch.target, factory=cancel_before_read)
    api["clock"][0] = 10
    notification_budget.run_due()
    assert len(api["bot"].adapter.sent) == 1
    assert notification_budget.counts["cancelled"] >= 1
