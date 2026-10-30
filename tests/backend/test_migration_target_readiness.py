"""Provider observations cannot become migration permission or leak credential details."""

from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations import readiness
from lockbot.backend.app.bot_migrations.models import BotMigration
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.core.platforms.feishu import FeishuAPIError


@pytest.fixture
def target(admin_header, db_session, monkeypatch):
    db_session.query(User).filter_by(username="adminuser").one().role = "super_admin"
    bot = Bot(
        user_id=1,
        uuid="11111111-1111-4111-8111-111111111111",
        name="Source",
        bot_type="QUEUE",
        platform="Infoflow",
        cluster_configs='["node1"]',
        config_overrides="{}",
        webhook_url=encryption.encrypt("https://example.invalid/hook"),
        aes_key=encryption.encrypt("FAKE_AES"),
        token=encryption.encrypt("FAKE_TOKEN"),
    )
    app = FeishuApplication(
        app_id="cli_fixture",
        name="Target",
        tenant_key="tenant_fixture",
        bot_open_id="ou_fixture",
        app_secret=encryption.encrypt("FAKE_SECRET"),
        verification_token=encryption.encrypt("FAKE_VERIFY"),
        transport="websocket",
    )
    db_session.add_all([bot, app])
    db_session.commit()
    calls = []

    def membership(chat):
        calls.append(chat)
        return True

    remote = SimpleNamespace(bot_identity=lambda: "ou_fixture", bot_in_group=membership)
    monkeypatch.setattr(readiness, "application_client", lambda app: remote)
    monkeypatch.setattr(readiness.connectors, "snapshot", lambda _: {"state": "receiving", "last_handshake_at": "old"})
    return bot.id, app.id, remote, calls


def probe(client, headers, target):
    bot_id, app_id, _, _ = target
    return client.post(
        f"/api/bots/{bot_id}/migration-target-readiness",
        headers=headers,
        json={"application_id": app_id, "chat_id": "oc_fixture"},
    )


def test_positive_live_probe_never_claims_permission_or_writes_state(client, admin_header, db_session, target):
    before = db_session.query(AuditLog).count()
    response = probe(client, admin_header, target)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["checks"] == {"provider_bot_identity": "matched", "provider_bot_membership": "present"}
    assert data["read_only"] and not data["can_commit"]
    assert "publication_and_usable_range_unverified" in data["blockers"]
    assert not data["receiver_observation_is_end_to_end_proof"]
    assert target[3] == ["oc_fixture"]
    assert db_session.query(BotMigration).count() == 0
    assert db_session.query(FeishuGroupRoute).count() == 0
    assert db_session.query(AuditLog).count() == before
    db_session.expire_all()
    assert db_session.get(Bot, target[0]).platform == "Infoflow"
    assert all(value not in response.text for value in ("FAKE_SECRET", "FAKE_VERIFY", "FAKE_TOKEN", "ou_fixture"))


@pytest.mark.parametrize("role", ["user", "admin"])
def test_non_super_administrator_cannot_probe_provider(client, admin_header, db_session, target, role):
    db_session.get(User, 1).role = role
    db_session.commit()
    assert probe(client, admin_header, target).status_code == 403
    assert not target[3]


def test_identity_mismatch_never_queries_group(client, admin_header, target):
    target[2].bot_identity = lambda: "ou_other_application"
    data = probe(client, admin_header, target).json()
    assert "provider_bot_identity_mismatch" in data["blockers"]
    assert not target[3]


def test_absent_membership_is_not_inferred_from_a_name_or_old_event(client, admin_header, target):
    target[2].bot_in_group = lambda _: False
    data = probe(client, admin_header, target).json()
    assert data["checks"]["provider_bot_membership"] == "absent"
    assert "provider_bot_not_in_target_group" in data["blockers"]


def test_upstream_error_details_are_not_returned(client, admin_header, target):
    def fail(_):
        raise FeishuAPIError("FAKE_SECRET proxy.example.invalid user_token=private")

    target[2].bot_in_group = fail
    response = probe(client, admin_header, target)
    assert "provider_check_unavailable" in response.json()["blockers"]
    assert "FAKE_SECRET" not in response.text and "proxy.example.invalid" not in response.text


def test_changed_application_invalidates_probe(client, admin_header, db_session, target):
    def change(_):
        db_session.get(FeishuApplication, target[1]).tenant_key = "another_tenant"
        db_session.commit()
        return True

    target[2].bot_in_group = change
    data = probe(client, admin_header, target).json()
    assert "target_or_source_changed_during_probe" in data["blockers"]
    assert set(data["checks"].values()) == {"invalidated"}


def test_revoked_actor_cannot_receive_probe_result(client, admin_header, db_session, target):
    def change(_):
        db_session.get(User, 1).role = "admin"
        db_session.commit()
        return True

    target[2].bot_in_group = change
    assert probe(client, admin_header, target).status_code == 403


@pytest.mark.parametrize("configured", [False, True])
def test_absent_optional_metrics_adds_no_authority_blocker(client, admin_header, db_session, target, configured):
    db_session.get(Bot, target[0]).utilization_config = '{"accessMode":"short_lived"}' if configured else None
    db_session.commit()
    data = probe(client, admin_header, target).json()
    assert ("metrics_clock_authority_unverified" in data["blockers"]) is configured
    assert data["can_commit"] is False
