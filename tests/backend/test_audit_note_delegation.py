# ruff: noqa: F811
"""Dedicated note delegation never inherits ordinary OP or bot-owner authority."""

import json
from datetime import timedelta

import pytest
from lockbot.backend.app.audit import delegation
from lockbot.backend.app.audit.delegation import AuditCredential
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api import event_log
from sqlalchemy.exc import OperationalError

from backend.test_bots import _sample_bot
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture(autouse=True)
def reset_budget():
    delegation.read_budget.hits.clear()
    yield
    delegation.read_budget.hits.clear()


@pytest.fixture
def setup(client, auth_header, db_session, tmp_path):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    bots = [
        client.post("/api/bots", headers=auth_header, json=_sample_bot(f"audit-{i}")).json()["id"] for i in range(2)
    ]
    for bot_id in bots:
        path = tmp_path / "bots" / str(bot_id) / "agent-operations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "schema_version": 1,
                        "bot_id": bot_id,
                        "timestamp": f"2026-10-30T00:00:0{i}Z",
                        "operation": "command",
                        "command": "alias node1 RAW_COMMAND_CANARY",
                        "purpose": f"NOTE_CANARY {i}",
                        "agent_label": "AGENT_CANARY",
                        "task_category": "operator_testing",
                        "actor": {"platform": "infoflow", "external_user_id": "alice"},
                        "credential_ref": "CREDENTIAL_CANARY",
                        "reply": "REPLY_CANARY",
                        "requested_resources": [{"resource_id": "node1"}],
                        "outcome": "dispatched",
                        "http_status": 200,
                    }
                )
                + "\n"
                for i in range(3)
            )
        )
    response = client.post(
        "/api/admin/audit-credentials",
        headers=auth_header,
        json={"name": "Fixture monitor", "bot_ids": [bots[0]], "expires_in_seconds": 3600},
    )
    assert response.status_code == 201, response.text
    grant = response.json()
    return {
        "bot": bots[0],
        "other": bots[1],
        "grant": grant,
        "headers": {"Authorization": "Bearer " + grant["credential"]},
        "user_id": user.id,
    }


def history_url(setup):
    return f"/api/audit/bots/{setup['bot']}/agent-history"


def test_secret_shown_once_digest_only_and_explicit_scope(client, auth_header, db_session, setup):
    grant = setup["grant"]
    assert grant["scope"] == "task_notes:read" and grant["shown_once"]
    row = db_session.query(AuditCredential).one()
    assert row.digest != grant["credential"] and len(row.digest) == 64
    assert grant["credential"] not in json.dumps(row.__dict__, default=str)
    listing = client.get("/api/admin/audit-credentials", headers=auth_header)
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert listing.json()["items"][0]["id"] == grant["id"]
    assert grant["credential"] not in listing.text and '"digest"' not in listing.text
    audit = db_session.query(AuditLog).filter_by(action="audit_credential.create").one()
    assert grant["credential"] not in str(audit.__dict__) and "Fixture monitor" not in (audit.detail or "")


