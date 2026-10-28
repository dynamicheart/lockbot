"""Round-3 regression probes: the Round-2 review schedules, adopted verbatim.

Covers recovery fresh-input (B23) / recovery error-status (B28), activation-failure cleanup (B26),
config-writer-vs-owner-held-derivation (B25), restart post-drain permission (B27), and the
canonical-root symlink-alias idempotency (AC-12).
"""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption, router
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend.test_scheduler_generation_exactness import finish, worker_call


@pytest.fixture
def independent_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(User(id=1, username="owner", email="o@example.test", password_hash="unused", role="admin"))
        db.add(User(id=2, username="next-owner", email="n@example.test", password_hash="unused", role="admin"))
        db.add(
            Bot(
                id=1,
                uuid="review",
                user_id=1,
                name="review",
                bot_type="NODE",
                webhook_url=encryption.encrypt("unused"),
                cluster_configs='["n1"]',
                status="stopped",
            )
        )
        db.commit()
    yield sessions
    engine.dispose()


def request():
    return Request(
        {"type": "http", "method": "POST", "path": "/api/bots/1/start", "headers": [], "client": ("127.0.0.1", 1)}
    )


def actor():
    return SimpleNamespace(id=1, username="owner", role="admin", max_running_bots=10)


def test_recovery_revalidates_deleted_candidate(independent_db, monkeypatch):
    import lockbot.backend.app.bots.manager as manager_module
    import lockbot.backend.app.main as main

    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "running"
        db.commit()
    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(manager_module, "BotInstance", MagicMock())
    monkeypatch.setattr(main, "SessionLocal", sessions)
    original = manager.transition
    first = [True]

    @contextlib.contextmanager
    def delete_before_owner(bot_id):
        if first[0]:
            first[0] = False

            # Recovery has selected its candidates, but has not acquired this bot's owner.
            def delete():
                with sessions() as db:
                    router.delete_bot(bot_id=bot_id, request=request(), user=actor(), db=db)

            thread, errors = worker_call(delete, "delete-before-recovery-owner")
            finish(thread, errors)
        with original(bot_id):
            yield

    monkeypatch.setattr(manager, "transition", delete_before_owner)
    main._reset_running_bots()
    with sessions() as db:
        row = db.get(Bot, 1)
        assert not manager.is_running(1), (row.is_deleted, row.status, row.pid, manager.is_running(1))


def test_recovery_error_candidate_commits_running(independent_db, monkeypatch):
    import lockbot.backend.app.bots.manager as manager_module
    import lockbot.backend.app.main as main

    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "error"
        db.commit()
    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(manager_module, "BotInstance", MagicMock())
    monkeypatch.setattr(main, "SessionLocal", sessions)
    main._reset_running_bots()
    with sessions() as db:
        row = db.get(Bot, 1)
        assert row.status == "running", (row.status, row.pid, manager.is_running(1))


def test_activation_failure_does_not_leave_admitted_unscheduled_generation(independent_db, monkeypatch):
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(manager._scheduler, "add", MagicMock(side_effect=RuntimeError("registration failed")))
    with independent_db() as db, pytest.raises(RuntimeError, match="registration failed"):
        router.start_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
    admitted = False
    try:
        with manager.admit(1):
            admitted = True
    except RuntimeError:
        pass
    with independent_db() as db:
        row = db.get(Bot, 1)
        assert not admitted, (row.status, row.pid, admitted, manager._scheduler._instances)


def test_config_edit_cannot_invalidate_owner_held_state_derivation(independent_db, monkeypatch, tmp_path):
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    original = router._validate_and_align_state
    calls = [0]

    def change_config_after_owned_derivation(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            # The second derivation is under the transition owner. Another request's config
            # writer nevertheless commits because update_bot does not participate.
            def edit():
                with independent_db() as db:
                    from lockbot.backend.app.bots.schemas import BotUpdate

                    router.update_bot(
                        bot_id=1, request=request(), body=BotUpdate(cluster_configs=["n2"]), user=actor(), db=db
                    )

            thread, errors = worker_call(edit, "config-edit-under-state-owner")
            finish(thread, errors)
        return result

    monkeypatch.setattr(router, "_validate_and_align_state", change_config_after_owned_derivation)
    with independent_db() as db:
        router.update_bot_state(bot_id=1, request=request(), state={"n1": {"status": "idle"}}, user=actor(), db=db)
    saved = json.loads((tmp_path / "bots" / "1" / "bot_state.json").read_text())["bot_state"]
    with independent_db() as db:
        configured = json.loads(db.get(Bot, 1).cluster_configs)
        assert set(saved) == set(configured), (configured, saved)


def test_transfer_during_restart_is_refused_owner_unchanged(independent_db, monkeypatch):
    """A transfer attempted mid-restart is refused; the restart keeps the original owner.

    Adapted from the Round-2 ``restart_rechecks_permission_after_drain`` schedule. That probe
    was written when ``transfer_bot_owner`` did NOT participate in the transition owner, so a
    concurrent transfer could commit a new owner mid-restart and the only defence was the
    restart's post-drain authz re-read (expected 403). R4 enrolled ``transfer_bot_owner`` in the
    owner, which makes the scenario STRONGER rather than merely re-checked: a transfer attempted
    while the restart holds the owner is refused up front with 503, so no new owner can commit
    mid-restart. The restart's post-drain re-check (router.py) remains as defence-in-depth. We
    assert the stronger landed invariant rather than the superseded 403.
    """
    manager = BotManager()
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    monkeypatch.setattr(router, "bot_manager", manager)
    manager.start_bot(1, {"BOT_TYPE": "NODE"})
    stop = manager.stop_bot

    def stop_then_transfer(bot_id):
        stop(bot_id)

        def transfer():
            with independent_db() as db:
                router.transfer_bot_owner(
                    bot_id=bot_id,
                    body={"username": "next-owner"},
                    request=request(),
                    user=SimpleNamespace(id=1, username="owner", role="super_admin"),
                    db=db,
                )

        thread, errors = worker_call(transfer, "transfer-during-restart")
        thread.join(5)
        assert not thread.is_alive()
        # The competing transfer is refused by the transition owner the restart holds.
        assert len(errors) == 1, errors
        assert getattr(errors[0], "status_code", None) == 503, errors[0]

    monkeypatch.setattr(manager, "stop_bot", stop_then_transfer)
    with independent_db() as db:
        response = router.restart_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
        status = response.status
    with independent_db() as db:
        row = db.get(Bot, 1)
        # Restart completes for the original owner; the refused transfer left ownership untouched.
        assert status == "running", (status, row.user_id)
        assert row.user_id == 1, row.user_id


def test_same_root_symlink_alias_is_idempotent(tmp_path):
    from lockbot.backend.app import state_ownership

    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    state_ownership.release_state_store_ownership()
    try:
        first = state_ownership.acquire_state_store_ownership(str(root))
        assert state_ownership.acquire_state_store_ownership(str(alias)) == first
    finally:
        state_ownership.release_state_store_ownership()
