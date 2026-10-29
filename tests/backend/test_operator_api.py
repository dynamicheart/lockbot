"""Real resource behavior through the OP routes, with scoped service credentials."""

# ruff: noqa: F811
import copy
import json
from contextlib import contextmanager

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.resources import snapshot_cache

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture
def operator(api, db_session, monkeypatch):
    row = db_session.get(Bot, api["bot_id"])
    row.api_key = "lbk_test_operator"
    db_session.commit()
    monkeypatch.setattr("lockbot.backend.app.resource_api.operator.bot_manager", api["manager"])
    monkeypatch.setattr(api["bot"].adapter, "send", lambda msg: [(200, "accepted")])
    return {**api, "op_headers": {"Authorization": "Bearer lbk_test_operator"}}


def command(client, case, text, *, other=False, purpose="develop"):
    response = client.post(
        f"/api/bots/{case['bot_id']}/commands",
        headers=case["other_headers"] if other else case["headers"],
        json={"command": text, "agent_label": "test-agent" if other else "dev-agent", "purpose": purpose},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_op_snapshot_paginates_purposes_and_kicklock_preserves_queue(client, operator, db_session, monkeypatch):
    case = operator
    base = f"/api/bots/{case['bot_id']}"
    command(client, case, "lock n1 1h")
    command(client, case, "book n1 1h", other=True, purpose="regression")
    first = client.get(base + "/op/resources?usage_limit=1", headers=case["op_headers"]).json()
    assert first["usage_complete"] is False and first["usage_page"]["next_offset"] == 1
    second = client.get(base + "/op/resources?usage_limit=1&usage_offset=1", headers=case["op_headers"]).json()
    assert second["usage_page"]["has_more"] is False
    purposes = first["usage_declarations"] + second["usage_declarations"]
    assert {p["purpose"] for p in purposes} == {"develop", "regression"}
    assert {p["actor"]["external_user_id"] for p in purposes} == {"alice", "bob"}
    snapshot = client.get(base + "/op/resources", headers=case["op_headers"]).json()
    n1 = snapshot["resources"][0]
    assert n1["holders"][0]["usage"]["agent_label"] == "dev-agent"
    assert n1["booking_list"][0]["usage"]["purpose"] == "regression"
    assert "claimable_by_you" not in n1
    assert "lbk_test_operator" not in json.dumps(snapshot)
    assert "webhook_url" not in snapshot

    before_queue = copy.deepcopy(case["bot"].state.bot_state["n1"]["booking_list"])
    sent = []

    def send_without_state_lock(message):
        assert case["bot"]._lock.acquire(blocking=False), "notification held the state lock"
        case["bot"]._lock.release()
        sent.append(message)

    monkeypatch.setattr(case["bot"].adapter, "send", send_without_state_lock)
    response = client.post(
        base + "/kicklock", headers=case["op_headers"], json={"node_keys": ["n1"], "reason": "Scheduled maintenance"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "freed": ["n1"], "affected_users": ["alice"]}
    operations = client.app.state.service_traffic.snapshot(case["bot_id"])["business_operations"]
    assert operations["total"] == 3  # Agent lock/book plus exactly one OP mutation, no snapshots.
    assert {"source": "operator", "command": "kicklock", "outcome": "dispatched", "count": 1} in operations["rows"]
    assert case["bot"].state.bot_state["n1"]["booking_list"] == before_queue
    assert sent and "Scheduled maintenance" in case["bot"].adapter.extract_reply_text(sent[0])
    after = client.get(base + "/op/resources", headers=case["op_headers"]).json()
    assert after["resources"][0]["holders"] == []
    assert {p["purpose"] for p in after["usage_declarations"]} == {"regression"}
    audit = db_session.query(AuditLog).filter_by(action="bot.kicklock").one()
    assert audit.operator_role == "service_key" and audit.operator_id is None
    assert json.loads(audit.detail)["reason"] == "Scheduled maintenance"
    assert (
        snapshot_cache.get(
            case["bot_id"],
            case["manager"]._generations[case["bot_id"]],
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        is None
    )


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_explicit_scope_validation_and_atomic_unknown_resource(client, operator):
    case = operator
    base = f"/api/bots/{case['bot_id']}"
    command(client, case, "lock n1 1h")
    for keys in ([], ["n1", "n1"], [""], ["n1\n"]):
        response = client.post(
            base + "/kicklock", headers=case["op_headers"], json={"node_keys": keys, "reason": "maintenance"}
        )
        assert response.status_code == 422
    for headers in ({}, case["headers"], case["auth_header"], {"Authorization": "Bearer lbk_wrong"}):
        assert client.get(base + "/op/resources", headers=headers).status_code == 401
        assert (
            client.post(
                base + "/kicklock", headers=headers, json={"node_keys": ["n1"], "reason": "maintenance"}
            ).status_code
            == 401
        )
    response = client.post(
        base + "/kicklock", headers=case["op_headers"], json={"node_keys": ["n1", "missing"], "reason": "maintenance"}
    )
    assert response.status_code == 404
    assert case["bot"].state.bot_state["n1"]["current_users"][0]["user_id"] == "alice"
    idle = client.post(
        base + "/kicklock", headers=case["op_headers"], json={"node_keys": ["n2"], "reason": "maintenance"}
    )
    assert idle.status_code == 200 and idle.json()["freed"] == []


@pytest.mark.parametrize("api", ["NODE", "DEVICE"], indirect=True)
def test_monitor_all_bot_types_but_kicklock_only_queue(client, operator):
    base = f"/api/bots/{operator['bot_id']}"
    view = client.get(base + "/op/resources", headers=operator["op_headers"])
    assert view.status_code == 200
    assert "kicklock" not in view.json()["supported_op_operations"]
    response = client.post(
        base + "/kicklock", headers=operator["op_headers"], json={"node_keys": ["n1"], "reason": "maintenance"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_key_is_reverified_after_admission(client, operator, session_factory, monkeypatch):
    manager = operator["manager"]
    admit = manager.admit

    @contextmanager
    def rotate_then_admit(*args, **kwargs):
        with admit(*args, **kwargs) as lease:
            with session_factory() as db:
                db.get(Bot, operator["bot_id"]).api_key = "lbk_rotated"
                db.commit()
            yield lease

    monkeypatch.setattr(manager, "admit", rotate_then_admit)
    response = client.get(f"/api/bots/{operator['bot_id']}/op/resources", headers=operator["op_headers"])
    assert response.status_code == 401


def test_public_schema_marks_both_new_endpoints_as_op_only(client):
    paths = client.get("/api/public-openapi.json").json()["paths"]
    for path, method in [("/api/bots/{bot_id}/op/resources", "get"), ("/api/bots/{bot_id}/kicklock", "post")]:
        assert paths[path][method]["security"] == [{"OpApiKey": []}]
        assert paths[path][method]["tags"] == ["OP API"]
