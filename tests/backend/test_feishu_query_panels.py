"""Panels never own commands; all tests use fake Feishu API calls."""

# ruff: noqa: F811 -- the imported shared pytest fixture is intentionally injected by name.

import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuQueryPanel
from lockbot.backend.app.feishu.query_panel import MAX_EDITS, QueryPanel, invalidate_query_panel, query_delivery
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient, FeishuPanelAPIError
from sqlalchemy.orm import sessionmaker

from tests.backend.test_feishu_ingress import message
from tests.backend.test_feishu_ingress import setup as feishu_setup  # noqa: F401


@pytest.fixture
def panel_env(feishu_setup, db_session, monkeypatch):
    app_id, bots, calls = feishu_setup
    factory = sessionmaker(bind=db_session.get_bind())
    for bot in bots:
        row = db_session.get(Bot, bot.config.get_val("BOT_ID"))
        row.status = "running"
        row.config_overrides = json.dumps({"FEISHU_QUERY_PANEL": True})
        bot.config.set_val("FEISHU_QUERY_PANEL", True)
    db_session.commit()
    events = []
    monkeypatch.setattr(
        FeishuClient, "create_query_panel", lambda self, payload: events.append(("create", payload)) or "om_panel"
    )
    monkeypatch.setattr(
        FeishuClient, "update_query_panel", lambda self, mid, payload: events.append(("update", mid, payload))
    )
    monkeypatch.setattr(FeishuClient, "send", lambda self, payload: events.append(("normal", payload)) or [])

    def panel(index=0):
        bot = bots[index]
        row = db_session.get(Bot, bot.config.get_val("BOT_ID"))
        return QueryPanel(factory, row.id, app_id, row.uuid, row.identity_revision, row.group_id, bot.adapter)

    def reply(index=0, text="Full query\n\nnode_a empty\n\n"):
        return bots[index].adapter.build_reply(text, ["ou_alice"], f"oc_group{index}")

    return factory, bots, events, panel, reply, app_id, calls


def test_create_update_restart_and_scope_isolation(panel_env):
    factory, bots, events, panel, reply, _, _ = panel_env
    assert panel().send(reply())
    assert panel().send(reply())  # Fresh handler simulates restart: verified DB reference reused.
    assert panel(1).send(reply(1))
    assert [e[0] for e in events] == ["create", "update", "create"]
    assert events[0][1]["content"] == events[1][2]["content"]
    with factory() as db:
        assert db.query(FeishuQueryPanel).count() == 2
        row = db.get(FeishuQueryPanel, bots[0].config.get_val("BOT_ID"))
        assert row.edit_count == 1
        assert "Full query" not in row.scope


@pytest.mark.parametrize("change", ["edit_limit", "old", "membership"])
def test_rollover_and_membership_invalidation(panel_env, change):
    factory, bots, events, panel, reply, _, _ = panel_env
    panel().send(reply())
    with factory() as db:
        row = db.get(FeishuQueryPanel, bots[0].config.get_val("BOT_ID"))
        if change == "edit_limit":
            row.edit_count = MAX_EDITS
        elif change == "old":
            row.created_at = datetime.utcnow() - timedelta(hours=1)
        else:
            invalidate_query_panel(db, row.bot_id)
        db.commit()
    panel().send(reply())
    assert [e[0] for e in events] == ["create", "create"]


@pytest.mark.parametrize("code", [230027, 230011, 230110, 230075, 230072, 230020, 230002])
def test_update_failure_falls_back_once_and_cools_down(panel_env, monkeypatch, code):
    _, _, events, panel, reply, _, _ = panel_env
    panel().send(reply())
    attempts = []

    def fail(*args):
        attempts.append(1)
        raise FeishuPanelAPIError(code)

    monkeypatch.setattr(FeishuClient, "update_query_panel", fail)
    assert panel().send(reply()) is False
    assert panel().send(reply()) is False
    assert attempts == [1]
    assert [e[0] for e in events] == ["create"]


def test_create_uncertainty_never_duplicates_or_adopts_inbound_id(panel_env, monkeypatch):
    factory, bots, events, panel, reply, _, _ = panel_env

    def fail(*args):
        raise FeishuPanelAPIError(uncertain=True)

    monkeypatch.setattr(FeishuClient, "create_query_panel", fail)
    with pytest.raises(FeishuAPIError):
        panel().send(reply())
    assert panel().send(reply()) is False
    assert not events
    with factory() as db:
        row = db.get(FeishuQueryPanel, bots[0].config.get_val("BOT_ID"))
        assert row.message_id is None
        assert row.reason == "delivery_uncertain"


