"""Failure-boundary checks with a real file-backed DB and an ungraceful child exit."""

import json
import multiprocessing
import os

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.backup.service import restore_apply
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.backend.app.managed_paths import ManagedPaths
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.test_backup_restore import _build_backup_zip


def _crash_during_install(db_path, data_dir, archive):
    import lockbot.backend.app.config as config

    config.MANAGED_PATHS = ManagedPaths(data_dir)
    engine = create_engine("sqlite:///" + db_path)
    original = os.replace

    def crash(source, target, *args, **kwargs):
        if str(target).endswith("bot_state.json"):
            os._exit(91)
        return original(source, target, *args, **kwargs)

    os.replace = crash
    with Session(engine) as db:
        restore_apply(archive, None, None, ["crash-uuid"], 1, db)


def test_process_death_after_config_commit_keeps_persisted_fence(tmp_path):
    db_path = tmp_path / "durable.db"
    engine = create_engine("sqlite:///" + str(db_path))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, username="owner", email="owner@test.invalid", password_hash="unused", role="super_admin"))
        db.add(
            Bot(
                id=1,
                uuid="crash-uuid",
                user_id=1,
                name="original",
                bot_type="NODE",
                platform="Infoflow",
                webhook_url="",
                aes_key="",
                token="",
                cluster_configs='["old"]',
                config_overrides="{}",
                status="stopped",
            )
        )
        db.commit()
    state = tmp_path / "bots" / "1" / "bot_state.json"
    state.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "bot_state": {
                "old": {
                    "status": "exclusive",
                    "current_users": [{"user_id": "alice", "start_time": 1, "duration": 9999999999}],
                    "booking_list": [],
                }
            }
        }
    )
    state.write_text(original)
    archive = _build_backup_zip(bot_uuid="crash-uuid", state_data={"bot_state": {}})
    child = multiprocessing.get_context("spawn").Process(
        target=_crash_during_install, args=(str(db_path), str(tmp_path), archive)
    )
    child.start()
    child.join(15)
    assert not child.is_alive()
    assert child.exitcode == 91
    with Session(engine) as db:
        row = db.get(Bot, 1)
        assert row.status == "recovery_required"
        assert json.loads(row.cluster_configs) == ["node1"]
        assert row.identity_revision == 1
    assert state.read_text() == original
    engine.dispose()


def test_missing_state_cannot_clear_recovery(client, admin_header, db_session):
    response = client.post(
        "/api/bots",
        headers=admin_header,
        json={"name": "fenced", "bot_type": "NODE", "webhook_url": "", "cluster_configs": ["node1"]},
    )
    bot_id = response.json()["id"]
    row = db_session.get(Bot, bot_id)
    row.status = "recovery_required"
    db_session.commit()
    result = restore_apply(_build_backup_zip(bot_uuid=row.uuid), None, None, [row.uuid], row.user_id, db_session)
    assert result["restored"] == 0 and result["errors"]
    db_session.expire_all()
    assert db_session.get(Bot, bot_id).status == "recovery_required"


def test_uuid_migration_rejects_misnamed_nonunique_index(tmp_path, monkeypatch):
    import lockbot.backend.app.main as main

    engine = create_engine("sqlite:///" + str(tmp_path / "bad-index.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bots (id INTEGER PRIMARY KEY, uuid TEXT)"))
        conn.execute(text("CREATE INDEX ix_bots_uuid_unique ON bots(uuid)"))
    monkeypatch.setattr(main, "engine", engine)
    with pytest.raises(RuntimeError, match="Invalid ix_bots_uuid_unique"):
        main._migrate_bots_uuid()
    engine.dispose()


def test_global_chat_revocation_waits_for_same_principal_on_another_bot(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from lockbot.backend.app.resource_auth.authority import authority_guard
    from lockbot.backend.app.resource_auth.credentials import digest_credential
    from lockbot.backend.app.resource_auth.models import ResourceBinding
    from lockbot.backend.app.resource_auth.service import authenticate, handle_chat_binding
    from lockbot.core.node_bot import NodeBot
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///" + str(tmp_path / "revocation.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", sessions)
    token = "lbr_" + "x" * 43
    with sessions() as db:
        db.add(User(id=1, username="test", email="test@test.invalid", password_hash="unused", role="user"))
        for bot_id in [1, 2]:
            db.add(
                Bot(
                    id=bot_id,
                    uuid=f"uuid-{bot_id}",
                    user_id=1,
                    name=f"bot-{bot_id}",
                    bot_type="NODE",
                    platform="fakeim",
                    webhook_url="",
                    cluster_configs='["n1"]',
                )
            )
        db.add(
            ResourceBinding(
                public_id="test-public-id",
                platform="fakeim",
                external_user_id="alice",
                bound_bot_id=2,
                bound_bot_uuid="uuid-2",
                credential_digest=digest_credential(token),
            )
        )
        db.commit()
    bot = NodeBot(
        config_dict={
            "BOT_ID": "cross-bot",
            "BOT_TYPE": "NODE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["n1"],
            "WEBHOOK_URL": "",
        }
    )
    entered, release, revoking, revoked = (threading.Event() for _ in range(4))

    def admitted_operation():
        with authority_guard(2, platform="fakeim", external_user_id="alice"):
            entered.set()
            assert release.wait(3)
            bot.lock("alice", "lock n1 10m", acquisition_limit=1)

    def revoke():
        revoking.set()
        result = handle_chat_binding(
            platform="fakeim", external_user_id="alice", bot_id=2, action="unbind", argument=""
        )
        revoked.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        command = pool.submit(admitted_operation)
        assert entered.wait(3)
        revocation = pool.submit(revoke)
        assert revoking.wait(3)
        assert not revoked.wait(0.1), "same-bot unbind must wait for an already-authorized write"
        release.set()
        command.result(timeout=3)
        revocation.result(timeout=3)
    assert revoked.is_set()
    assert bot.state_snapshot()[1]["n1"]["current_users"][0]["user_id"] == "alice"
    with sessions() as db:
        assert authenticate(db, bot_id=2, bot_uuid="uuid-2", token=token) is None
    engine.dispose()
