"""Migration preview must observe, never switch or silently discard obligations."""

import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption, router
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.migration_preflight import (
    _counts,
    _empty_destination_state,
    migration_review_receipt,
    verify_migration_review_receipt,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.core.bot_instance import BotInstance


@pytest.fixture
def setup(client, admin_header, db_session):
    user = db_session.query(User).filter_by(username="adminuser").one()
    user.role = "super_admin"
    db_session.commit()
    response = client.post(
        "/api/bots",
        headers=admin_header,
        json={
            "name": "migration-source",
            "bot_type": "QUEUE",
            "webhook_url": "https://example.invalid/hook",
            "aes_key": "FAKE_AES",
            "token": "FAKE_TOKEN",
            "cluster_configs": ["node1"],
        },
    )
    assert response.status_code == 201
    bot_id = response.json()["id"]
    db_session.get(Bot, bot_id).status = "running"
    app = FeishuApplication(
        app_id="cli_fixture",
        name="Fixture",
        tenant_key="fixture_tenant",
        bot_open_id="ou_fixture",
        app_secret=encryption.encrypt("FAKE_ENCRYPTED"),
        verification_token=encryption.encrypt("FAKE_VERIFY"),
        transport="websocket",
    )
    db_session.add(app)
    db_session.flush()
    db_session.add(
        FeishuGroupMembership(
            application_id=app.id,
            chat_id="oc_fixture",
            app_id=app.app_id,
            tenant_key=app.tenant_key,
            status="present",
            event_time_ms=1,
            event_id="fixture",
        )
    )
    db_session.commit()
    return bot_id, app.id, {"application_id": app.id, "chat_id": "oc_fixture"}


def live(monkeypatch, state, revision=0, health=None, whitelist=None):
    @contextmanager
    def admit(*args, **kwargs):
        yield SimpleNamespace(
            generation=7,
            health=health,
            bot=SimpleNamespace(
                _lock=threading.Lock(),
                state=SimpleNamespace(bot_state=state),
                config=SimpleNamespace(
                    get_val=lambda key, default=None: revision if key == "_IDENTITY_REVISION" else whitelist
                ),
            ),
        )

    monkeypatch.setattr(router.bot_manager, "admit", admit)


def test_preview_is_observation_only_without_state_db_or_audit_changes(
    client, admin_header, db_session, setup, monkeypatch
):
    bot_id, _, body = setup
    state = {"node1": {"status": "idle", "current_users": [], "booking_list": []}}
    live(monkeypatch, state)
    original = copy.deepcopy(state)
    before = dict(db_session.get(Bot, bot_id).__dict__)
    audits = db_session.query(AuditLog).count()
    result = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    data = result.json()
    assert data["read_only"] and not data["can_commit"] and not data["reservation_created"]
    assert data["blockers"] == ["cutover_not_implemented"]
    assert data["stored_obligations"]["holders"] == 0
    assert len(data["review_digest"]) == 64
    assert data["credential_changes"]["metrics"]["minimum_wait_seconds"] == 315
    assert state == original
    db_session.expire_all()
    after = dict(db_session.get(Bot, bot_id).__dict__)
    assert {k: v for k, v in before.items() if not k.startswith("_")} == {
        k: v for k, v in after.items() if not k.startswith("_")
    }
    assert db_session.query(AuditLog).count() == audits
    assert db_session.query(FeishuGroupRoute).count() == 0
    for secret in ("FAKE_AES", "FAKE_TOKEN", "FAKE_ENCRYPTED", "example.invalid"):
        assert secret not in result.text


@pytest.mark.parametrize("kind", ["NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"])
def test_stored_expired_locks_and_notified_offers_are_counted(kind):
    entry = {
        "status": "exclusive",
        "current_users": [{"user_id": "sensitive_holder", "start_time": 1, "duration": 1}],
        "booking_list": [{"user_id": "sensitive_booker", "is_notified": True, "start_time": 1}],
    }
    state = {"node1": [entry] if kind in {"DEVICE", "QUEUE_DEVICE"} else entry}
    assert _counts(state, kind) == {"resources": 1, "holders": 1, "reservations": 1, "offers": 1, "non_idle": 1}


def test_preview_never_returns_obligation_principals_or_notes(client, admin_header, setup, monkeypatch):
    bot_id, _, body = setup
    live(
        monkeypatch,
        {
            "node1": {
                "status": "idle",
                "current_users": [],
                "booking_list": [
                    {"user_id": "secret-principal", "purpose": "secret-purpose", "agent_label": "secret-label"}
                ],
            }
        },
    )
    result = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)
    assert result.json()["blockers"] == ["cutover_not_implemented"]
    assert result.json()["reset_policy"] == "clear_live_state_and_reenroll"
    assert result.json()["stored_obligations"]["reservations"] == 1
    assert all(value not in result.text for value in ("secret-principal", "secret-purpose", "secret-label"))