def test_accepted_without_proven_own_message_is_not_resent(panel_env, monkeypatch):
    _, _, events, panel, reply, _, _ = panel_env
    monkeypatch.setattr(FeishuClient, "create_query_panel", lambda *args: None)
    assert panel().send(reply()) is True
    assert panel().send(reply()) is False
    assert not events


@pytest.mark.parametrize("change", ["revision", "uuid", "group", "owner", "app"])
def test_identity_changes_never_reuse_old_reference(panel_env, change):
    factory, bots, events, panel, reply, app_id, _ = panel_env
    old = panel()
    old.send(reply())
    with factory() as db:
        bot = db.get(Bot, bots[0].config.get_val("BOT_ID"))
        if change == "revision":
            bot.identity_revision += 1
        elif change == "uuid":
            bot.uuid = "new-uuid"
        elif change == "group":
            bot.group_id = "oc_other"
        elif change == "owner":
            bot.user_id += 10
        else:
            db.get(FeishuApplication, app_id).app_id = "cli_other"
        db.commit()
    if change == "owner":
        assert old.send(reply())  # New scope may create, never edit the old owner's message.
        assert [e[0] for e in events] == ["create", "create"]
    else:
        with pytest.raises(FeishuAPIError):
            old.send(reply())
        assert [e[0] for e in events] == ["create"]


def test_invalidation_during_http_prevents_reference_commit(panel_env, monkeypatch):
    factory, bots, _, panel, reply, _, _ = panel_env

    def create(*args):
        # HTTP runs after the database transaction closes, and may observe membership changes.
        with factory() as db:
            invalidate_query_panel(db, bots[0].config.get_val("BOT_ID"))
            db.commit()
        return "om_orphan"

    monkeypatch.setattr(FeishuClient, "create_query_panel", create)
    with pytest.raises(FeishuAPIError):
        panel().send(reply())
    with factory() as db:
        assert db.get(FeishuQueryPanel, bots[0].config.get_val("BOT_ID")).message_id is None


def test_split_message_uses_existing_normal_sender(panel_env):
    _, _, events, panel, reply, _, _ = panel_env
    assert panel().send(reply(text="x" * 17000)) is False
    assert not events


def test_only_full_queries_use_panel_default_commands_untouched(panel_env, client, auth_header):
    _, _, events, _, _, app_id, calls = panel_env
    for index, command in enumerate(
        ["query", "lock node_a 2m", "book node_a 1m", "unlock node_a", "help", "node_a", ""]
    ):
        response = client.post(
            f"/api/feishu/events/{app_id}", json=message(message_id=f"om_case{index}", command=command)
        )
        assert response.status_code == 200
        fn, args = calls.pop(0)
        fn(*args)
    assert [e[0] for e in events] == ["create", "normal", "normal", "normal", "normal", "normal", "update"]


def test_default_off_does_not_touch_panel_table_or_change_payload(feishu_setup, client, monkeypatch, db_session):
    app_id, bots, calls = feishu_setup
    sent = []
    monkeypatch.setattr(FeishuClient, "send", lambda self, payload: sent.append(payload) or [])
    monkeypatch.setattr(FeishuClient, "create_query_panel", lambda *args: pytest.fail("default-off panel create"))
    client.post(f"/api/feishu/events/{app_id}", json=message(command="query"))
    fn, args = calls.pop(0)
    fn(*args)
    assert len(sent) == 1
    assert db_session.query(FeishuQueryPanel).count() == 0
    assert sent[0]["receive_id"] == "oc_group0"
    assert sent[0]["msg_type"] == "post"


def test_query_snapshot_mutex_is_scoped_to_bot(panel_env):
    factory, bots, _, _, _, app_id, _ = panel_env
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    row_ids = []
    with factory() as db:
        for bot in bots:
            row = db.get(Bot, bot.config.get_val("BOT_ID"))
            row_ids.append((row.id, row.uuid, row.identity_revision))

    def hold(index):
        bid, uid, revision = row_ids[index]
        with query_delivery(
            factory, bid, app_id, {"command": "query", "group_id": f"oc_group{index}"}, uid, revision, bots[index]
        ):
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=hold, args=(0,))
    thread.start()
    assert entered.wait(1)
    bid, uid, revision = row_ids[1]
    start = time.monotonic()
    with query_delivery(factory, bid, app_id, {"command": "query", "group_id": "oc_group1"}, uid, revision, bots[1]):
        second.set()
    release.set()
    thread.join(3)
    assert second.is_set() and time.monotonic() - start < 1


