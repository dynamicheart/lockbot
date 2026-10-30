"""Management API + real manager + existing verified ingress, no real provider IO."""

# ruff: noqa: F811

import json
import time

import pytest
from lockbot.backend.app.bot_migrations import router
from lockbot.backend.app.bot_migrations.models import BotMigration
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.feishu.service import accept_sdk_event

from backend.test_durable_platform_cutover import subject  # noqa: F401


@pytest.fixture
def api(subject, db_session, monkeypatch):
    _, manager, bot_id, app_id, _ = subject
    monkeypatch.setenv("LOCKBOT_ENABLE_PLATFORM_MIGRATION", "1")
    monkeypatch.setattr(router, "bot_manager", manager)
    db_session.get(Bot, bot_id).utilization_config = None
    db_session.commit()
    return bot_id, app_id


def plan(client, admin_header, api):
    bot_id, app_id = api
    response = client.post(
        f"/api/bots/{bot_id}/migration-plan",
        headers=admin_header,
        json={
            "target_application_id": app_id,
            "target_chat_id": "oc_target",
            "operation_id": "b" * 32,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def signed_reply(db, app_id, command, message_id):
    app = db.get(FeishuApplication, app_id)
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": message_id,
            "event_type": "im.message.receive_v1",
            "app_id": app.app_id,
            "tenant_key": app.tenant_key,
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_reviewer"}, "tenant_key": app.tenant_key},
            "message": {
                "message_id": message_id,
                "chat_type": "group",
                "chat_id": "oc_target",
                "message_type": "text",
                "create_time": str(int(time.time() * 1000) + 1),
                "content": json.dumps({"text": "@_user_1 " + command}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": app.bot_open_id}}],
            },
        },
    }
    assert accept_sdk_event(db, app, payload)["accepted"]


def issue(client, admin_header, state):
    response = client.post(
        f"/api/bot-migrations/{state['operation_id']}/target-challenge",
        headers=admin_header,
        json={
            "reviewed_target_digest": state["target_digest"],
            "publication_evidence_ref": "c" * 32,
            "publication_reviewed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_no_datasource_complete_management_flow_with_distinct_epoch_confirmation(client, admin_header, db_session, api):
    state = plan(client, admin_header, api)
    assert state["next_action"] == "source_challenge" and state["can_abort"] and not state["can_resume"]
    first = issue(client, admin_header, state)
    signed_reply(db_session, api[1], first["command"], "source-receipt")
    path = f"/api/bot-migrations/{state['operation_id']}"
    response = client.post(path + "/resume", headers=admin_header, json={"expected_phase": "planned"})
    assert response.status_code == 200, response.text
    switched = response.json()
    assert switched["phase"] == "switched" and switched["next_action"] == "target_challenge"
    assert not switched["can_abort"]
    second = issue(client, admin_header, switched)
    assert first["proof_id"] != second["proof_id"]
    signed_reply(db_session, api[1], second["command"], "target-receipt")
    response = client.post(path + "/resume", headers=admin_header, json={"expected_phase": "switched"})
    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "completed"
    assert db_session.query(FeishuGroupRoute).one().bot_id == api[0]
    serialized = json.dumps(response.json())
    assert "FAKE_" not in serialized and "purpose" not in serialized and "app_secret" not in serialized


def test_disabled_route_cannot_plan_or_freeze(client, admin_header, db_session, api, monkeypatch):
    monkeypatch.delenv("LOCKBOT_ENABLE_PLATFORM_MIGRATION")
    response = client.post(
        f"/api/bots/{api[0]}/migration-plan",
        headers=admin_header,
        json={
            "target_application_id": api[1],
            "target_chat_id": "oc_target",
            "operation_id": "b" * 32,
        },
    )
    assert response.status_code == 503
    assert db_session.query(BotMigration).count() == 0


def test_expected_phase_guard_rejects_stale_ui_before_switch(client, admin_header, db_session, api):
    state = plan(client, admin_header, api)
    path = f"/api/bot-migrations/{state['operation_id']}"
    response = client.post(path + "/resume", headers=admin_header, json={"expected_phase": "switched"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "migration_phase_changed"
    db_session.expire_all()
    assert db_session.get(BotMigration, state["operation_id"]).phase == "planned"
    assert db_session.get(Bot, api[0]).identity_revision == 0
    response = client.post(path + "/abort", headers=admin_header, json={"expected_phase": "planned"})
    assert response.status_code == 200 and response.json()["phase"] == "aborted"


@pytest.mark.parametrize("role", ["user", "admin"])
def test_non_super_admin_cannot_get_challenge_or_execute(client, admin_header, db_session, api, role):
    from lockbot.backend.app.auth.models import User

    state = plan(client, admin_header, api)
    db_session.get(User, 1).role = role
    db_session.commit()
    path = f"/api/bot-migrations/{state['operation_id']}"
    assert client.get(path, headers=admin_header).status_code == 403
    assert client.post(path + "/resume", headers=admin_header, json={"expected_phase": "planned"}).status_code == 403
    assert client.post(path + "/target-challenge", headers=admin_header, json={}).status_code == 403


def test_capability_is_read_only_default_off_and_discovers_switched_operation(
    client, admin_header, db_session, api, monkeypatch
):
    monkeypatch.delenv("LOCKBOT_ENABLE_PLATFORM_MIGRATION")
    path = f"/api/bots/{api[0]}/migration-management"
    response = client.get(path, headers=admin_header)
    assert response.status_code == 200 and response.json()["enabled"] is False
    assert response.json()["source_uuid"] == db_session.get(Bot, api[0]).uuid
    assert response.json()["latest_operation"] is None
    assert db_session.query(BotMigration).count() == 0
    monkeypatch.setenv("LOCKBOT_ENABLE_PLATFORM_MIGRATION", "1")
    state = plan(client, admin_header, api)
    proof = issue(client, admin_header, state)
    signed_reply(db_session, api[1], proof["command"], "source-for-reload")
    response = client.post(
        f"/api/bot-migrations/{state['operation_id']}/resume", headers=admin_header, json={"expected_phase": "planned"}
    )
    assert response.status_code == 200 and response.json()["phase"] == "switched"
    response = client.get(path, headers=admin_header)
    assert response.status_code == 200
    assert not response.json()["supported_source"]
    assert response.json()["latest_operation"]["operation_id"] == state["operation_id"]
    assert response.json()["latest_operation"]["phase"] == "switched"


def test_original_jwt_epoch_is_rechecked_after_policy_read(client, admin_header, db_session, api, monkeypatch):
    from lockbot.backend.app.auth.models import User

    original = router.reviewed_metrics_policy

    def revoke_after_read(db, bot):
        result = original(db, bot)
        db_session.get(User, 1).token_version = 1
        db_session.commit()
        return result

    monkeypatch.setattr(router, "reviewed_metrics_policy", revoke_after_read)
    response = client.post(
        f"/api/bots/{api[0]}/migration-plan",
        headers=admin_header,
        json={
            "target_application_id": api[1],
            "target_chat_id": "oc_target",
            "operation_id": "d" * 32,
        },
    )
    assert response.status_code == 403
    assert db_session.query(BotMigration).count() == 0


def test_client_cannot_supply_clock_or_skip_publication_review(client, admin_header, db_session, api):
    state = plan(client, admin_header, api)
    path = f"/api/bot-migrations/{state['operation_id']}"
    response = client.post(path + "/resume", headers=admin_header, json={"expected_phase": "planned", "clock_skew": 0})
    assert response.status_code == 422
    response = client.post(
        path + "/target-challenge",
        headers=admin_header,
        json={
            "reviewed_target_digest": state["target_digest"],
            "publication_evidence_ref": "c" * 32,
            "publication_reviewed": False,
        },
    )
    assert response.status_code == 422
    assert not db_session.query(FeishuGroupRoute).count()
