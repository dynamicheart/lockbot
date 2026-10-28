"""Stage-3 identity & restore regressions.

- restore no longer bypasses the transition owner (closes the R4 ``restore is not fenced`` probe);
- a restore overwrite drains the live instance and revokes old credentials (identity discontinuity);
- the credential is re-verified inside the admission lease, closing the authenticate→admit window.
"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name

import threading
from datetime import datetime
from unittest.mock import MagicMock

from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.managed_paths import get_managed_paths
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.dependencies import ResourceActor, reverify_actor
from lockbot.backend.app.resource_auth.models import ResourceBinding
from starlette.requests import Request

from backend.test_lifecycle_recovery_canonical import independent_db  # noqa: F401
from backend.test_scheduler_generation_exactness import finish, worker_call


def _add_binding(db, *, bot_id=1, bot_uuid="review", token, public_id="pub-1"):
    db.add(
        ResourceBinding(
            public_id=public_id,
            platform="infoflow",
            external_user_id="u1",
            bound_bot_id=bot_id,
            bound_bot_uuid=bot_uuid,
            credential_digest=credentials.digest_credential(token),
        )
    )
    db.commit()


def _request_with_token(token):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/bots/1/resources",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1),
        }
    )


def test_restore_is_fenced_by_existing_owner(independent_db, monkeypatch, tmp_path):
    """A restore whose bot has a lifecycle transition owned by another thread must be fenced:
    it skips that bot with no durable effect, rather than overwriting it unserialized."""
    import lockbot.backend.app.bots.manager as manager_module
    from lockbot.backend.app.backup import service

    from backend.test_backup_restore import _build_backup_zip

    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    held, release = threading.Event(), threading.Event()

    def hold_owner():
        with manager.transition(1):
            held.set()
            release.wait(5)

    thread, errors = worker_call(hold_owner, "hold-owner")
    try:
        assert held.wait(5)
        archive = _build_backup_zip(
            bot_uuid="review",
            bot_name="restored-under-owner",
            state_data={"bot_state": {"node1": {"status": "idle"}}},
            bot_id=1,
        )
        with independent_db() as db:
            result = service.restore_apply(archive, None, None, ["review"], 1, db)
        with independent_db() as db:
            assert db.get(Bot, 1).name == "review"  # not overwritten
        assert result["overwritten"] == 0 and result["errors"]
        assert not get_managed_paths().state_file(1).exists()
    finally:
        release.set()
        finish(thread, errors)


def test_restore_overwrite_drains_live_and_revokes_credentials(independent_db, monkeypatch):
    """Overwriting an existing bot drains its live instance and revokes its durable credentials."""
    import lockbot.backend.app.bots.manager as manager_module
    from lockbot.backend.app.backup import service

    from backend.test_backup_restore import _build_backup_zip

    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(manager_module, "BotInstance", MagicMock())
    sessions = independent_db
    token = credentials.generate_credential()
    with sessions() as db:
        db.get(Bot, 1).resource_auth_enabled = True
        _add_binding(db, token=token)
    manager.start_bot(1, {"BOT_TYPE": "NODE"})
    assert manager.is_running(1)

    archive = _build_backup_zip(
        bot_uuid="review",
        bot_name="restored",
        state_data={"bot_state": {"node1": {"status": "idle"}}},
        bot_id=1,
    )
    with sessions() as db:
        result = service.restore_apply(archive, None, None, ["review"], 1, db)

    assert result["overwritten"] == 1 and result["errors"] == []
    assert not manager.is_running(1)  # live instance drained under the owner
    with sessions() as db:
        assert db.get(Bot, 1).name == "restored"
        binding = db.query(ResourceBinding).filter_by(public_id="pub-1").one()
        assert binding.revoked_at is not None  # old credential revoked (identity discontinuity)
    assert get_managed_paths().state_file(1).exists()


def test_reverify_actor_fails_after_revocation(independent_db):
    """reverify_actor (run inside the admission lease) rejects a credential that was revoked
    after authentication but before execution — closing the authenticate→admit window."""
    sessions = independent_db
    token = credentials.generate_credential()
    with sessions() as db:
        db.get(Bot, 1).resource_auth_enabled = True
        _add_binding(db, token=token)
    actor = ResourceActor(
        platform="infoflow", external_user_id="u1", bot_id=1, binding_record_id=1, binding_public_id="pub-1"
    )
    request = _request_with_token(token)
    with sessions() as db:
        assert reverify_actor(db, request, actor) is True
    with sessions() as db:
        db.query(ResourceBinding).filter_by(public_id="pub-1").one().revoked_at = datetime.utcnow()
        db.commit()
    with sessions() as db:
        assert reverify_actor(db, request, actor) is False
