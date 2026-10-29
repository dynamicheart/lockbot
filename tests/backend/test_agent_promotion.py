"""Promotion is a saved platform preference, separate from Agent authorization."""

import pytest
from lockbot.core.base_bot import BaseLockBot
from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.queue_bot import QueueBot


@pytest.fixture
def site_cache(monkeypatch, session_factory):
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    BaseLockBot._invalidate_site_cache()
    yield
    BaseLockBot._invalidate_site_cache()


@pytest.mark.parametrize("bot_type", [NodeBot, DeviceBot, QueueBot])
@pytest.mark.parametrize("enabled", [False, True])
def test_success_reply_spacing(bot_type, enabled, tmp_path, monkeypatch):
    url = "https://example.test/api/agent-skill/SKILL.md"
    bot = bot_type(config_dict={"DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": ["n1"], "_AGENT_SKILL_URL": lambda: url})
    monkeypatch.setattr(
        bot, "_get_site_value", lambda key: "true" if enabled and key == "agent_promotion_enabled" else ""
    )
    monkeypatch.setattr(bot, "_current_usage", lambda key: "\nn1 alice 2.0 小时\nn2 空闲\n\n")
    expected = "✅【资源申请成功】\n\nn1 alice 2.0 小时\nn2 空闲"
    if enabled:
        expected += "\n\n发送给你的Agent来申请资源：" + url
    expected += "\n\n"
    reply = bot.adapter.build_reply(bot._msg_with_usage("success.resource_locked"), ["alice"])
    assert bot.adapter.extract_reply_text(reply) == expected
    links = [item for item in reply["message"]["body"] if item["type"] == "LINK"]
    assert links == ([{"type": "LINK", "href": url}] if enabled else [])
    # The URL callback is empty when this bot has Agent access disabled.
    monkeypatch.setattr(bot, "_agent_skill_url", lambda: "")
    assert url not in bot._msg_with_usage("success.resource_locked")


def test_setting_default_authorization_validation_and_shared_cache(
    client, admin_header, auth_header, site_cache, db_session
):
    from lockbot.backend.app.auth.models import User

    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    endpoint = "/api/admin/settings"
    rows = client.get(endpoint, headers=admin_header).json()
    assert next(row["value"] for row in rows if row["key"] == "agent_promotion_enabled") == "false"
    for cls in (NodeBot, DeviceBot, QueueBot):
        assert cls._get_site_value("agent_promotion_enabled") == ""
    payload = {"settings": {"agent_promotion_enabled": "true"}}
    assert client.put(endpoint, json=payload).status_code == 401
    assert client.put(endpoint, headers=auth_header, json=payload).status_code == 403
    assert client.put(endpoint, headers=admin_header, json=payload).status_code == 200
    for cls in (NodeBot, DeviceBot, QueueBot):
        assert cls._get_site_value("agent_promotion_enabled") == "true"
    assert (
        client.put(endpoint, headers=admin_header, json={"settings": {"agent_promotion_enabled": "yes"}}).status_code
        == 422
    )
    assert (
        client.put(endpoint, headers=admin_header, json={"settings": {"agent_promotion_enabled": "false"}}).status_code
        == 200
    )
    for cls in (NodeBot, DeviceBot, QueueBot):
        assert cls._get_site_value("agent_promotion_enabled") == "false"


def test_queue_renewal_preserves_promotion_link_and_waiting_notice(tmp_path, monkeypatch):
    url = "https://example.test/api/agent-skill/SKILL.md"
    bot = QueueBot(config_dict={"DATA_DIR": str(tmp_path), "CLUSTER_CONFIGS": ["n1"], "_AGENT_SKILL_URL": lambda: url})
    monkeypatch.setattr(bot, "_get_site_value", lambda key: "true" if key == "agent_promotion_enabled" else "")
    bot.lock("alice", "lock n1 1h")
    bot.book("bob", "book n1 1h")
    reply = bot.lock("alice", "lock n1 1h")
    text = bot.adapter.extract_reply_text(reply)
    assert "请注意等待时间已增加" in text
    assert "发送给你的Agent来申请资源：" + url in text
    assert {"type": "LINK", "href": url} in reply["message"]["body"]


