"""Independent, atomic Agent access and group notification preferences."""

# ruff: noqa: F811

from unittest.mock import patch

from lockbot.backend.app.resource_api.notifications import notification_budget
from sqlalchemy import text

from backend.test_resource_api_notifications import api  # noqa: F401


def test_notification_switch_suppresses_only_api_broadcasts(client, api, auth_header):
    bot_id = api["bot_id"]
    path = f"/api/bots/{bot_id}"
    with patch("lockbot.backend.app.bots.router.bot_manager.stop_bot") as stop:
        saved = client.put(path, headers=auth_header, json={"resource_api_notify_enabled": False})
        assert saved.status_code == 200
        assert saved.json()["resource_auth_enabled"] is True
        assert saved.json()["resource_api_notify_enabled"] is False
        stop.assert_not_called()
    result = client.post(
        path + "/commands", headers=api["headers"], json={"command": "lock n1 1h", "agent_label": "training worker"}
    )
    assert result.status_code == 200
    assert api["bot"].state_snapshot()[1]["n1"]["current_users"][0]["user_id"] == "alice"
    assert api["bot"].adapter.sent == []
    saved = client.put(path, headers=auth_header, json={"resource_api_notify_enabled": True})
    assert saved.status_code == 200
    notification_budget.reset()
    result = client.post(
        path + "/commands",
        headers=api["headers"],
        json={"command": "unlock n1", "agent_label": "training worker", "purpose": "开发训练功能"},
    )
    assert result.status_code == 200
    text = api["bot"].adapter.extract_reply_text(api["bot"].adapter.sent[-1])
    assert "training worker" in text and "alice" in text
    assert "备注：开发训练功能" in text
    assert "另有" not in text
    for field in ("agent_label", "purpose"):
        invalid = client.post(
            path + "/commands", headers=api["headers"], json={"command": "query n1", field: "line1\nline2"}
        )
        assert invalid.status_code == 422


def test_both_preferences_save_atomically_and_owner_controls_them(client, auth_header, admin_header):
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "preferences",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["n1"],
        },
    )
    assert created.status_code == 201
    bot = created.json()
    assert bot["resource_auth_enabled"] is True and bot["resource_api_notify_enabled"] is True
    path = f"/api/bots/{bot['id']}"
    rejected = client.put(
        path,
        headers=auth_header,
        json={"resource_api_notify_enabled": False, "config_overrides": {"DEFAULT_DURATION": 0}},
    )
    assert rejected.status_code == 422
    assert client.get(path, headers=auth_header).json()["resource_api_notify_enabled"] is True
    assert client.put(path, headers=admin_header, json={"resource_api_notify_enabled": False}).status_code == 403
    saved = client.put(
        path, headers=auth_header, json={"resource_auth_enabled": False, "resource_api_notify_enabled": False}
    )
    assert saved.status_code == 200
    saved = client.put(path, headers=auth_header, json={"resource_auth_enabled": True})
    assert saved.json()["resource_auth_enabled"] is True and saved.json()["resource_api_notify_enabled"] is False


def test_additive_migrations_keep_explicitly_disabled_settings(file_engine):
    from lockbot.backend.app import main

    with file_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE bots (id INTEGER PRIMARY KEY, resource_auth_enabled BOOLEAN NOT NULL DEFAULT 0)")
        )
        connection.execute(text("INSERT INTO bots VALUES (1,0)"))
        connection.execute(text("CREATE TABLE resource_usage_declarations (id INTEGER PRIMARY KEY, purpose TEXT)"))
        connection.execute(text("INSERT INTO resource_usage_declarations VALUES (1,'training')"))
    main._migrate_bots_resource_auth_enabled()
    main._migrate_bots_agent_notifications()
    main._migrate_bots_agent_notifications()
    main._migrate_usage_agent_label()
    main._migrate_usage_agent_label()
    with file_engine.connect() as connection:
        assert tuple(
            connection.execute(text("SELECT resource_auth_enabled,resource_api_notify_enabled FROM bots")).one()
        ) == (0, 1)
        assert tuple(connection.execute(text("SELECT purpose,agent_label FROM resource_usage_declarations")).one()) == (
            "training",
            None,
        )


from backend.test_resource_auth_migration import file_engine  # noqa: E402,F401


def test_help_skill_discovery_obeys_current_saved_agent_switch(client, api, db_session, session_factory, monkeypatch):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.bots.router import _build_config_dict
    from lockbot.backend.app.settings.models import SiteSetting
    from lockbot.core.node_bot import NodeBot

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    db_session.add(SiteSetting(key="platform_url", value="https://lockbot.example"))
    db_session.commit()
    row = db_session.get(Bot, api["bot_id"])
    bot = NodeBot(config_dict=_build_config_dict(row, db_session))
    url = "https://lockbot.example/api/agent-skill/SKILL.md"
    assert bot._agent_skill_url() == url
    assert "bindings" in bot.adapter.extract_reply_text(bot.print_help("alice"))
    row.resource_auth_enabled = False
    db_session.commit()
    hidden = bot.adapter.extract_reply_text(bot.print_help("alice"))
    assert bot._agent_skill_url() == ""
    assert "bind <" not in hidden and "bindings" not in hidden and "unbind" not in hidden
    row.resource_auth_enabled = True
    db_session.commit()
    assert bot._agent_skill_url() == url
