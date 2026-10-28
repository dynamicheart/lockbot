"""Round-2 probes: the Round-1 review's identity/staging schedules (B23/B24/B25), adopted as
repository regression tests, plus the identity-discontinuity (AC-9) and canonical-root (AC-12)
increments and the positive restart-edit-during-drain schedule the review asked for.
"""

import contextlib
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.backend.app.resource_auth.models import ResourceBinding
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.test_generation_edit_protocol import create_bot
from backend.test_scheduler_generation_exactness import finish, worker_call


def test_review_autostart_revalidates_after_delete(client, admin_header, db_session, tmp_path, monkeypatch):
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    entering, resume = threading.Event(), threading.Event()
    original = manager.transition
    first = [True]

    @contextlib.contextmanager
    def pause_before_owner(bot_id):
        if first[0]:
            first[0] = False
            entering.set()
            assert resume.wait(10)
        with original(bot_id):
            yield

    monkeypatch.setattr(manager, "transition", pause_before_owner)
    results = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: results.append(
                client.post(
                    "/api/bots",
                    headers=admin_header,
                    json={
                        "name": "autostart-delete",
                        "bot_type": "NODE",
                        "webhook_url": "https://example.invalid/hook",
                        "cluster_configs": ["n1"],
                    },
                )
            ),
            "create-before-auto-owner",
        )
        assert entering.wait(10)
        row = db_session.query(Bot).filter(Bot.name == "autostart-delete").one()
        bot_id = row.id
        try:
            deleted = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        finally:
            resume.set()
            finish(thread, errors)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert not (deleted.status_code == 204 and manager.is_running(bot_id)), (
        deleted.status_code,
        results[0].status_code,
        row.is_deleted,
        row.status,
        manager.is_running(bot_id),
    )


def test_review_fatal_before_start_route_commit(
    client, admin_header, db_session, tmp_path, monkeypatch, session_factory
):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    original = manager.start_bot

    def publish_then_fail(*args, **kwargs):
        pid = original(*args, **kwargs)
        # A scheduler fatal completion runs after publication, before the route's status commit.
        callback = manager._make_fatal_error_handler(bot_id)
        thread, errors = worker_call(lambda: callback(bot_id), "fatal-during-start-tail")
        finish(thread, errors)
        return pid

    monkeypatch.setattr(manager, "start_bot", publish_then_fail)
    with (
        patch("lockbot.backend.app.bots.router.bot_manager", manager),
        patch("lockbot.backend.app.database.SessionLocal", session_factory),
    ):
        response = client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert not (manager.is_failed(bot_id) and row.status == "running"), (
        response.status_code,
        response.json(),
        row.status,
        manager.is_running(bot_id),
        manager.is_failed(bot_id),
    )


def test_review_offline_state_recomputes_validation_after_owner(
    client, admin_header, db_session, tmp_path, monkeypatch
):
    manager = BotManager()
    bot_id = create_bot(client, admin_header, tmp_path)
    entering, resume = threading.Event(), threading.Event()
    original = manager.transition
    first = [True]

    @contextlib.contextmanager
    def pause_before_owner(bid):
        if first[0]:
            first[0] = False
            entering.set()
            assert resume.wait(10)
        with original(bid):
            yield

    monkeypatch.setattr(manager, "transition", pause_before_owner)
    results = []
    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        thread, errors = worker_call(
            lambda: results.append(
                client.put(
                    f"/api/bots/{bot_id}/state",
                    headers=admin_header,
                    json={"n1": {"status": "idle", "current_users": [], "booking_list": []}},
                )
            ),
            "state-before-owner",
        )
        assert entering.wait(10)
        try:
            updated = client.put(f"/api/bots/{bot_id}", headers=admin_header, json={"cluster_configs": ["n2"]})
        finally:
            resume.set()
            finish(thread, errors)
    state_file = tmp_path / "bots" / str(bot_id) / "bot_state.json"
    saved = json.loads(state_file.read_text())["bot_state"]
    db_session.expire_all()
    configured = json.loads(db_session.get(Bot, bot_id).cluster_configs)
    assert set(saved) == set(configured), (updated.status_code, results[0].status_code, configured, saved)


def test_review_filebacked_fatal_before_start_status_commit(tmp_path, monkeypatch):
    """No StaticPool/client fixture: real route body and independent SQLite connections."""
    from types import SimpleNamespace

    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bots import encryption, router
    from starlette.requests import Request

    engine = create_engine(f"sqlite:///{tmp_path / 'fatal-start.db'}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(User(id=1, username="reviewer", email="r@example.test", password_hash="unused", role="super_admin"))
        db.add(
            Bot(
                id=1,
                uuid="probe",
                user_id=1,
                name="probe",
                bot_type="NODE",
                webhook_url=encryption.encrypt("unused"),
                cluster_configs='["n1"]',
                status="stopped",
            )
        )
        db.commit()
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", sessions)
    original = manager.start_bot

    def publish_then_fail(*args, **kwargs):
        result = original(*args, **kwargs)
        thread, errors = worker_call(lambda: manager._make_fatal_error_handler(1)(1), "filebacked-fatal-start")
        finish(thread, errors)
        return result

    monkeypatch.setattr(manager, "start_bot", publish_then_fail)
    monkeypatch.setattr(router, "bot_manager", manager)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/bots/1/start", "headers": [], "client": ("127.0.0.1", 1)}
    )
    user = SimpleNamespace(id=1, username="reviewer", role="super_admin")
    with sessions() as db:
        response = router.start_bot.__wrapped__(request=request, bot_id=1, user=user, db=db)
    with sessions() as db:
        row = db.get(Bot, 1)
        assert not (manager.is_failed(1) and row.status == "running"), (
            response.status,
            row.status,
            row.pid,
            manager.is_running(1),
            manager.is_failed(1),
        )
    engine.dispose()


