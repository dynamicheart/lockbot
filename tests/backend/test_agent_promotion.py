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
    monkeypatch.setattr(bot, "_get_site_value", lambda key: "true" if enabled else "false")
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
    monkeypatch.setattr(bot, "_get_site_value", lambda key: "true")
    bot.lock("alice", "lock n1 1h")
    bot.book("bob", "book n1 1h")
    reply = bot.lock("alice", "lock n1 1h")
    text = bot.adapter.extract_reply_text(reply)
    assert "请注意等待时间已增加" in text
    assert "发送给你的Agent来申请资源：" + url in text
    assert {"type": "LINK", "href": url} in reply["message"]["body"]
