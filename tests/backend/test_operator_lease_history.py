# ruff: noqa: F811
"""Allocator history preserves OP scope and cannot turn a reference into authority."""

import json

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api import lease_history

from backend.test_operator_api import operator  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def test_lease_history_is_scoped_public_and_never_inherits_super_owner(client, operator, db_session):
    bot_id = operator["bot_id"]
    db_session.get(User, db_session.get(Bot, bot_id).user_id).role = "super_admin"
    db_session.commit()
    command = client.post(
        f"/api/bots/{bot_id}/commands",
        headers=operator["headers"],
        json={"command": "lock n1 1h", "purpose": "private-purpose", "agent_label": "private-agent"},
    )
    assert command.status_code == 200
    path = f"/api/bots/{bot_id}/op/lease-history"
    for headers in ({}, operator["headers"], operator["auth_header"], {"Authorization": "Bearer lbk_foreign"}):
        assert client.get(path, headers=headers).status_code == 401
    response = client.get(path, headers=operator["op_headers"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "private-" not in response.text and "holding_id" not in response.text
    assert response.json()["checkpoint"]["consistent"]
    lease = response.json()["items"][0]["state"]["allocations"][0]
    assert len(lease["ref"]) == 64
    forbidden = client.post(
        f"/api/bots/{bot_id}/commands",
        headers=operator["headers"],
        json={"command": "free n1", "lease_ref": lease["ref"]},
    )
    assert forbidden.status_code == 422
    db_session.get(Bot, bot_id).api_key = None
    db_session.commit()
    assert client.get(path, headers=operator["op_headers"]).status_code == 401


def test_rotation_during_lease_read_is_rejected_before_disclosure(client, operator, db_session, monkeypatch):
    from lockbot.backend.app.resource_api import event_log

    def rotate(*args, **kwargs):
        assert kwargs["stream"] == "lifecycle"
        db_session.get(Bot, operator["bot_id"]).api_key = "lbk_new_key"
        db_session.commit()
        return {"items": [{"not_returned": True}]}

    monkeypatch.setattr(event_log, "read_history", rotate)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/lease-history", headers=operator["op_headers"])
    assert response.status_code == 401 and "not_returned" not in response.text


def test_writer_snapshot_limit_marks_coverage_incomplete(client, operator, monkeypatch):
    monkeypatch.setattr(lease_history, "MAX_LINE_BYTES", 20)
    operator["bot"].config._lifecycle_observer.committed(operator["bot"].state.bot_state)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/lease-history", headers=operator["op_headers"])
    assert response.status_code == 200
    assert not response.json()["checkpoint"]["consistent"]
    assert response.json()["checkpoint"]["state"]["complete"] is False
    assert "credential" not in json.dumps(response.json())


def test_live_admin_state_edit_uses_the_same_committed_journal(client, operator, monkeypatch, db_session):
    db_session.get(User, db_session.get(Bot, operator["bot_id"]).user_id).role = "admin"
    db_session.commit()
    import copy

    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", operator["manager"])
    bot_id = operator["bot_id"]
    client.post(f"/api/bots/{bot_id}/commands", headers=operator["headers"], json={"command": "lock n1 1h"})
    state = copy.deepcopy(operator["bot"].state.bot_state)
    state["n1"]["current_users"] = []
    state["n1"]["status"] = "idle"
    response = client.put(f"/api/bots/{bot_id}/state", headers=operator["auth_header"], json={"state": state})
    assert response.status_code == 200, response.text
    history = client.get(f"/api/bots/{bot_id}/op/lease-history", headers=operator["op_headers"]).json()
    assert history["checkpoint"]["consistent"]
    assert history["items"][0]["source"] == "admin"
    assert any(event["kind"] == "released" for event in history["items"][0]["events"])