def _add_confirmed_binding(db, bot_id, bot_uuid, digest="d" * 64, public_id="pub-1"):
    db.add(
        ResourceBinding(
            public_id=public_id,
            platform="infoflow",
            external_user_id="alice",
            bound_bot_id=bot_id,
            bound_bot_uuid=bot_uuid,
            credential_digest=digest,
        )
    )
    db.commit()


def test_delete_revokes_durable_credentials(client, admin_header, db_session, tmp_path):
    """AC-9: deleting a bot revokes its durable credentials under the same owner/transaction."""
    bot_id = create_bot(client, admin_header, tmp_path)
    bot_uuid = db_session.get(Bot, bot_id).uuid
    _add_confirmed_binding(db_session, bot_id, bot_uuid)

    resp = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
    assert resp.status_code in (200, 204), resp.text

    db_session.expire_all()
    rows = db_session.query(ResourceBinding).filter(ResourceBinding.bound_bot_id == bot_id).all()
    assert rows and all(r.revoked_at is not None for r in rows), [(r.public_id, r.revoked_at) for r in rows]


def test_state_store_rejects_different_root_after_ownership(tmp_path):
    """AC-12: a process cannot claim a second, different managed state root after acquiring one."""
    from lockbot.backend.app import state_ownership

    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    state_ownership.release_state_store_ownership()
    try:
        state_ownership.acquire_state_store_ownership(root_a)
        # Same root is idempotent.
        assert state_ownership.acquire_state_store_ownership(root_a)
        # A different root is refused.
        with pytest.raises(state_ownership.StateStoreOwnershipError):
            state_ownership.acquire_state_store_ownership(root_b)
    finally:
        state_ownership.release_state_store_ownership()


def test_restart_applies_edit_committed_during_drain(client, admin_header, db_session, tmp_path, monkeypatch):
    """Positive restart schedule: a language edit admitted before the drain commits DURING the
    drain, and the replacement generation is built from that post-drain config (not a pre-drain
    snapshot)."""
    from lockbot.backend.app.bots import router

    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    instance = MagicMock()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=instance):
        manager.start_bot(bot_id, router._build_config_dict(db_session.get(Bot, bot_id), db_session))
    db_session.get(Bot, bot_id).status = "running"
    db_session.commit()

    editing, resume, draining = threading.Event(), threading.Event(), threading.Event()

    def pause_set_val(key, value):
        # The live language edit has committed config_overrides=en to the DB and now pauses while
        # still holding its admission lease, so the restart's drain MUST block on it.
        editing.set()
        assert resume.wait(10)

    instance.bot.config.set_val.side_effect = pause_set_val

    orig_await = manager._await_drained_locked

    def await_signal(bot_id, deadline):
        draining.set()  # the restart has entered its drain (under _lock)
        return orig_await(bot_id, deadline)

    monkeypatch.setattr(manager, "_await_drained_locked", await_signal)

    captured = {}
    build = router._build_config_dict

    def capture_restart_config(bot, db):
        # The only _build_config_dict call after this patch is the restart's post-drain build.
        cfg = build(bot, db)
        captured["lang"] = cfg.get("LANGUAGE")
        return cfg

    with patch("lockbot.backend.app.bots.router.bot_manager", manager):
        edit_thread, edit_errors = worker_call(
            lambda: client.put(f"/api/bots/{bot_id}/language", headers=admin_header, json={"language": "en"}),
            "live-language-edit",
        )
        assert editing.wait(10)  # the edit holds its lease and is paused
        monkeypatch.setattr(router, "_build_config_dict", capture_restart_config)
        restart_thread, restart_errors = worker_call(
            lambda: client.post(f"/api/bots/{bot_id}/restart", headers=admin_header),
            "restart-during-edit",
        )
        # The restart reaches its drain and is BLOCKED there by the in-flight edit's lease.
        assert draining.wait(10)
        assert restart_thread.is_alive(), "restart did not block on the in-flight edit's lease"
        resume.set()  # release the edit; its lease drops, the drain completes
        finish(edit_thread, edit_errors)
        finish(restart_thread, restart_errors)
    db_session.expire_all()
    persisted = json.loads(db_session.get(Bot, bot_id).config_overrides)["LANGUAGE"]
    assert persisted == "en"
    assert captured.get("lang") == "en", captured
