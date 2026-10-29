"""Ordinary management browsing never opts in to private task disclosure or audit."""

# ruff: noqa: F811
import json
from pathlib import Path

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_audit.models import BotAuditLog
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_private_task_permissions import _register
from backend.test_resource_api_resources import api  # noqa: F401
from backend.test_scoped_monitoring import hit


@pytest.fixture
def prepared(client, api, db_session, monkeypatch):
    _register(client, api, monkeypatch)
    owner = db_session.get(User, db_session.get(Bot, api["bot_id"]).user_id)
    owner.role = "super_admin"
    binding = db_session.query(ResourceBinding).filter_by(bound_bot_id=api["bot_id"]).first()
    binding.label = "private-authorization-note"
    db_session.add(
        BotAuditLog(
            operator_username="alice",
            operator_role="agent",
            action="resource_auth.bind",
            target_type="bot",
            target_id=api["bot_id"],
            detail=json.dumps({"label": "private-authorization-note"}),
            result="success",
        )
    )
    db_session.commit()
    from lockbot.backend.app.bots.router import _get_log_file

    Path(_get_log_file(api["bot_id"])).write_text(
        json.dumps(
            {
                "created_at": "2026-01-01 00:00:00",
                "level": "INFO",
                "category": "command",
                "message": "[alice] alias n1 private-project-detail",
                "source": "api",
            }
        )
        + "\n"
    )
    hit(client.app.state.service_traffic, api["bot_id"], "private-credential-ref", "alice")
    return api, owner


def endpoints(bot_id):
    return [
        (f"/api/bots/{bot_id}/state", {"include_usage": True}, "private-project-detail"),
        (f"/api/bots/{bot_id}/logs", {}, "private-project-detail"),
        (f"/api/bots/{bot_id}/agent-history", {}, "private-project-detail"),
        (f"/api/bots/{bot_id}/agent-activity", {}, "private-authorization-note"),
        (f"/api/bots/{bot_id}/audit-logs", {}, "private-authorization-note"),
        (f"/api/bots/{bot_id}/service-traffic", {}, "private-credential-ref"),
        ("/api/admin/service-traffic", {}, "private-credential-ref"),
    ]


def audit_rows(db):
    return db.query(AuditLog).filter_by(action="task_private.read").order_by(AuditLog.id).all()


def test_super_admin_polling_is_public_and_does_not_write_private_read_audits(client, prepared, db_session):
    api, _owner = prepared
    for _ in range(3):
        for path, params, needle in endpoints(api["bot_id"]):
            response = client.get(path, params=params, headers=api["auth_header"])
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert needle not in response.text
    assert audit_rows(db_session) == []
    own = client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
    assert "private-project-detail" in own.text  # Personal-key self access remains unchanged.


def test_each_explicit_private_surface_is_audited_with_a_named_target(client, prepared, db_session):
    api, _owner = prepared
    for path, params, needle in endpoints(api["bot_id"]):
        response = client.get(path, params={**params, "include_private": True}, headers=api["auth_header"])
        assert response.status_code == 200 and needle in response.text
    rows = audit_rows(db_session)
    assert len(rows) == 7
    assert len({json.loads(row.detail)["surface"] for row in rows}) == 7
    assert all(row.target_id == api["bot_id"] and row.target_name for row in rows[:-1])
    assert rows[-1].target_type == "service" and rows[-1].target_id is None and rows[-1].target_name == "Lockbot"
    assert "private-" not in " ".join(row.detail for row in rows)
    # Pagination/export requests are independent evidence; none are silently coalesced.
    for _ in range(2):
        response = client.get(
            f"/api/bots/{api['bot_id']}/logs",
            params={"limit": 10000, "include_private": True},
            headers=api["auth_header"],
        )
        assert "private-project-detail" in response.text
    assert len(audit_rows(db_session)) == 9
    # Returning to the public view creates no audit, even after prior private reads.
    assert (
        "private-project-detail" not in client.get(f"/api/bots/{api['bot_id']}/logs", headers=api["auth_header"]).text
    )
    assert len(audit_rows(db_session)) == 9


@pytest.mark.parametrize("role", ["user", "admin"])
def test_flag_does_not_grant_private_authority_or_search_disclosure(client, prepared, db_session, role):
    api, owner = prepared
    owner.role = role
    db_session.commit()
    for path, params, needle in endpoints(api["bot_id"]):
        response = client.get(path, params={**params, "include_private": True}, headers=api["auth_header"])
        assert response.status_code == (403 if path.startswith("/api/admin") else 200)
        assert needle not in response.text
    response = client.get(
        f"/api/bots/{api['bot_id']}/agent-history",
        params={"query": "private-project-detail", "include_private": True},
        headers=api["auth_header"],
    )
    assert response.json()["items"] == []
    assert audit_rows(db_session) == []


def test_private_search_is_opt_in_and_live_role_changes_take_effect(client, prepared, db_session):
    api, owner = prepared
    path = f"/api/bots/{api['bot_id']}/agent-history"
    assert (
        client.get(path, params={"query": "private-project-detail"}, headers=api["auth_header"]).json()["items"] == []
    )
    assert client.get(
        path, params={"query": "private-project-detail", "include_private": True}, headers=api["auth_header"]
    ).json()["items"]
    owner.role = "user"
    db_session.commit()
    assert (
        client.get(
            path, params={"query": "private-project-detail", "include_private": True}, headers=api["auth_header"]
        ).json()["items"]
        == []
    )
    assert len(audit_rows(db_session)) == 1