@pytest.mark.parametrize("error", [BotNotRunning, InstanceRetiring, AdmissionFull])
def test_no_live_generation_is_unknown_not_empty(client, admin_header, setup, monkeypatch, error):
    bot_id, _, body = setup
    monkeypatch.setattr(router.bot_manager, "admit", lambda *a, **k: (_ for _ in ()).throw(error()))
    result = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)
    assert result.status_code == 200
    assert result.json()["stored_obligations"] is None
    assert "source_state_unavailable" in result.json()["blockers"]


def test_route_conflict_and_changed_membership_fail_closed(client, admin_header, db_session, setup, monkeypatch):
    bot_id, app_id, body = setup
    live(monkeypatch, {})
    row = db_session.get(FeishuGroupMembership, (app_id, "oc_fixture"))
    row.tenant_key = "wrong_tenant"
    db_session.add(FeishuGroupRoute(application_id=app_id, chat_id="oc_fixture", bot_id=bot_id))
    db_session.commit()
    result = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).json()
    assert {
        "target_membership_identity_changed",
        "target_group_already_bound",
        "source_already_has_feishu_route",
    } <= set(result["blockers"])
    assert db_session.query(FeishuGroupRoute).one().bot_id == bot_id


@pytest.mark.parametrize("role", ["user", "admin"])
def test_preview_requires_current_super_admin(client, admin_header, db_session, setup, role):
    bot_id, _, body = setup
    user = db_session.query(User).filter_by(username="adminuser").one()
    user.role = role
    db_session.commit()
    assert client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).status_code == 403
    assert client.post(f"/api/bots/{bot_id}/migration-preflight", json=body).status_code == 401


def test_preview_rejects_malformed_target_and_extra_mutation_fields(client, admin_header, setup):
    bot_id, _, body = setup
    path = f"/api/bots/{bot_id}/migration-preflight"
    assert client.post(path, headers=admin_header, json={**body, "platform": "Feishu"}).status_code == 422
    assert client.post(path, headers=admin_header, json={**body, "chat_id": "not-a-group"}).status_code == 422


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"node1": {"status": "mystery", "current_users": [], "booking_list": []}},
        {"node1": {"status": "idle", "current_users": "broken", "booking_list": []}},
    ],
)
def test_missing_or_unrecognized_state_is_not_safe_empty(client, admin_header, setup, monkeypatch, state):
    bot_id, _, body = setup
    live(monkeypatch, state)
    data = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).json()
    assert {"source_inventory_mismatch", "source_state_unrecognized"} & set(data["blockers"])
    assert not data["can_commit"]


def test_changed_generation_and_failed_generation_block_preview(client, admin_header, setup, monkeypatch):
    bot_id, _, body = setup
    live(
        monkeypatch, {"node1": {"status": "idle", "current_users": [], "booking_list": []}}, revision=9, health="error"
    )
    data = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).json()
    assert {"source_generation_changed", "source_generation_unhealthy"} <= set(data["blockers"])


def test_missing_card_cannot_look_empty(client, admin_header, db_session, setup, monkeypatch):
    bot_id, _, body = setup
    bot = db_session.get(Bot, bot_id)
    bot.bot_type = "QUEUE_DEVICE"
    bot.cluster_configs = '{"node1":["gpu","gpu"]}'
    db_session.commit()
    live(monkeypatch, {"node1": [{"dev_id": 0, "status": "idle", "current_users": [], "booking_list": []}]})
    data = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).json()
    assert "source_inventory_mismatch" in data["blockers"]


