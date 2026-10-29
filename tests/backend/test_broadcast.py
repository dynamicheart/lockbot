"""Broadcast preview, durable dispatch and permissions without external IM calls."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from lockbot.backend.app.auth.dependencies import create_access_token
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.encryption import encrypt
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.broadcast.models import BroadcastJob, BroadcastTarget, GroupActivity
from lockbot.backend.app.broadcast.service import process_one, record_group_activity, recover_interrupted


@pytest.fixture
def setup(db_session, auth_header):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    bot = Bot(
        user_id=user.id,
        uuid="broadcast-bot",
        name="Broadcast bot",
        bot_type="QUEUE",
        platform="Infoflow",
        webhook_url=encrypt("https://example.invalid/fixture"),
        cluster_configs='["n1"]',
        status="running",
    )
    db_session.add(bot)
    db_session.flush()
    record_group_activity(db_session, bot, "123")
    record_group_activity(db_session, bot, "456")
    db_session.commit()
    return bot, user, {"Authorization": "Bearer " + create_access_token(user.id)}


def preview(client, setup, **kwargs):
    return client.post(
        "/api/admin/broadcasts/preview", headers=setup[2], json={"message": "Try Agent resources", **kwargs}
    )


def test_requires_super_admin_all_routes(client, auth_header, admin_header):
    for headers, code in [({}, 401), (auth_header, 403), (admin_header, 403)]:
        for method, path, body in [
            ("get", "", None),
            ("get", "/fake", None),
            ("post", "/preview", {"message": "test"}),
            ("post", "/fake/send", None),
        ]:
            assert (
                client.request(method, "/api/admin/broadcasts" + path, headers=headers, json=body).status_code == code
            )


def test_preview_has_explicit_verified_recent_deduplicated_targets(client, setup, db_session):
    bot, user, _ = setup
    old = db_session.get(GroupActivity, (bot.id, "456"))
    old.last_seen_at = datetime.utcnow() - timedelta(days=8)
    other = Bot(
        user_id=user.id,
        uuid="other",
        name="Other",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url=encrypt("https://example.invalid/unused"),
        cluster_configs="[]",
        status="running",
    )
    db_session.add(other)
    db_session.flush()
    record_group_activity(db_session, other, "123")
    # Historical un-timestamped group IDs must not become broadcast recipients.
    bot.observed_group_ids = "999"
    db_session.commit()
    result = preview(client, setup, mention_all=True).json()
    assert result["status"] == "draft" and result["mention_all"] is True
    assert result["total"] == 1 and result["targets"][0]["group_id"] == "123"
    assert db_session.query(BroadcastTarget).filter_by(status="sending").count() == 0
    assert preview(client, setup, targets=["arbitrary"]).status_code == 422
    assert preview(client, setup, message="   ").status_code == 422
    assert preview(client, setup, recent_days=31).status_code == 422


def test_durable_send_is_idempotent_and_preserves_outcomes(client, setup, session_factory, db_session):
    job = preview(client, setup, mention_all=True).json()
    url = f"/api/admin/broadcasts/{job['id']}/send"
    assert client.post(url, headers=setup[2]).status_code == 202
    assert client.post(url, headers=setup[2]).json()["total"] == 2
    sent = []

    @contextmanager
    def admit(bot_id):
        def send(text, group, at_all):
            with session_factory() as db:
                assert db.query(BroadcastTarget).filter_by(group_id=group, status="sending").count() == 1
            sent.append((text, group, at_all))
            return ("accepted", "platform_accepted") if group == "123" else ("unknown", "delivery_uncertain")

        yield SimpleNamespace(bot=SimpleNamespace(adapter=SimpleNamespace(send_broadcast=send)))

    manager = SimpleNamespace(admit=admit)
    for _ in range(4):
        process_one(session_factory, manager)
    assert len(sent) == 2 and all(row[2] for row in sent)
    data = client.get(url.removesuffix("/send"), headers=setup[2]).json()
    assert data["status"] == "completed"
    assert data["counts"] == {"accepted": 1, "unknown": 1}
    client.post(url, headers=setup[2])
    assert process_one(session_factory, manager) is False


@pytest.mark.parametrize("change", ["identity", "permission", "restart"])
def test_stale_identity_revoked_permission_and_interrupted_send_never_resend(
    client, setup, db_session, session_factory, change
):
    job = preview(client, setup).json()
    client.post(f"/api/admin/broadcasts/{job['id']}/send", headers=setup[2])
    if change == "identity":
        setup[0].identity_revision += 1
    elif change == "permission":
        setup[1].role = "user"
    else:
        db_session.query(BroadcastTarget).update({"status": "sending"})
    db_session.commit()
    if change == "restart":
        recover_interrupted(db_session)
    sender = Mock(side_effect=AssertionError("Should not send"))

    @contextmanager
    def admit(bot_id):
        yield SimpleNamespace(bot=SimpleNamespace(adapter=SimpleNamespace(send_broadcast=sender)))

    for _ in range(4):
        process_one(session_factory, SimpleNamespace(admit=admit))
    assert not sender.called
    db_session.expire_all()
    assert db_session.get(BroadcastJob, job["id"]).status == "completed"
    assert {t.status for t in db_session.query(BroadcastTarget)} == (
        {"unknown"} if change == "restart" else {"skipped"}
    )


def test_expired_preview_and_no_recipients(client, setup, db_session):
    job = preview(client, setup).json()
    db_session.get(BroadcastJob, job["id"]).created_at = datetime.utcnow() - timedelta(minutes=11)
    db_session.commit()
    assert client.post(f"/api/admin/broadcasts/{job['id']}/send", headers=setup[2]).status_code == 409
    db_session.query(GroupActivity).delete()
    db_session.commit()
    job = preview(client, setup).json()
    assert job["total"] == 0
    assert client.post(f"/api/admin/broadcasts/{job['id']}/send", headers=setup[2]).status_code == 409
