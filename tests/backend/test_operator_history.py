"""Ordinary bot OP history stays public, scoped and bounded even for super-admin owners."""

# ruff: noqa: F811
import json
from pathlib import Path

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api import event_log

from backend.test_bots import _sample_bot
from backend.test_operator_api import operator  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def history_file(tmp_path, bot_id, entries):
    path = tmp_path / "bots" / str(bot_id) / "agent-operations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in entries))
    return path


def event(bot_id, actor="alice", **extra):
    return {
        "schema_version": 1,
        "bot_id": bot_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": {"platform": "infoflow", "external_user_id": actor},
        "operation": "command",
        "command": "lock n1 2h",
        "requested_resources": [{"resource_id": "n1", "device_ids": [0, 1]}],
        "task_category": "operator_testing",
        "purpose": "private-purpose",
        "agent_label": "private-agent",
        "credential_ref": "private-credential",
        "reply": "private-reply",
        "observed_state": {"holding_id": "private-proof"},
        "outcome": "dispatched",
        "http_status": 200,
        **extra,
    }


def test_op_history_never_inherits_owner_private_authority(client, operator, db_session, tmp_path):
    bot_id = operator["bot_id"]
    history_file(tmp_path, bot_id, [event(bot_id)])
    db_session.get(User, db_session.get(Bot, bot_id).user_id).role = "super_admin"
    db_session.commit()
    response = client.get(f"/api/bots/{bot_id}/op/history", headers=operator["op_headers"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "private-" not in response.text
    item = response.json()["items"][0]
    assert item["actor"]["external_user_id"] == "alice" and item["command"] == "lock"
    assert item["public_task"] == "算子测试" and item["requested_resources"][0]["device_ids"] == [0, 1]
    assert response.json()["coverage"]["complete_lifecycle"] is False
    assert response.json()["coverage"]["complete_history"] is False


def test_history_rejects_missing_invalid_foreign_agent_management_and_rotated_keys(
    client, operator, auth_header, admin_header, db_session, tmp_path
):
    bot_id = operator["bot_id"]
    history_file(tmp_path, bot_id, [event(bot_id)])
    path = f"/api/bots/{bot_id}/op/history"
    other_id = client.post("/api/bots", headers=admin_header, json=_sample_bot("foreign")).json()["id"]
    db_session.get(Bot, other_id).api_key = "lbk_foreign_operator"
    db_session.commit()
    for headers in (
        {},
        {"Authorization": "Bearer lbk_bad"},
        {"Authorization": "Bearer lbk_foreign_operator"},
        operator["headers"],
        auth_header,
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
        assert "private-" not in response.text
    assert client.get(f"/api/bots/{other_id}/op/history", headers=operator["op_headers"]).status_code == 401
    db_session.get(Bot, bot_id).api_key = "lbk_rotated_operator"
    db_session.commit()
    assert client.get(path, headers=operator["op_headers"]).status_code == 401
    db_session.get(Bot, bot_id).api_key = None
    db_session.commit()
    assert client.get(path, headers={"Authorization": "Bearer lbk_rotated_operator"}).status_code == 401


def test_key_rotation_during_file_read_fails_closed(client, operator, db_session, monkeypatch):
    def rotate(*args, **kwargs):
        assert kwargs["private"] is False
        db_session.get(Bot, operator["bot_id"]).api_key = "lbk_rotated_during_read"
        db_session.commit()
        return {"items": [{"actor": "should-not-be-returned"}]}

    monkeypatch.setattr(event_log, "read_history", rotate)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/history", headers=operator["op_headers"])
    assert response.status_code == 401 and "should-not-be-returned" not in response.text


def test_search_and_pages_never_observe_private_fields(client, operator, tmp_path):
    bot_id = operator["bot_id"]
    history_file(tmp_path, bot_id, [event(bot_id, actor="alice"), event(bot_id, actor="bob")])
    path = f"/api/bots/{bot_id}/op/history"
    for needle in ("private-purpose", "private-agent", "private-reply", "private-credential", "private-proof"):
        result = client.get(path, params={"query": needle}, headers=operator["op_headers"]).json()
        assert result["items"] == []
    first = client.get(path, params={"query": "bob", "limit": 1}, headers=operator["op_headers"]).json()
    assert [item["actor"]["external_user_id"] for item in first["items"]] == ["bob"]
    second = client.get(
        path, params={"cursor": first["next_cursor"], "limit": 1}, headers=operator["op_headers"]
    ).json()
    assert [item["actor"]["external_user_id"] for item in second["items"]] == ["alice"]
    assert "private-" not in json.dumps(first["items"] + second["items"])


def test_append_rotation_and_retention_gap_are_explicit(client, operator, tmp_path):
    bot_id = operator["bot_id"]
    target = history_file(tmp_path, bot_id, [event(bot_id, actor="older"), event(bot_id, actor="newer")])
    path = f"/api/bots/{bot_id}/op/history"
    first = client.get(path, params={"limit": 1}, headers=operator["op_headers"]).json()
    with target.open("a") as output:
        output.write(json.dumps(event(bot_id, actor="appended")) + "\n")
    target.rename(Path(str(target) + ".1"))
    history_file(tmp_path, bot_id, [event(bot_id, actor="new-file")])
    second = client.get(
        path, params={"limit": 1, "cursor": first["next_cursor"]}, headers=operator["op_headers"]
    ).json()
    assert [item["actor"]["external_user_id"] for item in second["items"]] == ["older"]
    Path(str(target) + ".1").unlink()
    gap = client.get(path, params={"cursor": first["next_cursor"]}, headers=operator["op_headers"])
    assert gap.status_code == 409 and gap.headers["cache-control"] == "no-store"
    assert "refresh" in gap.text.lower()
    assert client.get(path, params={"cursor": "invalid-cursor"}, headers=operator["op_headers"]).status_code == 400


def test_bounded_scan_handles_corruption_and_partial_records(client, operator, tmp_path, monkeypatch):
    bot_id = operator["bot_id"]
    target = history_file(tmp_path, bot_id, [event(bot_id, actor=str(index)) for index in range(12)])
    with target.open("a") as output:
        output.write('null\n[]\nnot-json\n{"bot_id":')
    monkeypatch.setattr(event_log, "READ_BUDGET", 1400)
    path = f"/api/bots/{bot_id}/op/history"
    page = client.get(path, params={"limit": 100}, headers=operator["op_headers"]).json()
    actors = []
    while True:
        assert page["scanned_bytes"] <= 1400 and page["coverage"]["read_budget_bytes"] == 1400
        actors.extend(item["actor"]["external_user_id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        page = client.get(
            path, params={"limit": 100, "cursor": page["next_cursor"]}, headers=operator["op_headers"]
        ).json()
    assert len(actors) == len(set(actors)) == 12
    for params in ({"limit": 101}, {"limit": 0}, {"query": "x" * 129}, {"cursor": "x" * 257}):
        assert client.get(path, params=params, headers=operator["op_headers"]).status_code == 422


def test_usage_update_and_delete_preserve_available_previous_public_category(client, operator, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", operator["manager"])
    bot_id = operator["bot_id"]
    command = client.post(
        f"/api/bots/{bot_id}/commands",
        headers=operator["headers"],
        json={"command": "lock n1 1h", "purpose": "private-purpose", "task_category": "operator_testing"},
    )
    assert command.status_code == 200
    path = f"/api/bots/{bot_id}/usage-declarations/n1"
    assert (
        client.put(
            path, headers=operator["headers"], json={"purpose": "private-updated", "task_category": "unit_testing"}
        ).status_code
        == 200
    )
    assert client.delete(path, headers=operator["headers"]).status_code == 200
    history = client.get(f"/api/bots/{bot_id}/op/history", headers=operator["op_headers"]).json()["items"]
    updated = next(row for row in history if row["operation"] == "usage.update")
    deleted = next(row for row in history if row["operation"] == "usage.delete")
    assert updated["previous_task_category"] == "operator_testing" and updated["task_category"] == "unit_testing"
    assert deleted["previous_task_category"] == "unit_testing"
    assert "private-" not in json.dumps(history)
