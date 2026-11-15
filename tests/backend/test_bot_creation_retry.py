"""An ambiguous Console creation response can be retried without creating a second bot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot, BotCreationRequest
from lockbot.backend.app.bots.router import create_bot
from lockbot.backend.app.bots.schemas import BotCreate
from lockbot.backend.app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend.test_bots import _sample_bot


def test_retry_returns_existing_bot_and_conflicting_payload_is_rejected(client, auth_header, admin_header):
    key = str(uuid4())
    headers = {**auth_header, "Idempotency-Key": key}
    original = _sample_bot()
    first = client.post("/api/bots", headers=headers, json=original)
    replay = client.post("/api/bots", headers=headers, json=original)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]

    changed = client.post("/api/bots", headers=headers, json={**original, "token": "different"})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "bot_creation_payload_changed"
    assert len(client.get("/api/bots", headers=auth_header).json()) == 1

    other = client.post("/api/bots", headers={**admin_header, "Idempotency-Key": key}, json=original)
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]


def test_rejected_creation_does_not_claim_the_key(client, auth_header):
    headers = {**auth_header, "Idempotency-Key": str(uuid4())}
    invalid = client.post("/api/bots", headers=headers, json={**_sample_bot(), "bot_type": "INVALID"})
    assert invalid.status_code == 422
    created = client.post("/api/bots", headers=headers, json=_sample_bot())
    assert created.status_code == 201


def test_invalid_creation_key_value_is_not_echoed(client, auth_header):
    secret = "secret-value-" * 6
    invalid = client.post("/api/bots", headers={**auth_header, "Idempotency-Key": secret}, json=_sample_bot())
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_bot_creation_key"
    assert secret not in invalid.text


def test_deleted_result_cannot_be_recreated_by_a_retry(client, auth_header):
    headers = {**auth_header, "Idempotency-Key": str(uuid4())}
    first = client.post("/api/bots", headers=headers, json=_sample_bot())
    assert first.status_code == 201
    gone = client.delete(f"/api/bots/{first.json()['id']}", headers=auth_header)
    assert gone.is_success
    replay = client.post("/api/bots", headers=headers, json=_sample_bot())
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "bot_creation_result_unavailable"
    assert client.get("/api/bots", headers=auth_header).json() == []


def test_concurrent_retry_commits_one_bot_and_one_audit_event(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'create-retry.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    try:
        with sessions() as db:
            owner = User(
                username="owner",
                email="owner@example.invalid",
                password_hash="unused",
                role="user",
                max_running_bots=0,
            )
            db.add(owner)
            db.commit()
            owner_id = owner.id
        ready = Barrier(2)
        key = str(uuid4())

        def create():
            with sessions() as db:
                user = db.get(User, owner_id)
                ready.wait(timeout=5)
                request = Request({"type": "http"})
                return create_bot(request, BotCreate(**_sample_bot()), user, db, idempotency_key=key).id

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(create)
            second = pool.submit(create)
            assert first.result(timeout=10) == second.result(timeout=10)
        with sessions() as db:
            bot = db.query(Bot).one()
            assert db.query(BotCreationRequest).one().bot_id == bot.id
            assert db.query(AuditLog).filter_by(action="bot.create").count() == 1
    finally:
        engine.dispose()
