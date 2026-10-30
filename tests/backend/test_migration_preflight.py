"""Migration preview must observe, never switch or silently discard obligations."""

import copy
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption, router
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.migration_preflight import _counts
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute


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
def test_stored_expired_locks_and_notified_offers_remain_blockers(kind):
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
    assert "source_has_outstanding_allocations" in result.json()["blockers"]
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


def test_identity_based_rules_block_even_an_empty_pool(client, admin_header, setup, monkeypatch):
    bot_id, _, body = setup
    live(
        monkeypatch,
        {"node1": {"status": "idle", "current_users": [], "booking_list": []}},
        whitelist=["secret-source-principal"],
    )
    response = client.post(f"/api/bots/{bot_id}/migration-preflight", headers=admin_header, json=body)
    assert "source_identity_rules_require_mapping" in response.json()["blockers"]
    assert response.json()["identity_bound_rule_counts"] == {"DURATION_WHITELIST": 1}
    assert "secret-source-principal" not in response.text
