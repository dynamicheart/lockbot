# ruff: noqa: F811
from datetime import datetime

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.bot_audit.models import BotAuditLog
from lockbot.backend.app.bot_audit.service import migrate_binding_audit
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.settings.models import SiteSetting

from backend.test_resource_api_resources import api  # noqa: F401
from backend.test_resource_auth import bot_id, chat_hook, issued  # noqa: F401


@pytest.mark.parametrize("action,argument", [("bindings", ""), ("unbind", ""), ("bind", "xxx"), ("bind", "")])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_binding_noop_hints_use_trusted_site_url_and_saved_gate(action, argument, lang, bot_id, chat_hook, db_session):
    row = db_session.get(Bot, bot_id)
    row.config_overrides = '{"LANGUAGE":"' + lang + '"}'
    db_session.merge(SiteSetting(key="platform_url", value="https://trusted.example"))
    db_session.commit()
    text = chat_hook(action, argument, bot_id=bot_id)
    assert "https://trusted.example/api/agent-skill/SKILL.md" in text
    assert ("把链接发给你的 Agent" if lang == "zh" else "Send this link") in text
    row.resource_auth_enabled = False
    db_session.commit()
    assert "https://trusted.example" not in chat_hook(action, argument, bot_id=bot_id)


def test_authorization_audit_is_separate_scoped_and_atomic_migration(
    bot_id, chat_hook, issued, client, auth_header, db_session
):
    chat_hook("bind", issued["bind_code"], bot_id=bot_id)
    chat_hook("unbind", bot_id=bot_id)
    assert db_session.query(AuditLog).filter(AuditLog.action.like("resource_auth.%")).count() == 0
    assert db_session.query(BotAuditLog).count() == 2
    page = client.get(f"/api/bots/{bot_id}/audit-logs", headers=auth_header).json()
    assert page["total"] == 2 and all(x["target_id"] == bot_id for x in page["items"])
    assert client.get(f"/api/bots/{bot_id}/audit-logs").status_code == 401
    assert client.get(f"/api/bots/{bot_id + 999}/audit-logs", headers=auth_header).status_code == 404
    legacy = AuditLog(
        operator_username="old-user",
        operator_role="agent",
        action="resource_auth.bind",
        target_type="bot",
        target_id=bot_id,
        result="success",
        detail='{"source":"chat"}',
        created_at=datetime(2026, 1, 2),
    )
    db_session.add(legacy)
    db_session.commit()
    original = legacy.id
    assert migrate_binding_audit(db_session.get_bind()) == 1
    assert migrate_binding_audit(db_session.get_bind()) == 0
    db_session.expire_all()
    moved = db_session.query(BotAuditLog).filter_by(legacy_audit_id=original).one()
    assert moved.operator_username == "old-user" and moved.created_at == datetime(2026, 1, 2)
    assert db_session.get(AuditLog, original) is None
    assert (
        client.get("/api/audit/logs", headers=auth_header, params={"action": "resource_auth.bind"}).json()["total"] == 0
    )


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_documented_lock_free_and_queue_examples_change_actual_state(api, client):
    base = f"/api/bots/{api['bot_id']}"

    def cmd(text, headers=None):
        res = client.post(base + "/commands", headers=headers or api["headers"], json={"command": text})
        assert res.status_code == 200, res.text

    cmd("lock n1 dev0 30m" if api["bot"].__class__.__name__ == "DeviceBot" else "lock n1 30m")
    state = client.get(base + "/resources", headers=api["headers"]).json()["resources"][0]
    holders = state["holders"] or state.get("devices", [{}])[0].get("holders")
    assert holders[0]["external_user_id"] == "alice"
    if api["bot"].__class__.__name__ == "QueueBot":
        cmd("book n1 2h", api["other_headers"])
        queue = client.get(base + "/resources", headers=api["headers"]).json()["resources"][0]["booking_list"]
        assert queue[0]["external_user_id"] == "bob"
        cmd("free n1", api["other_headers"])
        assert not client.get(base + "/resources", headers=api["headers"]).json()["resources"][0]["booking_list"]
    cmd("free n1 dev0" if api["bot"].__class__.__name__ == "DeviceBot" else "free n1")
    state = client.get(base + "/resources", headers=api["headers"]).json()["resources"][0]
    assert not state["holders"] and not any(d["holders"] for d in state.get("devices", []))


