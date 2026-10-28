"""A shared link authorizes one read-only bot view, not an identity or platform session."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.backend.app.resource_api import view_links
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_api.view_links import BotViewWindow, view_link
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.test_resource_api_fakeim_e2e import fakeim_api  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_view_manager(api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.views.bot_manager", api["manager"])
    snapshot_cache.clear()
    yield
    snapshot_cache.clear()


def link(client, api, headers=None):
    response = client.get("/api/agent/context", headers=headers or api["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def headers(context):
    fragment = urlsplit(context["view_url"]).fragment
    value = fragment.rsplit("/", 1)[1] if fragment.startswith("/r/") else parse_qs(fragment.split("?", 1)[1])["view"][0]
    return {"Authorization": "Bearer " + value}


def test_lazy_creation_and_shared_24_hour_window(client, api, db_session, monkeypatch):
    clock = datetime(2030, 1, 1, 11, 23, 45)
    monkeypatch.setattr(view_links, "utcnow", lambda: clock)
    assert db_session.query(BotViewWindow).count() == 0
    assert client.get(f"/api/bot-views/{api['bot_id']}/state").status_code == 403
    assert db_session.query(BotViewWindow).count() == 0
    first = link(client, api)
    assert first["view_expires_at"] == "2030-01-02T11:23:45+00:00"
    assert f"/#/r/{api['bot_id']}/" in first["view_url"]
    assert len(headers(first)["Authorization"][7:]) == 22
    assert api["headers"]["Authorization"][7:] not in first["view_url"]
    other = link(client, api, api["other_headers"])
    assert other["view_url"] == first["view_url"]
    monkeypatch.setattr(view_links, "utcnow", lambda: clock + timedelta(hours=23))
    assert link(client, api)["view_url"] == first["view_url"]
    assert client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(first)).status_code == 200
    assert link(client, api)["view_expires_at"] == first["view_expires_at"]
    # The boundary is enforced without a scheduled refresh or a new link request.
    monkeypatch.setattr(view_links, "utcnow", lambda: clock + timedelta(hours=24))
    assert client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(first)).status_code == 403
    db_session.expire_all()
    assert db_session.query(BotViewWindow).one().expires_at == clock + timedelta(hours=24)
    renewed = link(client, api)
    assert renewed["view_url"] != first["view_url"]
    assert renewed["view_expires_at"] == "2030-01-03T11:23:45+00:00"
    assert client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(first)).status_code == 403
    assert client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(renewed)).status_code == 200


def test_missing_wrong_and_foreign_credentials_reveal_no_resource_data(client, api):
    context = link(client, api)
    for credential in [
        None,
        "lbv1.invalid.invalid",
        api["headers"]["Authorization"][7:],
        "lbk_operator",
        "eyJ.other.jwt",
    ]:
        auth = {"Authorization": "Bearer " + credential} if credential else {}
        existing = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=auth)
        missing = client.get("/api/bot-views/999999/state", headers=auth)
        assert existing.status_code == missing.status_code == 403
        assert existing.json() == missing.json()
        assert "resources" not in existing.json() and "name" not in existing.json()
        assert existing.headers["cache-control"] == "no-store"
    assert client.get("/api/bot-views/999999/state", headers=headers(context)).status_code == 403
    token = headers(context)["Authorization"][7:]
    # The code only belongs in the authorization header; query logging cannot expose it.
    assert client.get(f"/api/bot-views/{api['bot_id']}/state?view={token}").status_code == 403
    assert client.get("/api/agent/context", headers=headers(context)).status_code == 401
    command = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=headers(context), json={"command": "lock n1 1h"}
    )
    assert command.status_code == 401


@pytest.mark.parametrize("part", [1, 2])
def test_non_ascii_view_credentials_are_uniformly_rejected(client, api, db_session, part):
    link(client, api)
    window = db_session.get(BotViewWindow, api["bot_id"])
    token = ["lbv1", window.nonce, view_links._signature(db_session, window)]
    token[part] = "\xff" + token[part][1:]
    raw = ("Bearer " + ".".join(token)).encode("latin-1")
    response = client.get(f"/api/bot-views/{api['bot_id']}/state", headers={b"Authorization": raw})
    assert response.status_code == 403 and "resources" not in response.json()


@pytest.mark.parametrize("change", ["uuid", "revision", "delete"])
def test_identity_change_invalidates_links_without_refresh(client, api, db_session, change):
    context = link(client, api)
    bot = db_session.get(Bot, api["bot_id"])
    if change == "uuid":
        bot.uuid = "restored-bot-identity"
    elif change == "revision":
        bot.identity_revision += 1
    else:
        bot.is_deleted = True
    db_session.commit()
    response = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(context))
    assert response.status_code == 403 and "resources" not in response.json()


def test_other_bots_link_is_not_accepted(client, api, db_session):
    first = link(client, api)
    created = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "other-private-bot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": ["secret-node"],
        },
    ).json()
    other = db_session.get(Bot, created["id"])
    second = view_link(db_session, other, "http://testserver")
    assert client.get(f"/api/bot-views/{created['id']}/state", headers=headers(first)).status_code == 403
    assert client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(second)).status_code == 403


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_valid_link_shows_resource_state_and_purposes_without_binding_metadata(client, api):
    context = link(client, api)
    command = "lock n1 dev0 1h" if api["bot"].config.get_val("BOT_TYPE") == "DEVICE" else "lock n1 1h"
    result = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={
            "command": command,
            "agent_label": "build-agent",
            "purpose": "Develop the feature",
        },
    )
    assert result.status_code == 200
    response = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(context))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["health"] == "running" and payload["resources"]
    resource = payload["resources"][0]
    holder = (resource.get("devices", [{}])[0].get("holders") or resource["holders"])[0]
    assert holder["external_user_id"] == "alice"
    assert holder["usage"]["purpose"] == "Develop the feature"
    assert holder["usage"]["agent_label"] == "build-agent"
    assert "binding_id" not in holder["usage"] and "authorization_label" not in holder["usage"]
    assert "claim_command" not in response.text and "caller_queue_position" not in response.text
    assert payload["view_expires_at"] == context["view_expires_at"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert api["headers"]["Authorization"][7:] not in response.text


def test_queue_order_waiting_times_and_task_notes_are_shared(client, api, monkeypatch):
    # This fixture defaults to NODE; use its live state to exercise read DTO queue visibility.
    bot = api["bot"]
    import time

    node = bot.state.bot_state["n1"]
    node.update(
        status="exclusive",
        current_users=[{"user_id": "alice", "start_time": int(time.time()), "duration": 3600}],
        booking_list=[
            {"user_id": "bob", "duration": 1200, "is_notified": False},
            {"user_id": "carol", "duration": 600, "is_notified": False},
        ],
    )
    response = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(link(client, api)))
    queue = response.json()["resources"][0]["booking_list"]
    assert [row["external_user_id"] for row in queue] == ["bob", "carol"]
    assert queue[0]["position"] == 0 and queue[1]["position"] == 1
    assert queue[1]["estimated_wait_seconds"] - queue[0]["estimated_wait_seconds"] == 1200


def test_stopped_bot_remains_private_and_suspended_agent_cannot_get_new_link(client, api, db_session):
    context = link(client, api)
    api["manager"].stop_bot(api["bot_id"])
    bot = db_session.get(Bot, api["bot_id"])
    bot.status = "stopped"
    bot.resource_auth_enabled = False
    db_session.commit()
    suspended = link(client, api)
    assert suspended["view_url"] is None and suspended["view_expires_at"] is None
    response = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=headers(context))
    assert response.status_code == 200 and response.json()["health"] == "stopped"
    assert response.json()["resources"] == []


def test_group_query_appends_link_outside_state_lock(api):
    bot = api["bot"]

    def callback():
        assert bot._lock.acquire(blocking=False)
        bot._lock.release()
        return {"view_url": "https://approved.invalid/#/bot/1?view=fake-code"}

    bot.config.set_val("_BOT_VIEW_LINK", callback)
    reply = bot.query("alice")
    assert "approved.invalid/#/bot/1" in str(reply)


def test_signed_group_query_and_personal_context_share_the_same_link(client, fakeim_api, db_session, monkeypatch):
    from fake_adapter import VALID_SIGNATURE, encode_message
    from lockbot.backend.app.settings.models import SiteSetting

    bot_id, bot = fakeim_api["bot_id"], fakeim_api["bot"]
    row = db_session.get(Bot, bot_id)
    bot_uuid = row.uuid
    db_session.add(SiteSetting(key="platform_url", value="http://testserver"))
    db_session.commit()
    bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["view-test-group"])
    bot.config.set_val("_BOT_VIEW_LINK", lambda: view_links.group_view_link(bot_id, bot_uuid))
    monkeypatch.setattr("lockbot.backend.app.resource_api.views.bot_manager", fakeim_api["manager"])

    def message(text, conversation="view-test-group"):
        return client.post(
            f"/api/bots/webhook/{bot_id}",
            params={"signature": VALID_SIGNATURE},
            content=encode_message(text, actor="alice", conversation=conversation),
        )

    refused = message("", conversation="another-group")
    assert refused.status_code == 403
    assert db_session.query(BotViewWindow).count() == 0
    assert message("").status_code == 200
    sent = fakeim_api["bot"].adapter.sent[-1]
    url = next(part[1] for part in sent["envelope"]["segments"] if isinstance(part, tuple))
    assert url.startswith(f"http://testserver/#/r/{bot_id}/")

    issued = client.post("/api/resource-auth/bindings", json={}).json()
    assert message("bind " + issued["bind_code"]).status_code == 200
    context = client.get("/api/agent/context", headers={"Authorization": "Bearer " + issued["credential"]})
    assert context.status_code == 200
    assert context.json()["view_url"] == url
    state = client.get(f"/api/bot-views/{bot_id}/state", headers=headers(context.json()))
    assert state.status_code == 200 and state.json()["resources"]


def test_persisted_window_and_concurrent_first_use_are_stable(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "views.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Bot(
                id=1,
                user_id=1,
                uuid="test-stable-uuid",
                identity_revision=0,
                name="view",
                bot_type="NODE",
                platform="infoflow",
                webhook_url="",
                cluster_configs='["n1"]',
                is_deleted=False,
            )
        )
        db.commit()

    def obtain(_):
        with Session(engine) as db:
            return view_link(db, db.get(Bot, 1), "https://approved.invalid")

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            links = list(pool.map(obtain, range(10)))
        assert len({link["view_url"] for link in links}) == 1
        with Session(engine) as db:
            assert db.query(BotViewWindow).count() == 1
            assert view_link(db, db.get(Bot, 1), "https://approved.invalid") == links[0]
    finally:
        engine.dispose()


def test_short_and_published_long_links_share_the_existing_window(client, api, db_session):
    import base64

    context = link(client, api)
    window = db_session.get(BotViewWindow, api["bot_id"])
    original = (window.nonce, window.issued_at, window.expires_at)
    short = headers(context)["Authorization"][7:]
    assert len(base64.urlsafe_b64decode(short + "==")) == 16
    legacy = "lbv1." + window.nonce + "." + view_links._signature(db_session, window)
    for token in [short, legacy]:
        result = client.get(f"/api/bot-views/{api['bot_id']}/state", headers={"Authorization": "Bearer " + token})
        assert result.status_code == 200
        assert result.json()["view_path"] == f"/r/{api['bot_id']}/{short}"
        assert result.json()["view_expires_at"] == context["view_expires_at"]
    db_session.expire_all()
    window = db_session.get(BotViewWindow, api["bot_id"])
    assert (window.nonce, window.issued_at, window.expires_at) == original


def test_short_link_tampering_and_non_ascii_do_not_reveal_resources(client, api):
    code = headers(link(client, api))["Authorization"][7:]
    for index in range(len(code)):
        replacement = "A" if code[index] != "A" else "B"
        changed = code[:index] + replacement + code[index + 1 :]
        result = client.get(f"/api/bot-views/{api['bot_id']}/state", headers={"Authorization": "Bearer " + changed})
        assert result.status_code == 403 and "resources" not in result.json()
    result = client.get(
        f"/api/bot-views/{api['bot_id']}/state", headers={b"Authorization": b"Bearer \xff" + code[1:].encode()}
    )
    assert result.status_code == 403


def test_signing_secret_rotation_invalidates_both_code_formats(client, api, db_session, monkeypatch):
    context = link(client, api)
    window = db_session.get(BotViewWindow, api["bot_id"])
    legacy = "lbv1." + window.nonce + "." + view_links._signature(db_session, window)
    monkeypatch.setattr(view_links.config, "JWT_SECRET", "new-test-signing-secret")
    for auth in [headers(context), {"Authorization": "Bearer " + legacy}]:
        result = client.get(f"/api/bot-views/{api['bot_id']}/state", headers=auth)
        assert result.status_code == 403