def test_status_metadata_is_owner_authorized(panel_env, client, auth_header):
    _, bots, _, panel, reply, _, _ = panel_env
    panel().send(reply())
    bid = bots[0].config.get_val("BOT_ID")
    response = client.get(f"/api/feishu/bots/{bid}/query-panel", headers=auth_header)
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert "om_panel" not in response.text and "scope" not in response.text
    assert client.get(f"/api/feishu/bots/{bid}/query-panel").status_code == 401


def test_same_bot_serializes_snapshot_and_send(panel_env):
    factory, bots, _, _, _, app_id, _ = panel_env
    with factory() as db:
        row = db.get(Bot, bots[0].config.get_val("BOT_ID"))
        identity = (row.id, row.uuid, row.identity_revision)
    order, entered, release = [], threading.Event(), threading.Event()

    def worker(name):
        with query_delivery(
            factory,
            identity[0],
            app_id,
            {"command": "query", "group_id": "oc_group0"},
            identity[1],
            identity[2],
            bots[0],
        ):
            order.append(name + " snapshot")
            if name == "first":
                entered.set()
                release.wait(3)
            order.append(name + " send")

    first = threading.Thread(target=worker, args=("first",))
    first.start()
    assert entered.wait(1)
    next_thread = threading.Thread(target=worker, args=("second",))
    next_thread.start()
    time.sleep(0.05)
    assert order == ["first snapshot"]
    release.set()
    first.join(3)
    next_thread.join(3)
    assert order == ["first snapshot", "first send", "second snapshot", "second send"]


def test_http_delivery_does_not_hold_resource_lock(panel_env, client, monkeypatch):
    _, bots, _, _, _, app_id, calls = panel_env
    acquired = []

    def create(*args):
        def check():
            ok = bots[0]._lock.acquire(blocking=False)
            acquired.append(ok)
            if ok:
                bots[0]._lock.release()

        thread = threading.Thread(target=check)
        thread.start()
        thread.join(1)
        assert acquired == [True]
        return "om_panel"

    monkeypatch.setattr(FeishuClient, "create_query_panel", create)
    client.post(f"/api/feishu/events/{app_id}", json=message(command="query"))
    fn, args = calls.pop(0)
    fn(*args)
    assert acquired == [True]


def test_two_applications_same_chat_never_share_panel_references(panel_env, monkeypatch):
    from lockbot.backend.app.feishu.models import FeishuGroupRoute

    factory, bots, events, panel, reply, _, _ = panel_env
    panel().send(reply())
    bid = bots[1].config.get_val("BOT_ID")
    with factory() as db:
        app = FeishuApplication(
            app_id="cli_second",
            name="Second",
            tenant_key="tenant_two",
            bot_open_id="ou_two",
            app_secret="unused",
            verification_token="unused",
            transport="http",
        )
        db.add(app)
        db.flush()
        aid = app.id
        route = db.query(FeishuGroupRoute).filter_by(bot_id=bid).one()
        route.application_id, route.chat_id = aid, "oc_group0"
        row = db.get(Bot, bid)
        row.group_id = "oc_group0"
        uid, rev = row.uuid, row.identity_revision
        db.commit()
    bots[1].config.set_val("_AUTHORIZED_GROUP_IDS", ["oc_group0"])
    bots[1].config.set_val("_FEISHU_CLIENT", FeishuClient("cli_second", "unused"))
    second = QueryPanel(factory, bid, aid, uid, rev, "oc_group0", bots[1].adapter)
    assert second.send(bots[1].adapter.build_reply("query", [], "oc_group0"))
    assert [event[0] for event in events] == ["create", "create"]
    with factory() as db:
        scopes = [row.scope for row in db.query(FeishuQueryPanel).all()]
        assert len(scopes) == len(set(scopes)) == 2