def test_public_command_docs_have_named_examples_and_grammar(client):
    op = client.get("/api/public-openapi.json").json()["paths"]["/api/bots/{bot_id}/commands"]["post"]
    examples = op["requestBody"]["content"]["application/json"]["examples"]
    assert {"node_lock", "device_lock", "queue_book", "release_or_cancel"} <= examples.keys()
    for word in ["free node1", "book node1 2h", "dev0-3", "queue_claim_window_seconds"]:
        assert word in op["description"]
        assert word in client.get("/api/agent-skill/SKILL.md").text


def test_active_authorizations_paginate_without_hiding_unseen_keys(api, client, db_session):
    from datetime import timedelta

    from lockbot.backend.app.resource_auth.models import ResourceBinding

    original = db_session.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"], external_user_id="alice").one()
    original.revoked_at = datetime.utcnow()
    for i in range(25):
        db_session.add(
            ResourceBinding(
                public_id=f"fixture-{i}",
                platform=original.platform,
                external_user_id=f"user-{i}",
                bound_bot_id=original.bound_bot_id,
                bound_bot_uuid=original.bound_bot_uuid,
                bound_identity_revision=original.bound_identity_revision,
                credential_digest=f"fixture-digest-{i}",
                issued_at=datetime.utcnow(),
                credential_expires_at=datetime.utcnow() - timedelta(seconds=1) if i == 24 else None,
            )
        )
    db_session.commit()
    base = f"/api/bots/{api['bot_id']}/agent-activity"
    first = client.get(base, headers=api["auth_header"]).json()
    second = client.get(base, params={"page": 2}, headers=api["auth_header"]).json()
    assert first["total"] == 25 and len(first["items"]) == 20 and len(second["items"]) == 5
    assert not {x["binding_id"] for x in first["items"]} & {x["binding_id"] for x in second["items"]}
    history = client.get(base, params={"status": "history"}, headers=api["auth_header"]).json()
    assert history["total"] == 2 and all(not x["active"] for x in history["items"])
    summary = client.get(f"/api/bots/{api['bot_id']}/agent-summary", headers=api["auth_header"]).json()
    assert summary["authorizations"] == 25


def test_summary_counts_only_live_task_declarations_and_updates_after_release(api, client, db_session, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    monkeypatch.setattr("lockbot.backend.app.bots.manager.bot_manager", api["manager"])
    row = db_session.get(Bot, api["bot_id"])
    row.status = "running"
    db_session.commit()
    base = f"/api/bots/{api['bot_id']}"
    assert client.post(base + "/commands", headers=api["headers"], json={"command": "lock n1 1h"}).status_code == 200
    assert (
        client.put(
            base + "/usage-declarations/n1",
            headers=api["headers"],
            json={"purpose": "feature work", "agent_label": "dev"},
        ).status_code
        == 200
    )
    summary = client.get(base + "/agent-summary", headers=api["auth_header"]).json()
    assert summary["authorizations"] == 2 and summary["usage_registrations"] == 1
    platform = client.get("/api/public/stats", headers=api["auth_header"]).json()
    assert platform["agent_authorizations"] == 2 and platform["agent_usage_registrations"] == 1
    assert client.post(base + "/commands", headers=api["headers"], json={"command": "free n1"}).status_code == 200
    assert client.get(base + "/agent-summary", headers=api["auth_header"]).json()["usage_registrations"] == 0


def test_public_docs_separate_operator_personal_and_anonymous_endpoints(client):
    schema = client.get("/api/public-openapi.json").json()
    groups = {item["name"] for item in schema["tags"]}
    assert groups == {"Agent API", "OP API", "Agent 绑定", "Skill 下载"}
    paths = schema["paths"]
    assert paths["/api/bots/{bot_id}/commands"]["post"]["security"] == [{"AgentKey": []}]
    assert paths["/api/bots/{bot_id}/resources"]["get"]["tags"] == ["Agent API"]
    for path, method in [("/api/bots/{bot_id}/opkick", "post"), ("/api/bots/{bot_id}/locked-users", "get")]:
        assert paths[path][method]["tags"] == ["OP API"]
        assert paths[path][method]["security"] == [{"OpApiKey": []}]
    assert paths["/api/resource-auth/bindings"]["post"]["security"] == []
    assert paths["/api/agent-skill/SKILL.md"]["get"]["security"] == []
    assert not any("audit" in path or "agent-activity" in path for path in paths)
    assert set(schema["components"]["securitySchemes"]) == {"AgentKey", "OpApiKey"}
