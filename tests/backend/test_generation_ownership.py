"""Round 7/8 review probes for the remaining generation-ownership intervals, adopted as
repository regression tests:

- the scheduler sweep's admission is bound to the captured registration (B18);
- fatal DB completion is ownership-checked at the commit (B4);
- a stop cannot overtake an in-progress start's publication (B19 publication-consistency half).

The three offline-edit reservation probes (unpublished-start state/language edits) remain the
next mainline (B17 offline) and are intentionally not duplicated here.
"""

import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from lockbot.backend.app.bots.manager import BotManager, InstanceRetiring
from lockbot.backend.app.bots.models import Bot

from backend.test_generation_edit_protocol import create_bot
from backend.test_scheduler_generation_exactness import finish, start, worker_call


def test_replacement_between_currency_check_and_admission_is_not_swept(monkeypatch):
    manager = BotManager()
    old = start(manager)
    scheduler = manager._scheduler
    paused, resume = threading.Event(), threading.Event()
    original_admission = scheduler._admissions[1]

    @contextmanager
    def paused_admission(bot_id):
        # _run_check has already checked scheduler instance currency, but no lease exists.
        paused.set()
        assert resume.wait(5)
        with original_admission(bot_id) as lease:
            yield lease

    monkeypatch.setitem(scheduler._admissions, 1, paused_admission)
    thread, errors = worker_call(scheduler._fire_due, "currency-to-admission")
    assert paused.wait(5)
    manager.stop_bot(1)
    replacement = start(manager)
    resume.set()
    finish(thread, errors)
    old.bot._check_and_notify.assert_not_called()
    assert replacement.bot._check_and_notify.call_count == 0, (
        "old due work admitted and swept the replacement after its currency check"
    )


def test_due_batch_captures_instance_with_its_generation():
    manager = BotManager()
    first = start(manager)
    second = MagicMock()
    second.bot._check_and_notify.return_value = None
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=second):
        manager.start_bot(2, {"BOT_TYPE": "NODE"})
    paused, resume = threading.Event(), threading.Event()

    def first_sweep():
        paused.set()
        assert resume.wait(5)

    first.bot._check_and_notify.side_effect = first_sweep
    thread, errors = worker_call(manager._scheduler._fire_due, "due-batch")
    assert paused.wait(5)
    manager.stop_bot(2)
    replacement = MagicMock()
    replacement.bot._check_and_notify.return_value = None
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=replacement):
        manager.start_bot(2, {"BOT_TYPE": "NODE"})
    resume.set()
    finish(thread, errors)
    second.bot._check_and_notify.assert_not_called()
    assert replacement.bot._check_and_notify.call_count == 0, (
        "old due tuple looked up a replacement instance after another bot's sweep"
    )


def test_stop_cannot_overtake_start_publication(monkeypatch):
    manager = BotManager()
    paused, resume = threading.Event(), threading.Event()
    add = manager._scheduler.add

    def pause_before_add(*args, **kwargs):
        paused.set()
        assert resume.wait(5)
        return add(*args, **kwargs)

    monkeypatch.setattr(manager._scheduler, "add", pause_before_add)
    thread, errors = worker_call(lambda: start(manager), "publishing-start")
    assert paused.wait(5)
    # The publishing start owns the lifecycle transition; a stop is refused up front and cannot
    # act on a half-published instance (manager entry present, scheduler registration not made).
    with pytest.raises(InstanceRetiring):
        manager.stop_bot(1)
    resume.set()
    finish(thread, errors)
    # After the start completes, the two registries agree (no late-registration gap was exposed).
    assert (1 in manager._bots) == (1 in manager._scheduler._instances)
    assert manager._scheduler._instances.get(1) is manager.get_instance(1)


def test_fatal_db_ownership_survives_query_after_recheck(
    client, admin_header, tmp_path, db_session, session_factory, monkeypatch
):
    bot_id = create_bot(client, admin_header, tmp_path)
    manager = BotManager()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
        manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    row = db_session.get(Bot, bot_id)
    row.status = "running"
    db_session.commit()
    paused, resume = threading.Event(), threading.Event()

    def session_with_paused_query():
        session = session_factory()
        original_query = session.query

        def query(*args, **kwargs):
            # The callback's still_ours check has passed and its manager lock is released.
            paused.set()
            assert resume.wait(5)
            return original_query(*args, **kwargs)

        session.query = query
        return session

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_with_paused_query)
    callback = manager._make_fatal_error_handler(bot_id)
    thread, errors = worker_call(lambda: callback(bot_id), "fatal-after-recheck")
    assert paused.wait(5)
    manager.stop_bot(bot_id)
    replacement = MagicMock()
    with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=replacement):
        manager.start_bot(bot_id, {"BOT_TYPE": "NODE"})
    row.status = "running"
    db_session.commit()
    resume.set()
    finish(thread, errors)
    db_session.expire_all()
    assert manager.get_instance(bot_id) is replacement
    assert db_session.get(Bot, bot_id).status == "running", (
        "fatal DB completion still crossed stop/start after the ownership recheck"
    )
