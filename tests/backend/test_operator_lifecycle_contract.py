# ruff: noqa: F811
"""V2 export must repeat current bot credential checks before disclosure."""

from lockbot.backend.app.bots.models import Bot

from backend.test_operator_api import operator  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def test_v2_rejects_other_credentials_and_remains_public(client, operator):
    bot_id = operator["bot_id"]
    client.post(
        f"/api/bots/{bot_id}/commands",
        headers=operator["headers"],
        json={"command": "lock n1 1h", "purpose": "never-public-purpose", "agent_label": "never-public-label"},
    )
    path = f"/api/bots/{bot_id}/op/lifecycle-audit"
    for headers in ({}, operator["headers"], operator["auth_header"], {"Authorization": "Bearer lbk_foreign"}):
        assert client.get(path, headers=headers).status_code == 401
    response = client.get(path, headers=operator["op_headers"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["audit_schema_version"] == 2
    assert "never-public" not in response.text and "holding_id" not in response.text
    frame = response.json()["items"][0]
    assert frame["contract"]["atomic_state"] is True
    assert frame["contract"]["initiator"]["kind"] == "platform_user"


def test_v2_revocation_during_read_is_checked_before_return(client, operator, db_session, monkeypatch):
    from lockbot.backend.app.resource_api import lifecycle_feed

    def revoke(*args, **kwargs):
        db_session.get(Bot, operator["bot_id"]).api_key = None
        db_session.commit()
        return {"not_returned": True}

    monkeypatch.setattr(lifecycle_feed, "read_feed", revoke)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/lifecycle-audit", headers=operator["op_headers"])
    assert response.status_code == 401 and "not_returned" not in response.text


def test_live_admin_initiator_is_authenticated_account(client, operator, db_session, monkeypatch):
    import copy

    from lockbot.backend.app.auth.models import User

    bot_id = operator["bot_id"]
    user = db_session.get(User, db_session.get(Bot, bot_id).user_id)
    user.role = "admin"
    db_session.commit()
    monkeypatch.setattr("lockbot.backend.app.bots.router.bot_manager", operator["manager"])
    client.post(f"/api/bots/{bot_id}/commands", headers=operator["headers"], json={"command": "lock n1 1h"})
    state = copy.deepcopy(operator["bot"].state.bot_state)
    state["n1"]["current_users"], state["n1"]["status"] = [], "idle"
    assert (
        client.put(f"/api/bots/{bot_id}/state", headers=operator["auth_header"], json={"state": state}).status_code
        == 200
    )
    frame = client.get(f"/api/bots/{bot_id}/op/lifecycle-audit", headers=operator["op_headers"]).json()["items"][0]
    assert frame["contract"]["initiator"] == {"kind": "management_user", "user_id": user.id}
    assert frame["contract"]["atomic_state"] is True
    assert frame["events"][0]["kind"] == "released"


def test_v2_identity_change_during_read_is_rejected(client, operator, db_session, monkeypatch):
    from lockbot.backend.app.resource_api import lifecycle_feed

    def change(*args, **kwargs):
        bot = db_session.get(Bot, operator["bot_id"])
        bot.identity_revision += 1
        db_session.commit()
        return {"not_returned": True}

    monkeypatch.setattr(lifecycle_feed, "read_feed", change)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/lifecycle-audit", headers=operator["op_headers"])
    assert response.status_code == 409 and "not_returned" not in response.text
