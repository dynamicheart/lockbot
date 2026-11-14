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
    migration_review_receipt,
    verify_migration_review_receipt,
)
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.models import ResourceBinding


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