@pytest.mark.parametrize(
    "lang,default,prefix",
    [
        ("zh", "发送给你的Agent来申请资源：", "交给你的 Agent："),
        ("en", "Try an Agent for resource requests: ", "Ask your Agent: "),
    ],
)
def test_saved_prefix_updates_running_bot_and_preserves_link(
    lang, default, prefix, client, admin_header, auth_header, site_cache, db_session, tmp_path
):
    from lockbot.backend.app.auth.models import User

    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    endpoint = "/api/admin/settings"
    key = f"agent_promotion_text_{lang}"
    url = "https://deployment.example:8155/api/agent-skill/SKILL.md"
    bot = NodeBot(
        config_dict={
            "DATA_DIR": str(tmp_path),
            "LANGUAGE": lang,
            "CLUSTER_CONFIGS": ["n1"],
            "_AGENT_SKILL_URL": lambda: url,
        }
    )
    # Exercise a real warmed shared cache, then update settings without recreating the bot.
    assert bot._get_site_value(key) == ""
    payload = {"settings": {"agent_promotion_enabled": "true", key: prefix}}
    assert client.put(endpoint, headers=auth_header, json=payload).status_code == 403
    assert client.put(endpoint, headers=admin_header, json=payload).status_code == 200
    for override, expected in [
        (prefix, prefix),
        ("x" * 120, "x" * 120),
        ("", default),
        (None, default),
        ("   ", default),
    ]:
        assert client.put(endpoint, headers=admin_header, json={"settings": {key: override}}).status_code == 200
        parts = bot._msg_with_usage("success.resource_locked")
        assert parts[1] == (expected, url)
        assert parts[0].endswith("\n\n") and parts[2] == "\n"
        reply = bot.adapter.build_reply(parts, ["alice"])
        assert {"type": "LINK", "href": url} in reply["message"]["body"]
        assert bot.adapter.extract_reply_text(reply).endswith(expected + url + "\n\n")
    assert (
        client.put(
            endpoint, headers=admin_header, json={"settings": {"agent_promotion_enabled": "false", key: prefix}}
        ).status_code
        == 200
    )
    assert url not in bot._msg_with_usage("success.resource_locked")


@pytest.mark.parametrize(
    "value",
    [
        "line\nbreak",
        "line\rbreak",
        "tab\t",
        "null\x00",
        "\u202ehidden",
        "\u2028",
        "x" * 121,
        "<b>text</b>",
        "[link](url)",
        "{url}",
        "**bold**",
        "`code`",
        "https://example.test",
        "WWW.example.test",
    ],
)
def test_invalid_promotion_prefix_is_rejected_atomically(value, client, admin_header, db_session):
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.settings.models import SiteSetting

    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    for lang in ("zh", "en"):
        key = f"agent_promotion_text_{lang}"
        response = client.put(
            "/api/admin/settings",
            headers=admin_header,
            json={"settings": {"agent_promotion_enabled": "true", key: value}},
        )
        assert response.status_code == 422
        assert db_session.get(SiteSetting, "agent_promotion_enabled") is None


@pytest.mark.parametrize("role,expected", [("anonymous", 401), ("user", 403), ("admin", 403)])
def test_only_super_admin_can_change_prefixes(role, expected, client, auth_header, admin_header, db_session):
    from lockbot.backend.app.settings.models import SiteSetting

    headers = {"anonymous": {}, "user": auth_header, "admin": admin_header}[role]
    settings = {"agent_promotion_enabled": "true", "agent_promotion_text_zh": "自定义文案："}
    response = client.put("/api/admin/settings", headers=headers, json={"settings": settings})
    assert response.status_code == expected
    assert all(db_session.get(SiteSetting, key) is None for key in settings)


def test_unrelated_settings_and_toggle_preserve_saved_localized_prefixes(client, admin_header, db_session, site_cache):
    from lockbot.backend.app.auth.models import User

    db_session.query(User).filter_by(username="adminuser").one().role = "super_admin"
    db_session.commit()
    endpoint = "/api/admin/settings"
    prefixes = {"agent_promotion_text_zh": "交给你的 Agent：", "agent_promotion_text_en": "Ask your Agent: "}
    assert client.put(endpoint, headers=admin_header, json={"settings": prefixes}).status_code == 200
    for key, value in prefixes.items():
        assert BaseLockBot._get_site_value(key) == value
    # Ordinary later settings saves and toggling promotion must not replace custom copy.
    for enabled in ("true", "false", "true"):
        settings = {"agent_promotion_enabled": enabled, "news_content": "Service notice"}
        assert client.put(endpoint, headers=admin_header, json={"settings": settings}).status_code == 200
        rows = {row["key"]: row["value"] for row in client.get(endpoint, headers=admin_header).json()}
        for key, value in prefixes.items():
            assert rows[key] == value
            assert BaseLockBot._get_site_value(key) == value
