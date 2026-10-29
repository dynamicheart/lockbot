"""Display-only cache metadata never replaces actor identity or broadens log visibility."""

from collections import OrderedDict

import pytest
from lockbot.backend.app.bots.encryption import encrypt
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu import service
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute


@pytest.fixture
def display_bot(client, auth_header, db_session, monkeypatch):
    monkeypatch.setattr(service, "_clients", OrderedDict())
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "display-fixture",
            "bot_type": "NODE",
            "cluster_configs": {"n1": "gpu"},
            "webhook_url": "https://example.invalid/hook",
        },
    )
    assert created.status_code == 201, created.text
    bot = db_session.get(Bot, created.json()["id"])
    bot.platform, bot.group_id, bot.last_user_id = "Feishu", "oc_fixture", "ou_alice"
    app = FeishuApplication(
        app_id="cli_display_fixture",
        name="fixture",
        tenant_key="tenant",
        bot_open_id="ou_bot",
        app_secret=encrypt("fixture-secret"),
        verification_token=encrypt("fixture-verification"),
    )
    db_session.add(app)
    db_session.flush()
    db_session.add(FeishuGroupRoute(application_id=app.id, chat_id=bot.group_id, bot_id=bot.id))
    db_session.commit()
    return bot, app


def test_detail_and_logs_expose_only_cached_display_metadata_without_changing_original_text(
    client, auth_header, db_session, monkeypatch, display_bot
):
    from pathlib import Path

    import requests
    from lockbot.backend.app.bots.router import _get_log_file, _write_log

    bot, app = display_bot
    cached = service.application_client(app)
    cached._names["ou_alice"] = ("Example name", 10**12)
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("display read attempted contact access"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("display read attempted token access"))
    _write_log(bot.id, "[ou_alice] lock n1 private-task-text", category="command", source="chat")
    _write_log(bot.id, "system ou_alice must remain literal", category="system")
    original = Path(_get_log_file(bot.id)).read_text()
    detail = client.get(f"/api/bots/{bot.id}", headers=auth_header)
    assert detail.status_code == 200, detail.text
    assert detail.json()["last_user_id"] == "ou_alice"
    assert detail.json()["last_user_display_name"] == "Example name"
    assert detail.json()["feishu_application"] == {"id": app.id, "name": "fixture"}
    logs = client.get(f"/api/bots/{bot.id}/logs", headers=auth_header)
    assert logs.status_code == 200
    command = next(row for row in logs.json() if row["category"] == "command")
    assert command["message"] == "[ou_alice] lock"
    assert command["actor"] == {"platform": "feishu", "external_user_id": "ou_alice", "display_name": "Example name"}
    assert all("actor" not in row for row in logs.json() if row["category"] == "system")
    assert "private-task-text" not in logs.text
    assert Path(_get_log_file(bot.id)).read_text() == original


def test_cold_or_rotated_app_cache_falls_back_to_stable_id_without_constructing_client(
    client, auth_header, db_session, monkeypatch, display_bot
):
    bot, app = display_bot
    cached = service.application_client(app)
    cached._names["ou_alice"] = ("Old app name", 10**12)
    app.app_secret = encrypt("rotated-fixture-secret")
    db_session.commit()
    monkeypatch.setattr(service, "application_client", lambda *a: pytest.fail("cold display read created client"))
    detail = client.get(f"/api/bots/{bot.id}", headers=auth_header).json()
    assert detail["last_user_id"] == "ou_alice" and detail["last_user_display_name"] is None
    assert len(service._clients) == 1


def test_name_cache_is_scoped_to_current_routed_application(db_session, display_bot):
    bot, first_app = display_bot
    first_client = service.application_client(first_app)
    first_client._names["ou_alice"] = ("First app label", 10**12)
    second_app = FeishuApplication(
        app_id="cli_second_fixture",
        name="second",
        tenant_key="tenant2",
        bot_open_id="ou_bot2",
        app_secret=encrypt("second-secret"),
        verification_token=encrypt("verification"),
    )
    db_session.add(second_app)
    db_session.flush()
    second_client = service.application_client(second_app)
    second_client._names["ou_alice"] = ("Second app label", 10**12)
    route = db_session.query(FeishuGroupRoute).filter_by(bot_id=bot.id).one()
    route.application_id = second_app.id
    db_session.commit()
    assert service.cached_bot_client(bot, db_session).cached_user_name("ou_alice") == "Second app label"
    bot.group_id = "oc_wrong_route"
    assert service.cached_bot_client(bot, db_session) is None
