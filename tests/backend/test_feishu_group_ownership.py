"""A group route is exclusive across owners; Infoflow enrollment stays independent."""

# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.router import create_bot
from lockbot.backend.app.bots.schemas import BotCreate
from lockbot.backend.app.database import Base
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend.test_feishu_ingress import setup  # noqa: F401


def payload(name, application_id, platform="Feishu"):
    return {
        "name": name,
        "bot_type": "QUEUE",
        "platform": platform,
        "group_id": "oc_group0" if platform == "Feishu" else "123456",
        "feishu_application_id": application_id,
        "webhook_url": "",
        "cluster_configs": ["node_a"],
    }


@pytest.mark.parametrize("role,expected", [("user", 403), ("admin", 403), ("super_admin", 409)])
def test_other_owner_cannot_claim_an_enrolled_group(client, setup, admin_header, db_session, role, expected):
    application_id, _, _ = setup
    db_session.query(User).filter_by(username="adminuser").one().role = role
    db_session.commit()
    original = db_session.query(FeishuGroupRoute).filter_by(application_id=application_id, chat_id="oc_group0").one()
    original_bot_id = original.bot_id
    before = db_session.query(Bot).count()
    response = client.post("/api/bots", headers=admin_header, json=payload("other-owner", application_id))
    assert response.status_code == expected, response.text
    db_session.expire_all()
    assert db_session.query(Bot).count() == before  # The rejected creation cannot leave an orphan bot.
    routes = db_session.query(FeishuGroupRoute).filter_by(application_id=application_id, chat_id="oc_group0").all()
    assert len(routes) == 1 and routes[0].bot_id == original_bot_id


def test_existing_feishu_bot_cannot_move_to_another_group(client, setup, auth_header, db_session):
    application_id, bots, _ = setup
    bot_id = bots[0].config.get_val("BOT_ID")
    response = client.put(f"/api/bots/{bot_id}", headers=auth_header, json={"group_id": "oc_group1"})
    assert response.status_code == 422
    assert "re-enrollment" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Bot, bot_id).group_id == "oc_group0"
    route = db_session.query(FeishuGroupRoute).filter_by(bot_id=bot_id).one()
    assert (route.application_id, route.chat_id) == (application_id, "oc_group0")


def test_infoflow_same_group_remains_available_to_different_owners(
    client, setup, auth_header, admin_header, db_session
):
    application_id, _, _ = setup
    first = client.post("/api/bots", headers=auth_header, json=payload("first-infoflow", application_id, "Infoflow"))
    second = client.post("/api/bots", headers=admin_header, json=payload("second-infoflow", application_id, "Infoflow"))
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    db_session.expire_all()
    rows = db_session.query(Bot).filter(Bot.id.in_([first.json()["id"], second.json()["id"]])).all()
    assert len({row.user_id for row in rows}) == 2
    assert {row.group_id for row in rows} == {"123456"}
    assert db_session.query(FeishuGroupRoute).filter(FeishuGroupRoute.bot_id.in_([row.id for row in rows])).count() == 0


def test_concurrent_group_claims_commit_only_one_bot(tmp_path):
    # Separate file-backed connections exercise transaction visibility, unlike the shared
    # in-memory backend fixture. Both administrators start with no observed route.
    engine = create_engine(f"sqlite:///{tmp_path / 'group-claims.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    try:
        with sessions() as db:
            for index in range(2):
                db.add(
                    User(
                        username=f"owner-{index}",
                        email=f"owner-{index}@example.invalid",
                        password_hash="unused",
                        role="super_admin",
                    )
                )
            application = FeishuApplication(
                app_id="cli_concurrent",
                name="Test",
                tenant_key="tenant",
                bot_open_id="ou_bot",
                app_secret="",
                verification_token="",
                encrypt_key="",
                transport="websocket",
            )
            db.add(application)
            db.commit()
            application_id = application.id
        ready = Barrier(2)

        def claim(index):
            with sessions() as db:
                owner = db.query(User).filter_by(username=f"owner-{index}").one()
                assert db.query(FeishuGroupRoute).count() == 0
                ready.wait(timeout=5)
                request = Request({"type": "http", "client": ("127.0.0.1", 0)})
                try:
                    result = create_bot(request, BotCreate(**payload(f"contender-{index}", application_id)), owner, db)
                    return 201, result.id
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, range(2)))
        assert sorted(code for code, _ in outcomes) == [201, 409]
        with sessions() as db:
            bots = db.query(Bot).all()
            routes = db.query(FeishuGroupRoute).all()
            assert len(bots) == len(routes) == 1
            assert routes[0].bot_id == bots[0].id
            assert routes[0].chat_id == "oc_group0"
    finally:
        engine.dispose()