def test_identity_based_rules_are_counted_without_requesting_identity_mapping(client, admin_header, setup, monkeypatch):
    bot_id, _, body = setup
    live(
        monkeypatch,
        {"node1": {"status": "idle", "current_users": [], "booking_list": []}},
        whitelist=["secret-source-principal"],
    )
    response = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)
    assert response.json()["blockers"] == ["cutover_not_implemented"]
    assert response.json()["identity_bound_rule_counts"] == {"DURATION_WHITELIST": 1}
    assert "secret-source-principal" not in response.text


def test_reset_counts_only_live_bindings_and_unexpired_declarations(
    client, admin_header, db_session, setup, monkeypatch
):
    bot_id, _, body = setup
    bot = db_session.get(Bot, bot_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future = now + timedelta(days=3)
    past = now - timedelta(days=3)
    for label, attrs in [
        ("current", {}),
        ("expired", {"credential_expires_at": past}),
        ("revoked", {"revoked_at": past}),
        ("old_revision", {"bound_identity_revision": bot.identity_revision + 1}),
        ("old_uuid", {"bound_bot_uuid": "old-bot"}),
        ("different_platform", {"platform": "feishu"}),
    ]:
        values = dict(
            platform=bot.platform.lower(),
            bound_bot_uuid=bot.uuid,
            bound_identity_revision=bot.identity_revision,
        )
        values.update(attrs)
        row = ResourceBinding(
            public_id=f"fixture-{label}",
            external_user_id=f"hidden-user-{label}",
            bound_bot_id=bot_id,
            credential_digest=f"hidden-credential-{label}",
            **values,
        )
        db_session.add(row)
        db_session.flush()
        for resource, expires in [("node1", future), ("expired-declaration", past)]:
            db_session.add(
                UsageDeclaration(
                    bot_id=bot_id,
                    binding_id=row.id,
                    resource_id=resource,
                    purpose="hidden-purpose",
                    holding_proof="fixture-proof",
                    updated_at=past,
                    expires_at=expires,
                )
            )
    db_session.commit()
    live(monkeypatch, {"node1": {"status": "idle", "current_users": [], "booking_list": []}})

    response = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)

    assert response.status_code == 200
    counts = response.json()["stored_obligations"]
    assert counts["agent_bindings"] == counts["usage_declarations"] == 1
    assert "hidden-" not in response.text
    assert db_session.query(ResourceBinding).count() == 6
    assert db_session.query(UsageDeclaration).count() == 12

    original_review = response.json()["review_digest"]
    binding = db_session.query(ResourceBinding).filter_by(public_id="fixture-current").one()
    binding.credential_generation += 1
    db_session.commit()
    rotated = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body).json()
    assert rotated["stored_obligations"] == counts
    assert rotated["review_digest"] != original_review


def test_review_digest_changes_for_same_count_lease_and_identifies_target(
    client, admin_header, db_session, setup, monkeypatch
):
    bot_id, app_id, target = setup
    state = {
        "node1": {
            "status": "exclusive",
            "current_users": [{"user_id": "private-owner", "start_time": 5, "duration": 100}],
            "booking_list": [{"user_id": "private-booking", "duration": 50}],
        }
    }
    live(monkeypatch, state)

    def observe():
        response = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=target)
        assert response.status_code == 200
        assert "private-" not in response.text
        return response.json()

    original = observe()
    assert observe()["review_digest"] == original["review_digest"]
    state["node1"]["current_users"][0]["duration"] = 200
    renewed = observe()
    assert renewed["stored_obligations"] == original["stored_obligations"]
    assert renewed["configuration_digest"] == original["configuration_digest"]
    assert renewed["review_digest"] != original["review_digest"]

    db_session.get(FeishuApplication, app_id).bot_open_id = "ou_replaced"
    db_session.commit()
    changed_destination = observe()
    assert changed_destination["review_digest"] != renewed["review_digest"]
    assert changed_destination["target"] == renewed["target"]


