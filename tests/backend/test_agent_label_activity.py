# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import quote

import sqlalchemy as sa
from fastapi.testclient import TestClient
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.resource_api import activity
from lockbot.backend.app.resource_api.activity import AgentLabelActivity
from lockbot.backend.app.resource_auth.dependencies import ResourceActor
from lockbot.backend.app.resource_auth.models import ResourceBinding
from sqlalchemy.orm import Session

from backend.test_personal_panel_connections import PREFIX, env  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401

HEADER = "X-Lockbot-Agent-Label"


def test_activity_is_scoped_to_proven_bindings_and_observes_only_peer_ip(client, api, env, db_session, admin_header):
    _, agent, browser, _ = env
    alice = api["headers"]
    bob = api["other_headers"]
    context = "/api/agent/context"
    other_peer = TestClient(client.app, client=("2001:db8::5", 50000))
    first = quote("编程 Cafe\u0301")
    assert client.get(context, headers={**alice, HEADER: first}).status_code == 200
    assert client.get(context, headers={**alice, HEADER: "build-agent"}).status_code == 200
    assert client.get(context, headers=alice).status_code == 200
    assert client.get(context, headers={**bob, HEADER: "other-owner"}).status_code == 200
    assert client.get(context, headers={HEADER: "fake"}).status_code == 401
    for invalid in ("%09hello", "%FF", "%GG", "x" * 65, quote("lbr_fake-token")):
        assert client.get(context, headers={**alice, HEADER: invalid}).status_code == 200
    assert (
        other_peer.get(
            context,
            headers={**alice, HEADER: quote(" 编程 Café "), "X-Forwarded-For": "198.51.100.99"},
        ).status_code
        == 200
    )
    body_label = "deployment-agent"
    commands = f"/api/bots/{api['bot_id']}/commands"
    assert client.post(commands, headers=alice, json={"command": "query", "agent_label": body_label}).status_code == 200

    assert agent.get(PREFIX + "/connections").status_code == 401
    response = browser.get(PREFIX + "/connections")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    connection = response.json()["connections"][0]
    personal = connection["agent_activity"]
    items = {row["agent_label"]: row for row in personal["items"]}
    assert personal["window_seconds"] == 3600 and personal["recent_active_agents"] == 3
    assert set(items) == {"编程 Café", "build-agent", body_label, None}
    assert items["编程 Café"]["last_observed_ip"] == "2001:db8::5"
    assert items["编程 Café"]["last_seen_at"].endswith("+00:00")
    assert all(label not in connection["instructions"] for label in ("build-agent", "2001:db8::5"))

    stats = f"/api/bots/{api['bot_id']}/agent-activity"
    for auth in (api["auth_header"], admin_header):
        ordinary = client.get(stats, headers=auth)
        assert ordinary.status_code == 200
        assert all("items" not in row["agent_activity"] for row in ordinary.json()["items"])
        assert "2001:db8::5" not in ordinary.text and body_label not in ordinary.text
    db_session.query(User).filter_by(username="adminuser").one().role = "super_admin"
    db_session.commit()
    assert "2001:db8::5" not in client.get(stats, headers=admin_header).text
    elevated = client.get(stats, params={"include_private": True}, headers=admin_header)
    assert "2001:db8::5" in elevated.text and "other-owner" in elevated.text
    platform_stats = "/api/admin/service-traffic"
    assert client.get(platform_stats, headers=api["auth_header"]).status_code == 403
    summary = client.get(platform_stats, headers=admin_header)
    assert summary.json()["agent_activity"] == {"window_seconds": 3600, "recent_active_agents": 4}
    assert "2001:db8::5" not in summary.text and body_label not in summary.text

    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.credential_expires_at = activity._utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert client.get(context, headers={**alice, HEADER: "must-not-track"}).status_code == 401
    assert browser.get(PREFIX + "/connections").status_code == 401
    response = client.get(platform_stats, headers=admin_header)
    assert response.json()["agent_activity"]["recent_active_agents"] == 1
    assert db_session.query(AgentLabelActivity).count() == 5


def test_metadata_is_bounded_concurrent_and_observations_do_not_move_backwards(api, db_session, tmp_path, monkeypatch):
    source = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    actor = ResourceActor(source.platform, source.external_user_id, source.bound_bot_id, source.id, source.public_id)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'concurrent-activity.sqlite3'}")
    with db_session.get_bind().begin() as connection, engine.begin() as destination:
        connection.connection.backup(destination.connection.driver_connection)
    monkeypatch.setattr(activity, "MAX_TRACKED_LABELS", 4)
    now = activity._utcnow()

    def record(label, address, observed):
        activity._record(engine, actor, "read", 200, label, address, observed)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda index: record("same", "203.0.113.9", now - timedelta(seconds=index)),
                    range(32),
                )
            )
        record("same", "203.0.113.10", now + timedelta(seconds=1))
        record("same", "192.0.2.7", now - timedelta(seconds=64))
        with Session(engine) as db:
            same = db.get(AgentLabelActivity, (actor.binding_record_id, "same"))
            assert same.last_observed_ip == "203.0.113.10" and same.last_seen_at == now + timedelta(seconds=1)
            assert db.get(activity.AgentActivity, actor.binding_record_id).read_requests == 34
        record("", "203.0.113.9", now)
        for index in range(16):
            record(str(index), "203.0.113.9", now + timedelta(seconds=index + 2))
        with Session(engine) as db:
            assert {row.agent_label for row in db.query(AgentLabelActivity)} == {"", "12", "13", "14", "15"}
            db.query(AgentLabelActivity).filter_by(agent_label="13").update(
                {"last_seen_at": now - activity.AGENT_ACTIVITY_RETENTION - timedelta(seconds=1)}
            )
            db.commit()
            assert all(
                item["agent_label"] != "13"
                for item in activity.binding_label_activity(db, actor.binding_record_id, include_items=True)["items"]
            )
            activity.cleanup_label_activity(db)
            assert db.query(AgentLabelActivity).count() == 4
            binding = db.get(ResourceBinding, actor.binding_record_id)
            binding.revoked_at = now
            db.commit()
        record("after-revocation", "203.0.113.9", now)
        with Session(engine) as db:
            assert db.get(AgentLabelActivity, (actor.binding_record_id, "after-revocation")) is None
            assert activity.recent_label_activity(db)["recent_active_agents"] == 0
    finally:
        engine.dispose()
