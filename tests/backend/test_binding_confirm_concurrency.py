"""AC-2 / task26a: concurrent confirmation of one code yields exactly one durable row.

The in-memory suite DB shares a single connection (StaticPool), which serialises writes and
can hide a real race. This test uses a *file-backed* SQLite database with an independent
connection per thread, so the concurrency is genuine. The single-use pending claim is what
guarantees exactly one confirmation reaches the durable insert.
"""

import threading

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.resource_auth.pending import pending_store
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PLATFORM = "infoflow"
ACTOR = "alice"
CODE = "CONFIRM1"
THREADS = 16


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A file-backed SQLite with one bind-enabled bot, wired into the chat hook."""
    import lockbot.backend.app.audit.models  # noqa: F401
    import lockbot.backend.app.bots.models  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with Session() as s:
        s.add(User(username="owner", email="o@test.com", password_hash="x", role="user"))
        s.flush()
        s.add(
            Bot(
                id=1,
                user_id=1,
                name="concbot",
                bot_type="NODE",
                webhook_url="x",
                cluster_configs="{}",
                resource_auth_enabled=True,
            )
        )
        s.commit()

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", Session)
    return Session


def test_concurrent_confirmations_write_exactly_one_row(file_db):
    pending_store.create(
        binding_id=credentials.generate_public_id(),
        code_digest=credentials.digest_bind_code(CODE),
        credential_digest=credentials.digest_credential("lbr_x"),
        source="",
        label="agent",
        window_seconds=600,
        max_total=1000,
        max_per_source=1000,
    )

    replies: list[str] = []
    replies_lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker():
        barrier.wait()
        reply = service.handle_chat_binding(
            platform=PLATFORM, external_user_id=ACTOR, bot_id=1, action="bind", argument=CODE
        )
        with replies_lock:
            replies.append(reply)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with file_db() as s:
        rows = s.query(ResourceBinding).all()
    assert len(rows) == 1, "exactly one durable binding must exist"
    assert rows[0].bound_bot_id == 1
    assert rows[0].external_user_id == ACTOR

    confirmed = [r for r in replies if "【Agent授权成功】" in r]
    assert len(confirmed) == 1, "exactly one caller may be told the binding was confirmed"


def _seed_pending(code=CODE):
    record = pending_store.create(
        binding_id=credentials.generate_public_id(),
        code_digest=credentials.digest_bind_code(code),
        credential_digest=credentials.digest_credential("lbr_x"),
        source="",
        label="agent",
        window_seconds=600,
        max_total=1000,
        max_per_source=1000,
    )
    return record.binding_id


def _confirm():
    return service.handle_chat_binding(
        platform=PLATFORM, external_user_id=ACTOR, bot_id=1, action="bind", argument=CODE
    )


@pytest.mark.parametrize("failing_method", ["commit", "flush"])
def test_durable_write_failure_restores_the_code_for_retry(file_db, monkeypatch, failing_method):
    """B13: a flush/commit failure after the single-use reserve must not strand the code."""
    binding_id = _seed_pending()

    calls = {"n": 0}

    def flaky_factory():
        session = file_db()
        if calls["n"] == 0:
            calls["n"] += 1

            def boom(*_a, **_k):
                raise RuntimeError(f"{failing_method} boom")

            monkeypatch.setattr(session, failing_method, boom)
        return session

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", flaky_factory)

    # The first confirmation hits the injected durable-write failure and propagates it.
    with pytest.raises(RuntimeError):
        _confirm()

    # No durable row was written, and the single-use code was handed back, not consumed.
    with file_db() as s:
        assert s.query(ResourceBinding).count() == 0
    assert pending_store.get(binding_id) is not None, "the reserved code must be restored on failure"

    # A genuine retry of the same code now succeeds, exactly once.
    reply = _confirm()
    assert "【Agent授权成功】" in reply
    with file_db() as s:
        rows = s.query(ResourceBinding).all()
    assert len(rows) == 1
    assert rows[0].public_id == binding_id

    # And it is now single-use: a third attempt confirms nothing further.
    second = _confirm()
    assert "【Agent授权成功】" not in second
    with file_db() as s:
        assert s.query(ResourceBinding).count() == 1