def test_review_endpoint_checks_snapshot_without_changing_state(client, admin_header, setup, monkeypatch):
    bot_id, _, target = setup
    state = {"node1": {"status": "idle", "current_users": [], "booking_list": []}}
    live(monkeypatch, state)
    preflight_url = f"/api/bots/{bot_id}/migration-preflight"
    review_url = f"/api/bots/{bot_id}/migration-review"
    observation = client.post(preflight_url, headers=admin_header, json=target).json()
    request = {**target, "review_receipt": observation["review_receipt"]["token"]}
    good = client.post(review_url, headers=admin_header, json=request)
    assert good.status_code == 200
    assert good.headers["Cache-Control"] == "no-store"
    assert good.json() == {"review_matches": True, "can_commit": False}
    assert client.post(review_url, json=request).status_code == 401

    state["node1"]["booking_list"].append({"user_id": "fixture-user", "duration": 3600})
    stale = client.post(review_url, headers=admin_header, json=request)
    assert stale.status_code == 409
    assert stale.headers["Cache-Control"] == "no-store"
    assert "fixture-user" not in stale.text
    assert state["node1"]["booking_list"][0]["duration"] == 3600


def test_receipt_is_bound_to_administrator_target_and_time():
    observed = {
        "review_digest": "scope-fingerprint",
        "source": {"bot_uuid": "fixture", "identity_revision": 2},
        "target": {"chat_id": "oc_fixture", "application_id": 1},
    }
    actor = SimpleNamespace(id=42, effective_token_version=7)
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    token = migration_review_receipt(actor, observed, now=now)["token"]
    assert verify_migration_review_receipt(token, actor, observed, now=now)
    assert not verify_migration_review_receipt(token, actor, observed, now=now + timedelta(minutes=5))
    assert not verify_migration_review_receipt(token, actor, observed, now=now - timedelta(seconds=1))
    assert not verify_migration_review_receipt("invalid-receipt", actor, observed, now=now)
    changed_chat = {**observed, "target": {**observed["target"], "chat_id": "oc_changed"}}
    assert not verify_migration_review_receipt(token, actor, changed_chat, now=now)
    other_actor = SimpleNamespace(id=43, effective_token_version=7)
    assert not verify_migration_review_receipt(token, other_actor, observed, now=now)
    actor.effective_token_version = 8
    assert not verify_migration_review_receipt(token, actor, observed, now=now)


@pytest.mark.parametrize("bot_type", ["NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"])
def test_reset_snapshot_uses_normal_runtime_shape_and_preserves_inventory(tmp_path, bot_type):
    device_bot = bot_type in {"DEVICE", "QUEUE_DEVICE"}
    inventory = {"node1": ["P800", "P800"]} if device_bot else ["Node1"]
    normalized = {"node1": ["p800", "p800"]} if device_bot else ["node1"]
    clean = BotInstance(
        bot_type,
        {"BOT_ID": 88, "BOT_NAME": "migration-fixture", "DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": normalized},
    ).bot.state.bot_state
    source = copy.deepcopy(clean)
    entries = source["node1"] if device_bot else [source["node1"]]
    for entry in entries:
        entry.update(
            status="exclusive",
            current_users=[{"user_id": "source-owner"}],
            booking_list=[{"user_id": "source-booker", "is_notified": True}],
            opaque_runtime_metadata={"user_id": "source-user"},
        )
        if device_bot:
            entry["dev_model"] = "outdated"

    reset = _empty_destination_state(source, bot_type, inventory)
    assert reset == clean
    assert all(entry["current_users"] and entry["booking_list"] for entry in entries)
    assert "source-user" not in str(reset)


def test_reset_rejects_case_collisions_in_node_inventory():
    with pytest.raises(ValueError, match="differ"):
        _empty_destination_state({"node1": {}}, "NODE", ["Node1", "node1"])


def test_persisted_migration_fences_manual_routes_and_startup(
    client, admin_header, setup, session_factory, monkeypatch
):
    from lockbot.backend.app import main
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.bots.migration_journal import migration_fence, record_migration_freeze
    from lockbot.backend.app.bots.migration_preflight import MigrationPreflight

    bot_id, _, target = setup
    with session_factory() as db:
        bot = db.get(Bot, bot_id)
        migration = record_migration_freeze(db, bot, MigrationPreflight(**target), "a" * 64)
        operation_id = migration.operation_id
        db.commit()

    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.bot_manager", manager)
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(main, "SessionLocal", session_factory)

    def unexpected_start(*args, **kwargs):
        pytest.fail("Attempted to start a bot with a persisted migration fence")

    monkeypatch.setattr(manager, "start_bot", unexpected_start)
    main._reset_running_bots()
    for action in ("start", "restart", "stop"):
        response = client.post(f"/api/bots/{bot_id}/{action}", headers=admin_header)
        assert response.status_code == 409
        assert "platform migration" in response.text
    with session_factory() as db:
        row = migration_fence(db, bot_id)
        assert row.operation_id == operation_id
        assert row.target_chat_id == target["chat_id"]
        assert db.get(Bot, bot_id).status == "running"
    assert not manager.is_running(bot_id)