def test_runtime_toggle_off_never_edits_cached_message(panel_env):
    factory, bots, events, panel, reply, _, _ = panel_env
    panel().send(reply())
    with factory() as db:
        row = db.get(Bot, bots[0].config.get_val("BOT_ID"))
        row.config_overrides = json.dumps({"FEISHU_QUERY_PANEL": False})
        db.commit()
    assert panel().send(reply()) is False
    assert [event[0] for event in events] == ["create"]


def test_stale_orm_membership_invalidation_uses_atomic_generation(panel_env):
    factory, bots, _, panel, reply, _, _ = panel_env
    panel().send(reply())
    bid = bots[0].config.get_val("BOT_ID")
    with factory() as stale:
        cached = stale.get(FeishuQueryPanel, bid)
        initial_generation = cached.generation
        with factory() as current:
            current.query(FeishuQueryPanel).filter_by(bot_id=bid).update(
                {FeishuQueryPanel.generation: FeishuQueryPanel.generation + 1}
            )
            current.commit()
        invalidate_query_panel(stale, bid)
        stale.commit()
    with factory() as db:
        row = db.get(FeishuQueryPanel, bid)
        assert row.generation == initial_generation + 2
        assert row.message_id is None


def test_panel_and_reaction_modes_compose_without_swallowing_outcomes(panel_env, client, monkeypatch):
    factory, bots, events, _, _, app_id, calls = panel_env
    bots[0].config.set_val("FEISHU_REACTION_REPLIES", True)
    with factory() as db:
        row = db.get(Bot, bots[0].config.get_val("BOT_ID"))
        row.config_overrides = json.dumps({"FEISHU_QUERY_PANEL": True, "FEISHU_REACTION_REPLIES": True})
        db.commit()
    reactions = []
    monkeypatch.setattr(FeishuClient, "react", lambda self, mid: reactions.append(mid))
    for index, command in enumerate(["query", "lock node_a 2m", "query", "lock nonexistent 2m", "help"]):
        response = client.post(
            f"/api/feishu/events/{app_id}", json=message(message_id=f"om_combo{index}", command=command)
        )
        assert response.status_code == 200
        fn, args = calls.pop(0)
        fn(*args)
    assert reactions == ["om_combo1"]
    assert [event[0] for event in events] == ["create", "update", "normal", "normal"]


def test_real_membership_hook_invalidates_panel_with_notices_disabled(panel_env, client):
    from lockbot.backend.app.feishu.models import FeishuGroupMembership

    from tests.backend.test_feishu_membership import event

    factory, bots, events, panel, reply, app_id, _ = panel_env
    panel().send(reply())
    bid = bots[0].config.get_val("BOT_ID")
    with factory() as db:
        original_generation = db.get(FeishuQueryPanel, bid).generation
    removed = event(kind="deleted", event_id="panel_removed")
    assert client.post(f"/api/feishu/events/{app_id}", json=removed).json() == {"accepted": True}
    with factory() as db:
        row = db.get(FeishuQueryPanel, bid)
        assert row.message_id is None and row.status == "invalidated"
        assert row.generation > original_generation
        removed_generation = row.generation
        assert db.get(FeishuGroupMembership, (app_id, "oc_group0")).status == "removed"
        assert json.loads(db.get(Bot, bid).config_overrides).get("FEISHU_MEMBERSHIP_NOTICES", False) is False
    assert client.post(f"/api/feishu/events/{app_id}", json=removed).json()["duplicate"] is True
    with factory() as db:
        assert db.get(FeishuQueryPanel, bid).generation == removed_generation
    added = event(kind="added", event_id="panel_rejoined", offset=10)
    assert client.post(f"/api/feishu/events/{app_id}", json=added).json() == {"accepted": True}
    panel().send(reply())
    assert [item[0] for item in events] == ["create", "create"]


def test_membership_during_panel_http_cannot_restore_reference(panel_env, client, monkeypatch):
    from tests.backend.test_feishu_membership import event

    factory, bots, _, panel, reply, app_id, _ = panel_env

    def create(*args):
        result = client.post(f"/api/feishu/events/{app_id}", json=event(kind="deleted", event_id="during_create"))
        assert result.json() == {"accepted": True}
        return "om_stale_created"

    monkeypatch.setattr(FeishuClient, "create_query_panel", create)
    with pytest.raises(FeishuAPIError):
        panel().send(reply())
    with factory() as db:
        row = db.get(FeishuQueryPanel, bots[0].config.get_val("BOT_ID"))
        assert row.message_id is None and row.status == "invalidated"
