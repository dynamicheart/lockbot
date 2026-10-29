# ruff: noqa: F811
"""Management, operational and private-task authority must remain separate."""

import copy
import json
from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_audit.models import BotAuditLog
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.privacy import can_read_private_tasks, public_event
from sqlalchemy import text

from backend.test_resource_api_resources import api  # noqa: F401


def _register(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", api["manager"])
    monkeypatch.setattr("lockbot.backend.app.resource_api.operator.bot_manager", api["manager"])
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={
            "command": "lock n1 1h",
            "purpose": "private-project-detail",
            "agent_label": "private-agent-name",
            "task_category": "operator_testing",
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("management_role", ["user", "admin", "super_admin"])
def test_management_state_uses_current_role_not_bot_ownership_or_username(
    client, api, db_session, monkeypatch, management_role
):
    _register(client, api, monkeypatch)
    user = db_session.get(User, db_session.get(Bot, api["bot_id"]).user_id)
    user.role = management_role
    user.username = "alice"  # Matching an IM user is not a verified identity link.
    db_session.commit()
    path = f"/api/bots/{api['bot_id']}/state?include_usage=true"
    response = client.get(path, headers=api["auth_header"])
    assert response.status_code == 200
    if management_role == "super_admin":
        assert "private-project-detail" in response.text and "private-agent-name" in response.text
        log = db_session.query(AuditLog).filter_by(action="task_private.read").one()
        assert json.loads(log.detail) == {"surface": "management.state"}
        assert "private-project-detail" not in log.detail
    else:
        assert "private-project-detail" not in response.text and "private-agent-name" not in response.text
        assert "operator_testing" in response.text
        assert '"authorization_label"' not in response.text and '"binding_id"' not in response.text
    own = client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
    assert "private-project-detail" in own.text and "private-agent-name" in own.text
    # A role change takes effect without replacing the user's management token.
    user.role = "user"
    db_session.commit()
    assert "private-project-detail" not in client.get(path, headers=api["auth_header"]).text


def test_helper_does_not_trust_stale_super_admin_object(db_session):
    user = User(username="privacy-admin", email="privacy@example.invalid", password_hash="unused", role="super_admin")
    db_session.add(user)
    db_session.commit()
    assert can_read_private_tasks(db_session, user)
    db_session.execute(text("UPDATE users SET role='user' WHERE id=:id"), {"id": user.id})
    db_session.commit()
    stale = SimpleNamespace(id=user.id, role="super_admin")
    assert not can_read_private_tasks(db_session, stale)


def test_op_key_never_inherits_private_authority_from_super_admin_owner(client, api, db_session, monkeypatch):
    _register(client, api, monkeypatch)
    bot = db_session.get(Bot, api["bot_id"])
    bot.api_key = "lbk_privacy_test_operator"
    db_session.get(User, bot.user_id).role = "super_admin"
    db_session.commit()
    response = client.get(
        f"/api/bots/{bot.id}/op/resources", headers={"Authorization": "Bearer lbk_privacy_test_operator"}
    )
    assert response.status_code == 200
    assert "private-project-detail" not in response.text and "private-agent-name" not in response.text
    assert "operator_testing" in response.text
    for item in response.json()["usage_declarations"]:
        assert not {"purpose", "agent_label", "binding_id", "authorization_label"}.intersection(item)


def test_activity_and_authorization_audit_hide_private_labels_for_bot_owner(client, api, db_session):
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    binding = db_session.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"]).first()
    binding.label = "private-authorization-note"
    db_session.add(
        BotAuditLog(
            operator_username="alice",
            operator_role="agent",
            action="resource_auth.bind",
            target_type="bot",
            target_id=api["bot_id"],
            detail=json.dumps(
                {
                    "source": "chat",
                    "label": "private-authorization-note",
                    "credential_id": "private-id",
                    "future_private": "hidden",
                }
            ),
            result="success",
        )
    )
    db_session.commit()
    activity = client.get(f"/api/bots/{api['bot_id']}/agent-activity", headers=api["auth_header"])
    assert activity.status_code == 200 and activity.json()["items"]
    assert "private-authorization-note" not in activity.text and binding.public_id not in activity.text
    assert all(row["binding_id"] is None for row in activity.json()["items"])
    audit = client.get(f"/api/bots/{api['bot_id']}/audit-logs", headers=api["auth_header"])
    assert audit.status_code == 200
    assert (
        "private-authorization-note" not in audit.text
        and "private-id" not in audit.text
        and "future_private" not in audit.text
    )


def test_legacy_log_query_cannot_download_or_page_into_private_text(client, api, db_session, monkeypatch):
    from lockbot.backend.app.bots.router import _get_log_file

    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", api["manager"])
    path = _get_log_file(api["bot_id"])
    entries = [
        {
            "created_at": "2026-01-01 00:00:00",
            "level": "ERROR",
            "category": "system",
            "message": "private-project-detail",
            "purpose": "private-project-detail",
            "future_private": "hidden",
        },
        {
            "created_at": "2026-01-01 00:00:01",
            "level": "INFO",
            "category": "command",
            "source": "api",
            "message": "[alice] alias n1 private-project-detail",
            "agent_label": "private-agent-name",
        },
    ]
    from pathlib import Path

    Path(path).write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    endpoint = f"/api/bots/{api['bot_id']}/logs"
    for query in ("", "?category=system", "?category=command", "?limit=1&page=2", "?since=2025-01-01"):
        response = client.get(endpoint + query, headers=api["auth_header"])
        assert response.status_code == 200
        assert (
            "private-project-detail" not in response.text
            and "private-agent-name" not in response.text
            and "future_private" not in response.text
        )
    rows = client.get(endpoint, headers=api["auth_header"]).json()
    assert rows[0]["message"] == "[alice] alias" and rows[0]["source"] == "api"
    user = db_session.get(User, db_session.get(Bot, api["bot_id"]).user_id)
    user.role = "super_admin"
    db_session.commit()
    assert "private-project-detail" in client.get(endpoint, headers=api["auth_header"]).text


def test_public_event_allowlist_excludes_future_fields_and_searchable_private_text():
    raw = {
        "schema_version": 1,
        "timestamp": "2026-01-01",
        "bot_id": 1,
        "operation": "command",
        "command": "alias n1 private-note",
        "purpose": "private-note",
        "agent_label": "private-agent",
        "binding_id": "private-id",
        "future_private": {"payload": "private-note"},
        "reply": "private-note",
        "task_category": "operator_testing",
        "actor": {"platform": "infoflow", "external_user_id": "alice", "secret": "private-note"},
        "requested_resources": [{"resource_id": "n1", "device_ids": [0], "purpose": "private-note"}],
        "observed_state": {"resources": [{"holding_id": "private-proof"}]},
        "http_status": 200,
        "outcome": "dispatched",
    }
    before = copy.deepcopy(raw)
    public = public_event(raw)
    assert raw == before
    assert public["command"] == "alias" and public["public_task"] == "算子测试"
    assert public["requested_resources"] == [{"resource_id": "n1", "device_ids": [0]}]
    assert "private-" not in json.dumps(public) and "future_private" not in public
    assert public_event({"requested_resources": None})["requested_resources"] == []


def test_public_usage_event_keeps_legacy_resource_identifier():
    assert public_event({"requested_resources": ["n1:dev0"]})["requested_resources"] == [{"resource_id": "n1:dev0"}]


def test_public_history_rejects_malformed_nested_scalars_and_unknown_operations():
    raw = {
        key: {"private": "hidden"}
        for key in (
            "schema_version",
            "timestamp",
            "bot_id",
            "bot_type",
            "operation",
            "http_status",
            "outcome",
            "duration_ms",
        )
    }
    raw["actor"] = {"platform": {"private": "hidden"}, "external_user_id": ["hidden"]}
    assert public_event(raw) == {"actor": {}, "command": None, "requested_resources": []}
    assert "operation" not in public_event({"operation": "unknown private operation"})
    assert "outcome" not in public_event({"outcome": "private error details"})
