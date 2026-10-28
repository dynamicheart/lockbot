"""Agent access participates in the normal configuration save transaction."""

from unittest.mock import patch

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.bots.models import Bot


def create(client, headers, **extra):
    response = client.post(
        "/api/bots",
        headers=headers,
        json={
            "name": "agent-save",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["n1"],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_create_enables_access_by_default(client, auth_header):
    bot_id = create(client, auth_header)
    assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["resource_auth_enabled"] is True


def test_flag_only_save_does_not_stop_a_running_instance(client, auth_header, db_session):
    bot_id = create(client, auth_header, resource_auth_enabled=False)
    bot = db_session.get(Bot, bot_id)
    bot.status = "running"
    db_session.commit()
    with patch("lockbot.backend.app.bots.router.bot_manager.stop_bot") as stop:
        for enabled in [True, False]:
            response = client.put(f"/api/bots/{bot_id}", headers=auth_header, json={"resource_auth_enabled": enabled})
            assert response.status_code == 200, response.text
            assert response.json()["resource_auth_enabled"] is enabled
            assert response.json()["status"] == "running"
        stop.assert_not_called()
    assert db_session.query(AuditLog).filter_by(action="bot.update").count() == 2
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action.in_(("bot.resource_auth_enabled", "bot.resource_auth_disabled")))
        .count()
        == 0
    )


def test_invalid_combined_save_changes_neither_name_nor_access(client, auth_header):
    bot_id = create(client, auth_header, resource_auth_enabled=False)
    response = client.put(
        f"/api/bots/{bot_id}",
        headers=auth_header,
        json={
            "name": "must-not-save",
            "resource_auth_enabled": True,
            "config_overrides": {"DEFAULT_DURATION": 0},
        },
    )
    assert response.status_code == 422
    bot = client.get(f"/api/bots/{bot_id}", headers=auth_header).json()
    assert bot["name"] == "agent-save" and bot["resource_auth_enabled"] is False


def test_non_owner_cannot_change_access_through_config_save(client, auth_header, admin_header):
    bot_id = create(client, auth_header, resource_auth_enabled=False)
    assert (
        client.put(f"/api/bots/{bot_id}", headers=admin_header, json={"resource_auth_enabled": True}).status_code == 403
    )
    assert client.get(f"/api/bots/{bot_id}", headers=auth_header).json()["resource_auth_enabled"] is False


def test_repeated_gate_value_does_not_repeat_permission_audit(client, auth_header, db_session):
    bot_id = create(client, auth_header, resource_auth_enabled=False)
    for value in [False, False, True, True, False, False]:
        response = client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": value})
        assert response.status_code == 200, response.text
    assert db_session.query(AuditLog).filter_by(action="bot.update").count() == 2
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action.in_(("bot.resource_auth_enabled", "bot.resource_auth_disabled")))
        .count()
        == 0
    )
    response = client.put(f"/api/bots/{bot_id}", headers=auth_header, json={"resource_auth_enabled": False})
    assert response.status_code == 200
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action.in_(("bot.resource_auth_enabled", "bot.resource_auth_disabled")))
        .count()
        == 0
    )