def test_authorized_history_is_narrow_and_audited_without_note_bodies(client, db_session, setup):
    response = client.get(history_url(setup), headers=setup["headers"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert len(body["items"]) == 3 and body["scope"] == "task_notes:read"
    assert all(
        item["purpose"].startswith("NOTE_CANARY") and item["agent_label"] == "AGENT_CANARY" for item in body["items"]
    )
    assert all(item["public_task"] == "算子测试" for item in body["items"])
    assert not any(value in response.text for value in ("RAW_COMMAND_CANARY", "REPLY_CANARY", "CREDENTIAL_CANARY"))
    audit = db_session.query(AuditLog).filter_by(action="task_notes.read").one()
    assert json.loads(audit.detail)["principal"] == setup["grant"]["id"]
    assert json.loads(audit.detail)["returned_count"] == 3
    assert not any(
        value in str(audit.__dict__) for value in ("NOTE_CANARY", "AGENT_CANARY", setup["grant"]["credential"])
    )


def test_search_only_uses_delegated_fields_not_raw_command_or_reply(client, setup):
    for needle in ("RAW_COMMAND_CANARY", "REPLY_CANARY", "CREDENTIAL_CANARY"):
        result = client.get(history_url(setup), headers=setup["headers"], params={"query": needle})
        assert result.status_code == 200 and result.json()["items"] == []
    result = client.get(history_url(setup), headers=setup["headers"], params={"query": "NOTE_CANARY"})
    assert len(result.json()["items"]) == 3


@pytest.mark.parametrize("token", [None, "ordinary-op", "lbk_fake", "lbr_fake", "eyJfake.JWT.value", "lba1.bad.bad"])
def test_other_credential_families_never_authenticate(client, setup, token):
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    result = client.get(history_url(setup), headers=headers)
    assert result.status_code == 401 and "NOTE_CANARY" not in result.text
    assert result.headers["cache-control"] == "no-store"


def test_management_jwt_and_audit_token_cannot_substitute_for_each_other(client, auth_header, setup):
    assert client.get(history_url(setup), headers=auth_header).status_code == 401
    assert client.get("/api/admin/audit-credentials", headers=setup["headers"]).status_code == 401
    assert client.get(f"/api/bots/{setup['bot']}/op/history", headers=setup["headers"]).status_code == 401
    assert (
        client.post(
            f"/api/bots/{setup['bot']}/commands", headers=setup["headers"], json={"command": "free"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("role", ["user", "admin"])
def test_only_current_super_can_manage_credentials(client, auth_header, db_session, setup, role):
    user = db_session.get(User, setup["user_id"])
    user.role = role
    db_session.commit()
    base = "/api/admin/audit-credentials"
    assert client.get(base, headers=auth_header).status_code == 403
    assert (
        client.post(base, headers=auth_header, json={"name": "Forbidden", "bot_ids": [setup["bot"]]}).status_code == 403
    )
    assert client.post(base + "/" + setup["grant"]["id"] + "/rotate", headers=auth_header, json={}).status_code == 403
    assert client.delete(base + "/" + setup["grant"]["id"], headers=auth_header).status_code == 403
    assert client.get(history_url(setup), headers=setup["headers"]).status_code == 403


@pytest.mark.parametrize(
    "change",
    ["expiry", "revocation", "scope", "issuer_version", "issuer_missing", "bot_identity", "bot_owner", "bot_deleted"],
)
def test_stale_or_wrong_authority_fails_closed(client, db_session, setup, change):
    row = db_session.query(AuditCredential).one()
    bot = db_session.get(Bot, setup["bot"])
    user = db_session.get(User, setup["user_id"])
    if change == "expiry":
        row.expires_at = delegation._now() - timedelta(seconds=1)
    elif change == "revocation":
        row.revoked_at = delegation._now()
    elif change == "scope":
        row.scope = "metrics:read"
    elif change == "issuer_version":
        user.token_version += 1
    elif change == "issuer_missing":
        db_session.delete(user)
    elif change == "bot_identity":
        bot.identity_revision += 1
    elif change == "bot_owner":
        bot.user_id += 100
    elif change == "bot_deleted":
        bot.is_deleted = True
    db_session.commit()
    result = client.get(history_url(setup), headers=setup["headers"])
    assert result.status_code in (401, 403) and "NOTE_CANARY" not in result.text


def test_bot_allowlist_is_explicit_and_rotation_replaces_token_and_cursor(client, auth_header, setup):
    assert client.get(f"/api/audit/bots/{setup['other']}/agent-history", headers=setup["headers"]).status_code == 403
    first = client.get(history_url(setup), headers=setup["headers"], params={"limit": 1}).json()
    assert first["next_cursor"]
    path = "/api/admin/audit-credentials/" + setup["grant"]["id"] + "/rotate"
    rotated = client.post(path, headers=auth_header, json={"expires_in_seconds": 600})
    assert rotated.status_code == 200
    new = {"Authorization": "Bearer " + rotated.json()["credential"]}
    assert client.get(history_url(setup), headers=setup["headers"]).status_code == 401
    assert client.get(history_url(setup), headers=new).status_code == 200
    assert (
        client.get(history_url(setup), headers=new, params={"limit": 1, "cursor": first["next_cursor"]}).status_code
        == 400
    )


def test_cursor_binds_bot_query_page_size_and_expiry(client, auth_header, setup):
    first = client.get(history_url(setup), headers=setup["headers"], params={"limit": 1}).json()
    cursor = first["next_cursor"]
    second = client.get(history_url(setup), headers=setup["headers"], params={"limit": 1, "cursor": cursor}).json()
    assert second["items"][0]["id"] != first["items"][0]["id"]
    for changed in ({"limit": 2}, {"query": "NOTE_CANARY"}):
        assert (
            client.get(
                history_url(setup), headers=setup["headers"], params={"cursor": cursor, "limit": 1, **changed}
            ).status_code
            == 400
        )
    bad = cursor[:-3] + "xxx"
    assert (
        client.get(history_url(setup), headers=setup["headers"], params={"cursor": bad, "limit": 1}).status_code == 400
    )
    path = "/api/admin/audit-credentials/" + setup["grant"]["id"]
    assert client.delete(path, headers=auth_header).status_code == 204
    assert client.delete(path, headers=auth_header).status_code == 204
    assert client.get(history_url(setup), headers=setup["headers"]).status_code == 401
    assert client.post(path + "/rotate", headers=auth_header, json={}).status_code == 409


@pytest.mark.parametrize("change", ["revoke", "demote", "identity", "owner", "rotate"])
def test_mid_read_authority_change_discards_response(client, db_session, setup, monkeypatch, change):
    original = event_log.read_history

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        row = db_session.query(AuditCredential).one()
        if change == "revoke":
            row.revoked_at = delegation._now()
        elif change == "demote":
            db_session.get(User, setup["user_id"]).role = "user"
        elif change == "identity":
            db_session.get(Bot, setup["bot"]).identity_revision += 1
        elif change == "owner":
            db_session.get(Bot, setup["bot"]).user_id += 100
        elif change == "rotate":
            row.generation += 1
        db_session.commit()
        return result

    monkeypatch.setattr(event_log, "read_history", changed)
    result = client.get(history_url(setup), headers=setup["headers"])
    assert result.status_code in (401, 403) and "NOTE_CANARY" not in result.text


def test_failed_disclosure_audit_never_returns_notes(client, setup, monkeypatch):
    def fail(*args, **kwargs):
        raise OperationalError("fake-audit", {}, Exception("fixture failure"))

    monkeypatch.setattr(delegation, "_audit", fail)
    with pytest.raises(OperationalError):
        client.get(history_url(setup), headers=setup["headers"])


def test_read_budget_is_per_credential_and_memory_bounded(client, setup):
    for _ in range(30):
        assert client.get(history_url(setup), headers=setup["headers"]).status_code == 200
    denied = client.get(history_url(setup), headers=setup["headers"])
    assert denied.status_code == 429 and denied.headers["retry-after"] == "60"
    for i in range(300):
        delegation.read_budget.check(str(i))
    assert len(delegation.read_budget.hits) == 256


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "bot_ids": []},
        {"name": "x", "bot_ids": [0]},
        {"name": "x", "bot_ids": [1, 1]},
        {"name": "x", "bot_ids": [1], "scope": "*"},
        {"name": "x", "bot_ids": [1], "expires_in_seconds": 999999},
    ],
)
def test_no_wildcards_or_unbounded_credentials(client, auth_header, setup, body):
    assert client.post("/api/admin/audit-credentials", headers=auth_header, json=body).status_code == 422


def test_live_notes_fail_explicitly_when_state_unavailable(client, setup, monkeypatch):
    from lockbot.backend.app.bots.manager import BotNotRunning, bot_manager

    monkeypatch.setattr(bot_manager, "admit", lambda *a, **kw: (_ for _ in ()).throw(BotNotRunning()))
    result = client.get(f"/api/audit/bots/{setup['bot']}/task-notes", headers=setup["headers"])
    assert result.status_code == 503 and "NOTE_CANARY" not in result.text


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_live_notes_match_current_resource_incarnation(client, db_session, api, monkeypatch):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.bot_manager", api["manager"])
    target = "n1 dev0" if api["bot"].config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"} else "n1"

    def post(command):
        return client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers=api["headers"],
            json={
                "command": command,
                "purpose": "NOTE_CANARY",
                "agent_label": "AGENT_CANARY",
                "task_category": "operator_testing",
            },
        )

    assert post(f"lock {target} 1h").status_code == 200
    grant = client.post(
        "/api/admin/audit-credentials",
        headers=api["auth_header"],
        json={"name": "Live monitor", "bot_ids": [api["bot_id"]]},
    ).json()
    headers = {"Authorization": "Bearer " + grant["credential"]}
    url = f"/api/audit/bots/{api['bot_id']}/task-notes"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1 and items[0]["purpose"] == "NOTE_CANARY"
    assert items[0]["agent_label"] == "AGENT_CANARY"
    assert items[0]["public_task"] and items[0]["untrusted_text"]
    for forbidden in ("binding_id", "authorization_label", "holding_proof", "credential_ref", "revision"):
        assert forbidden not in response.text
    assert post(f"free {target}").status_code == 200
    assert client.get(url, headers=headers).json()["items"] == []


def test_audit_credential_family_is_redacted_from_arbitrary_log_values(setup):
    token = setup["grant"]["credential"]
    assert token not in json.dumps(event_log._safe({"purpose": "prefix " + token + " suffix"}))


def test_required_password_change_invalidates_delegation(client, db_session, setup):
    db_session.get(User, setup["user_id"]).must_change_password = True
    db_session.commit()
    assert client.get(history_url(setup), headers=setup["headers"]).status_code == 403


def test_authenticated_cross_bot_denial_is_audited_without_notes(client, db_session, setup):
    response = client.get(f"/api/audit/bots/{setup['other']}/agent-history", headers=setup["headers"])
    assert response.status_code == 403
    audit = db_session.query(AuditLog).filter_by(action="task_notes.denied").one()
    assert audit.result == "failure"
    assert json.loads(audit.detail)["reason"] == "bot_scope"
    assert "CANARY" not in audit.detail and setup["grant"]["credential"] not in audit.detail


def test_openapi_advertises_separate_bearer_family(client):
    schema = client.get("/openapi.json").json()
    security = schema["components"]["securitySchemes"]["TaskNoteAuditCredential"]
    assert security["type"] == "http" and security["scheme"] == "bearer"
    for suffix in ("agent-history", "task-notes"):
        route = schema["paths"][f"/api/audit/bots/{{bot_id}}/{suffix}"]["get"]
        assert {"TaskNoteAuditCredential": []} in route["security"]