@pytest.mark.parametrize(
    ("retirement", "expected"),
    [
        ("unchanged", "drained"),
        ("switch", "drained"),
        ("changed", "source_changed"),
        ("timeout", "drained"),
        ("restart", "recovery_required"),
    ],
)
def test_frozen_migration_rechecks_the_retired_source(setup, db_session, monkeypatch, tmp_path, retirement, expected):
    from lockbot.backend.app.bots.manager import MayHaveExecuted
    from lockbot.backend.app.bots.migration_cutover import (
        activate_switched_migration,
        continue_source_drain,
        freeze_migration,
        switch_drained_migration,
    )
    from lockbot.backend.app.bots.migration_journal import migration_fence, read_destination_checkpoint
    from lockbot.backend.app.bots.migration_preflight import (
        MigrationPreflight,
        MigrationReview,
        observe_migration,
    )

    bot_id, _, destination = setup
    actor = db_session.query(User).filter_by(username="adminuser").one()
    config = SimpleNamespace(get_val=lambda key, default=None: default)
    if retirement == "switch":
        bot = db_session.get(Bot, bot_id)
        bot.api_key = "fixture-old-op"
        binding = ResourceBinding(
            public_id="fixture-old-binding",
            platform="infoflow",
            external_user_id="fixture-owner",
            bound_bot_id=bot_id,
            bound_bot_uuid=bot.uuid,
            bound_identity_revision=bot.identity_revision,
            credential_digest="fixture-old-agent",
        )
        db_session.add(binding)
        db_session.flush()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add(
            UsageDeclaration(
                bot_id=bot_id,
                binding_id=binding.id,
                resource_id="node1",
                purpose="fixture-purpose",
                holding_proof="fixture-proof",
                updated_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        db_session.commit()
    state = {"node1": {"status": "idle", "current_users": [], "booking_list": []}}
    lease = SimpleNamespace(
        generation=1,
        health=None,
        bot=SimpleNamespace(
            _lock=threading.Lock(),
            config=config,
            state=SimpleNamespace(bot_state=state),
            state_snapshot=lambda: (datetime.now(timezone.utc), copy.deepcopy(state)),
        ),
    )

    @contextmanager
    def admit(*args, **kwargs):
        yield lease

    calls = []

    def retire(retiring_bot_id):
        row = migration_fence(db_session, retiring_bot_id)
        assert row.phase == "frozen"
        calls.append(retiring_bot_id)
        if retirement == "changed":
            state["node1"]["booking_list"].append({"user_id": "last-admitted", "duration": 60})
        if retirement == "timeout" and len(calls) == 1:
            raise MayHaveExecuted(retiring_bot_id)
        if retirement == "restart":
            raise BotNotRunning()
        return lease

    manager = SimpleNamespace(
        transition=admit, admit=admit, retire_bot=retire, get_instance=lambda _: None, is_retiring=lambda _: False
    )
    target = MigrationPreflight(**destination)
    observation = observe_migration(db_session, manager, bot_id, target)
    receipt = migration_review_receipt(actor, observation)
    review = MigrationReview(**destination, review_receipt=receipt["token"])
    from fastapi import HTTPException

    stale_review = review.model_copy(update={"review_receipt": "bad-receipt"})
    with pytest.raises(HTTPException) as error:
        freeze_migration(db_session, manager, bot_id, stale_review, actor)
    assert error.value.status_code == 409
    assert migration_fence(db_session, bot_id) is None

    operation = freeze_migration(db_session, manager, bot_id, review, actor)
    assert operation.requested_by == actor.id
    operation_id = operation.operation_id
    if retirement == "timeout":
        assert operation.phase == "frozen"
        operation = continue_source_drain(db_session, manager, bot_id, operation_id, actor)
    assert operation.phase == expected
    assert operation.operation_id == operation_id
    assert continue_source_drain(db_session, manager, bot_id, operation_id, actor).phase == expected
    assert len(calls) == (2 if retirement == "timeout" else 1)
    assert not db_session.query(FeishuGroupRoute).count()
    db_session.expunge_all()
    persisted = migration_fence(db_session, bot_id)
    if retirement == "switch":
        bot = db_session.get(Bot, bot_id)
        original_uuid, original_revision, inventory = bot.uuid, bot.identity_revision, bot.cluster_configs
        original_app = db_session.get(FeishuApplication, destination["application_id"])
        original_app.bot_open_id = "ou_changed"
        db_session.commit()

        def verify(app, chat, drained_at):
            assert app.app_id == "cli_fixture"
            assert chat == "oc_fixture"
            assert drained_at is not None

        with pytest.raises(HTTPException) as stale:
            switch_drained_migration(db_session, manager, bot_id, operation_id, actor, verify_destination=verify)
        assert stale.value.status_code == 409
        original_app.bot_open_id = "ou_fixture"
        db_session.commit()
        switched = switch_drained_migration(db_session, manager, bot_id, operation_id, actor, verify_destination=verify)
        assert switched.phase == "switched"
        assert read_destination_checkpoint(switched)["node1"]["status"] == "idle"
        assert (bot.uuid, bot.identity_revision, bot.cluster_configs) == (
            original_uuid,
            original_revision + 1,
            inventory,
        )
        assert bot.api_key is None and bot.pid is None and bot.status == "stopped"
        assert bot.platform == "Feishu"
        assert db_session.query(ResourceBinding).one().revoked_at is not None
        assert db_session.query(UsageDeclaration).count() == 0
        assert db_session.query(FeishuGroupRoute).one().chat_id == "oc_fixture"
        assert db_session.query(AuditLog).filter_by(action="bot.migration_switch").count() == 1
        assert (
            switch_drained_migration(db_session, manager, bot_id, operation_id, actor, verify_destination=verify).phase
            == "switched"
        )
        assert bot.identity_revision == original_revision + 1

        import json

        from lockbot.backend.app.managed_paths import ManagedPaths

        paths = ManagedPaths(tmp_path / "migration-data")
        monkeypatch.setattr("lockbot.backend.app.bots.migration_cutover.get_managed_paths", lambda: paths)
        writes = []

        def start(*args, **kwargs):
            writes.append("start")
            assert kwargs == {"activate": False}
            if len(writes) == 1:
                raise RuntimeError("fixture start failed")
            return 42

        manager.get_pid = lambda _: None
        manager.start_bot = start
        manager.activate_bot = lambda _: writes.append("activate")

        def build_config(bot, db):
            return {"PLATFORM": bot.platform}

        with pytest.raises(RuntimeError, match="fixture start failed"):
            activate_switched_migration(db_session, manager, bot_id, operation_id, actor, build_config=build_config)
        stored = migration_fence(db_session, bot_id)
        assert stored.phase == "installed"
        state_file = paths.state_file(bot_id)
        assert json.loads(state_file.read_text())["bot_state"]["node1"]["status"] == "idle"
        saved_file = state_file.with_name("hidden.fixture")
        state_file.rename(saved_file)
        with pytest.raises(HTTPException) as missing:
            activate_switched_migration(db_session, manager, bot_id, operation_id, actor, build_config=build_config)
        assert missing.value.status_code == 409
        saved_file.rename(state_file)
        completed = activate_switched_migration(
            db_session, manager, bot_id, operation_id, actor, build_config=build_config
        )
        assert completed.phase == "completed" and completed.destination_payload is None
        assert migration_fence(db_session, bot_id) is None
        state_file.write_text("new-runtime-state")
        assert (
            activate_switched_migration(
                db_session, manager, bot_id, operation_id, actor, build_config=build_config
            ).phase
            == "completed"
        )
        assert state_file.read_text() == "new-runtime-state"
        assert writes == ["start", "start", "activate"]
        return
    if expected == "drained":
        assert read_destination_checkpoint(persisted) == {
            "node1": {"status": "idle", "current_users": [], "booking_list": []}
        }
        persisted.destination_payload = "{}"
        with pytest.raises(ValueError, match="does not match"):
            read_destination_checkpoint(persisted)
    else:
        assert persisted.destination_payload is None
        with pytest.raises(ValueError, match="unavailable"):
            read_destination_checkpoint(persisted)
