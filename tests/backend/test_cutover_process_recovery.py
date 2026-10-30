"""Real SIGKILL boundaries on fictional local state; never connect to a provider."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOT_UUID = "22222222-2222-4222-8222-222222222222"
OPERATION = "c" * 32


def _worker():
    import socket

    action, directory, boundary = sys.argv[1:]
    root = Path(directory)
    os.umask(0o077)
    os.environ["DATA_DIR"] = str(root / "data")
    os.environ["DATABASE_URL"] = "sqlite:///" + str(root / "database.sqlite")

    def deny_network(*_args, **_kwargs):
        (root / "network-attempt").write_text("blocked")
        raise AssertionError("Process recovery acceptance must never access a network")

    socket.socket.connect = deny_network
    import requests

    requests.sessions.Session.request = deny_network
    import lockbot.backend.app.config as config

    # A public synthetic fixture key, never a deployment key.
    config.ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    import lockbot.backend.app.main  # noqa: F401 -- register all additive tables, no lifespan
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.bot_migrations.coordinator import CutoverCoordinator
    from lockbot.backend.app.bot_migrations.models import BotMigration
    from lockbot.backend.app.bot_migrations.recovery import restore_fences
    from lockbot.backend.app.bots import encryption
    from lockbot.backend.app.bots.manager import BotManager, BotNotRunning, InstanceRetiring
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.bots.router import _build_config_dict
    from lockbot.backend.app.database import Base, engine
    from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
    from lockbot.backend.app.managed_paths import get_managed_paths
    from sqlalchemy.orm import Session, sessionmaker

    def stop_at(name):
        if name != boundary:
            return
        with (root / "ready.tmp").open("w") as target:
            json.dump({"boundary": name}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(root / "ready.tmp", root / "ready.json")
        while True:
            signal.pause()

    class CommitBoundarySession(Session):
        def commit(self):
            operation = next((item for item in self.identity_map.values() if isinstance(item, BotMigration)), None)
            phase = operation.phase if operation else None
            # The transaction has not reached SQLite; a fresh process must see the old phase.
            if phase and boundary == "commit_not_durable:" + phase:
                stop_at(boundary)
            super().commit()
            # SQLite committed, but the coordinator never receives a successful return.
            if phase and boundary == "commit_return_lost:" + phase:
                stop_at(boundary)

    factory = sessionmaker(bind=engine, class_=CommitBoundarySession, autoflush=False)
    Base.metadata.create_all(engine)
    manager = BotManager()

    def trace():
        platforms = [item.config.get_val("PLATFORM") for item in manager._bots.values()]
        with (root / "trace.jsonl").open("a") as target:
            target.write(json.dumps({"pid": os.getpid(), "platforms": platforms}) + "\n")

    start, stop = manager.start_bot, manager.stop_bot

    def tracked_start(*args, **kwargs):
        result = start(*args, **kwargs)
        trace()
        return result

    def tracked_stop(*args, **kwargs):
        result = stop(*args, **kwargs)
        trace()
        return result

    manager.start_bot, manager.stop_bot = tracked_start, tracked_stop
    if action == "seed":
        with factory() as db:
            db.add(
                User(
                    id=1,
                    username="fixture",
                    email="fixture@example.invalid",
                    password_hash="unused",
                    role="super_admin",
                )
            )
            bot = Bot(
                id=1,
                uuid=BOT_UUID,
                user_id=1,
                name="Fixture",
                bot_type="QUEUE",
                platform="Infoflow",
                identity_revision=0,
                group_id="1234",
                webhook_url=encryption.encrypt("https://example.invalid/hook"),
                aes_key=encryption.encrypt("FAKE_AES"),
                token=encryption.encrypt("FAKE_TOKEN"),
                cluster_configs='["node1"]',
                config_overrides="{}",
                api_key="FAKE_OP",
                status="running",
            )
            db.add(bot)
            db.add(
                FeishuApplication(
                    id=1,
                    app_id="cli_fixture",
                    name="Fixture app",
                    tenant_key="fixture_tenant",
                    bot_open_id="ou_fixture",
                    app_secret=encryption.encrypt("FAKE_APP"),
                    verification_token=encryption.encrypt(""),
                    encrypt_key=encryption.encrypt(""),
                    transport="websocket",
                )
            )
            db.commit()
            manager.start_bot(1, _build_config_dict(bot, db))
    else:
        restore_fences(factory, manager)
        with factory() as db:
            bot = db.get(Bot, 1)
            if bot.status == "running" and not manager.migration_fenced(1):
                manager.start_bot(1, _build_config_dict(bot, db))
    coordinator = CutoverCoordinator(
        factory,
        manager,
        clock=lambda: 1_800_000_000.0,
        maximum_clock_offset=0,
        target_validator=lambda *_args: True,
        fault=stop_at,
    )
    if action == "seed":
        coordinator.plan(1, 1, "oc_target", actor_id=1, operation_id=OPERATION)
        stop_at("planned")
        coordinator.resume(OPERATION, actor_id=1)
    elif action == "resume":
        result = coordinator.resume(OPERATION, actor_id=1)
        assert result["phase"] == "completed"
        assert coordinator.resume(OPERATION, actor_id=1) == result
    with factory() as db:
        bot, operation = db.get(Bot, 1), db.get(BotMigration, OPERATION)
        try:
            with manager.admit(1):
                write_admitted = True
        except (BotNotRunning, InstanceRetiring):
            write_admitted = False
        body = json.loads(get_managed_paths().state_file(1).read_text())
        answer = {
            "phase": operation.phase,
            "fenced": manager.migration_fenced(1),
            "write_admitted": write_admitted,
            "platform": bot.platform,
            "revision": bot.identity_revision,
            "uuid": bot.uuid,
            "route_count": db.query(FeishuGroupRoute).count(),
            "operation_count": db.query(BotMigration).count(),
            "op_key_cleared": bot.api_key is None,
            "state": body["bot_state"],
        }
        if action == "resume":
            answer["state_revision"] = body["lifecycle_outbox"]["identity_revision"]
    manager.shutdown_all()
    print(json.dumps(answer))


def _launch(root, action, boundary="none"):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO / "python"), "PYTHONHASHSEED": "0"}
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), action, str(root), boundary],
        env=env,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process):
    try:
        out, err = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("Isolated recovery process timed out")
    assert process.returncode == 0, err[-4000:]
    return json.loads(out.splitlines()[-1])


@pytest.mark.skipif(os.name != "posix", reason="Requires actual POSIX SIGKILL")
@pytest.mark.parametrize(
    ("boundary", "durable_phase", "switched"),
    [
        ("planned", "planned", False),
        ("freeze_committed", "freezing", False),
        ("checkpoint_published", "freezing", False),
        ("identity_switched", "switched", True),
        ("target_staged", "activating", True),
        ("commit_not_durable:freezing", "planned", False),
        ("commit_return_lost:switched", "switched", True),
    ],
)
def test_os_kill_recovers_only_same_operation_without_channel_overlap(tmp_path, boundary, durable_phase, switched):
    process = _launch(tmp_path, "seed", boundary)
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready.json").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not (tmp_path / "ready.json").exists():
            error = process.communicate(timeout=2)[1][-4000:] if process.poll() is not None else "marker timeout"
            pytest.fail(error)
        assert json.loads((tmp_path / "ready.json").read_text()) == {"boundary": boundary}
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    observation = _finish(_launch(tmp_path, "inspect"))
    assert observation["phase"] == durable_phase
    assert observation["fenced"] is (durable_phase != "planned")
    assert observation["write_admitted"] is (durable_phase == "planned")
    assert observation["revision"] == int(switched)
    assert observation["route_count"] == int(switched)
    recovered = _finish(_launch(tmp_path, "resume"))
    assert recovered["phase"] == "completed" and not recovered["fenced"] and recovered["write_admitted"]
    assert recovered["platform"] == "Feishu" and recovered["uuid"] == BOT_UUID
    assert recovered["revision"] == recovered["state_revision"] == recovered["route_count"] == 1
    assert recovered["operation_count"] == 1 and recovered["op_key_cleared"]
    assert recovered["state"] == observation["state"]
    entry = recovered["state"]["node1"]
    assert entry["status"] == "idle" and not entry["current_users"] and not entry["booking_list"]
    assert not (tmp_path / "network-attempt").exists()
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert traces and all(len(item["platforms"]) <= 1 for item in traces)


if __name__ == "__main__":
    _worker()
