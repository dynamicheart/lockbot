# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import sqlalchemy as sa
from fastapi.testclient import TestClient
from lockbot.backend.app.resource_api import activity
from lockbot.backend.app.resource_api.activity import AgentActivity, AgentLabelActivity
from lockbot.backend.app.resource_auth.dependencies import ResourceActor
from lockbot.backend.app.resource_auth.models import ResourceBinding
from sqlalchemy.orm import Session

from backend.test_resource_api_resources import api  # noqa: F401


def test_legacy_labels_and_peer_ip_are_not_recorded(client, api, db_session):
    headers = {**api["headers"], "X-Lockbot-Agent-Label": "legacy-header-label"}
    peer = TestClient(client.app, client=("2001:db8::5", 50000))
    assert peer.get("/api/agent/context", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/agent/commands",
            headers=headers,
            json={"command": "query", "agent_label": "legacy-body-label"},
        ).status_code
        == 200
    )
    assert db_session.query(AgentLabelActivity).count() == 0
    aggregate = db_session.query(AgentActivity).one()
    assert aggregate.read_requests == 2 and aggregate.last_status == 200


def test_aggregate_activity_is_atomic_and_does_not_move_backwards(api, db_session, tmp_path):
    binding = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    actor = ResourceActor(
        binding.platform, binding.external_user_id, binding.bound_bot_id, binding.id, binding.public_id
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'concurrent-activity.sqlite3'}")
    with db_session.get_bind().begin() as connection, engine.begin() as destination:
        connection.connection.backup(destination.connection.driver_connection)
    now = activity._utcnow()

    def record(index):
        activity._record(engine, actor, "read", 200, now - timedelta(seconds=index))

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(32)))
        activity._record(engine, actor, "write", 429, now + timedelta(seconds=1))
        record(63)
        with Session(engine) as db:
            aggregate = db.get(AgentActivity, actor.binding_record_id)
            assert aggregate.last_seen_at == now + timedelta(seconds=1)
            assert aggregate.last_status == 429
            assert (aggregate.read_requests, aggregate.write_requests, aggregate.limited_requests) == (33, 1, 1)
            assert db.query(AgentLabelActivity).count() == 0
    finally:
        engine.dispose()
