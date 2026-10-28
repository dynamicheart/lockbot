"""Independent Round-3 review probes; copy into tests/backend to run."""

# ruff: noqa: F811
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lockbot.backend.app.bots import router
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.models import Bot
from lockbot.core.config import Config
from lockbot.core.io import _bot_dir

from backend.test_lifecycle_recovery_canonical import actor, independent_db, request  # noqa: F401
from backend.test_scheduler_generation_exactness import finish, worker_call


def test_config_writer_after_the_only_owned_derivation(independent_db, monkeypatch, tmp_path):
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    derive = router._validate_and_align_state
    writes = []

    def derive_then_edit(*args, **kwargs):
        aligned = derive(*args, **kwargs)

        def edit():
            from lockbot.backend.app.bots.schemas import BotUpdate

            with independent_db() as db:
                try:
                    router.update_bot(
                        bot_id=1, request=request(), body=BotUpdate(cluster_configs=["n2"]), user=actor(), db=db
                    )
                    writes.append(200)
                except Exception as exc:
                    writes.append(getattr(exc, "status_code", str(exc)))

        thread, errors = worker_call(edit, "config-after-only-derivation")
        finish(thread, errors)
        return aligned

    monkeypatch.setattr(router, "_validate_and_align_state", derive_then_edit)
    with independent_db() as db:
        router.update_bot_state(bot_id=1, request=request(), state={"n1": {"status": "idle"}}, user=actor(), db=db)
    saved = json.loads((tmp_path / "bots/1/bot_state.json").read_text())["bot_state"]
    with independent_db() as db:
        configured = json.loads(db.get(Bot, 1).cluster_configs)
    assert writes == [503] and set(saved) == set(configured), (writes, configured, saved)


def test_recovery_failure_compensates_before_releasing_owner(independent_db, monkeypatch):
    import lockbot.backend.app.bots.manager as manager_module
    import lockbot.backend.app.main as main

    sessions = independent_db
    with sessions() as db:
        db.get(Bot, 1).status = "running"
        db.commit()
    manager = BotManager()
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(
        manager_module, "BotInstance", MagicMock(side_effect=[RuntimeError("recovery construct failure"), MagicMock()])
    )
    original = manager.transition
    first = [True]
    starts = []

    @contextlib.contextmanager
    def start_after_failed_owner(bot_id):
        outer = first[0]
        first[0] = False
        try:
            with original(bot_id):
                yield
        except RuntimeError:
            if outer:

                def start():
                    with sessions() as db:
                        starts.append(
                            router.start_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db).status
                        )

                thread, errors = worker_call(start, "start-after-recovery-owner-release")
                finish(thread, errors)
            raise

    monkeypatch.setattr(manager, "transition", start_after_failed_owner)
    main._reset_running_bots()
    with sessions() as db:
        row = db.get(Bot, 1)
        observed = (starts, row.status, row.pid, manager.is_running(1))
        assert row.status == "running" and row.pid is not None and manager.is_running(1), observed


def test_transfer_is_refused_while_transition_owned(independent_db, monkeypatch):
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    statuses = []

    def transfer():
        with independent_db() as db:
            try:
                router.transfer_bot_owner(
                    bot_id=1,
                    body={"username": "next-owner"},
                    request=request(),
                    user=SimpleNamespace(id=1, username="owner", role="super_admin"),
                    db=db,
                )
                statuses.append(200)
            except Exception as exc:
                statuses.append(getattr(exc, "status_code", str(exc)))

    with manager.transition(1):
        thread, errors = worker_call(transfer, "transfer-under-owner")
        finish(thread, errors)
    with independent_db() as db:
        owner = db.get(Bot, 1).user_id
    assert statuses == [503] and owner == 1, (statuses, owner)


def test_no_environment_managed_state_matches_ownership_root(tmp_path):
    code = """
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from lockbot.backend.app import config
from lockbot.backend.app.bots.router import _build_config_dict, _get_bot_data_dir
from lockbot.core.config import Config
from lockbot.core.io import _bot_dir
# This path-resolution probe must not discover a real deployment's legacy occupancy.
config.LEGACY_CORE_ROOT = Path(sys.argv[1])
bot = SimpleNamespace(
    id=1, name="review", bot_type="NODE", platform="Infoflow", webhook_url="",
    aes_key="", token="", cluster_configs='["n1"]', config_overrides="{}",
)
from unittest.mock import patch
with patch("lockbot.core.io.os.makedirs"):
    core = os.path.join(_bot_dir(Config(_build_config_dict(bot))), "bot_state.json")
router = os.path.join(_get_bot_data_dir(bot), "bot_state.json")
print(json.dumps({"root": str(config.MANAGED_PATHS.root), "core": core, "router": router}))
assert os.path.realpath(core) == os.path.realpath(router)
assert os.path.realpath(core) == str(config.MANAGED_PATHS.root / "bots" / "1" / "bot_state.json")
"""
    env = dict(os.environ)
    env.pop("DATA_DIR", None)
    env["PYTHONPATH"] = str(Path("python").resolve())
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_managed_override_cannot_escape_locked_root(independent_db, tmp_path):
    outside = tmp_path / "outside-shared.json"
    with independent_db() as db:
        bot = db.get(Bot, 1)
        bot.config_overrides = json.dumps({"STATE_FILENAME": str(outside)})
        core_path = os.path.join(_bot_dir(Config(router._build_config_dict(bot, db))), "bot_state.json")
        router_path = os.path.join(router._get_bot_data_dir(bot), "bot_state.json")
    expected = str(tmp_path / "bots" / "1" / "bot_state.json")
    assert core_path == router_path == expected, (core_path, router_path, expected)


def test_restart_initial_commit_failure_compensates_status(independent_db, monkeypatch):
    manager = BotManager()
    monkeypatch.setattr(router, "bot_manager", manager)
    monkeypatch.setattr("lockbot.backend.app.bots.manager.BotInstance", MagicMock())
    manager.start_bot(1, {"BOT_TYPE": "NODE"})
    with independent_db() as db:
        row = db.get(Bot, 1)
        row.status = "running"
        row.pid = os.getpid()
        db.commit()
    with independent_db() as db:
        commit = db.commit
        first = [True]

        def fail_once():
            if first[0]:
                first[0] = False
                raise RuntimeError("initial status commit failed")
            return commit()

        monkeypatch.setattr(db, "commit", fail_once)
        with pytest.raises(RuntimeError, match="initial status commit failed"):
            router.restart_bot.__wrapped__(request=request(), bot_id=1, user=actor(), db=db)
    with independent_db() as db:
        row = db.get(Bot, 1)
        observed = (row.status, row.pid, manager.is_running(1), manager._staged, manager._scheduler._instances)
        assert row.status == "stopped" and row.pid is None and not manager.is_running(1), observed
