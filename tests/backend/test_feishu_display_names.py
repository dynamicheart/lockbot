"""Authorized batch display names cannot become an arbitrary-ID contact oracle."""
# ruff: noqa: F811

from types import SimpleNamespace

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.feishu import display_names, service
from lockbot.core.platforms.feishu import FeishuClient

from backend.test_feishu_display_metadata import display_bot  # noqa: F401


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


def test_name_jobs_have_global_capacity_and_per_application_singleflight():
    executor = DeferredExecutor()
    jobs = display_names.NameBatches(executor)
    for index in range(10):
        jobs.submit(FeishuClient(f"cli_{index}", "secret"), ["ou_one"])
    assert len(executor.calls) == len(jobs.active) == 8
    jobs.submit(FeishuClient("cli_0", "rotated-secret"), ["ou_two"])
    assert len(executor.calls) == 8
    assert jobs.pending(SimpleNamespace(app_id="cli_0")) == {"ou_one"}
    key, client, identities = executor.calls[0][1]
    client.warm_user_names = lambda values: None
    jobs._run(key, client, identities)
    assert len(jobs.active) == 7


def test_refresh_derives_only_current_bot_actors_and_get_never_schedules(
    client, auth_header, db_session, display_bot, monkeypatch
):
    bot, app = display_bot
    executor = DeferredExecutor()
    monkeypatch.setattr(display_names, "batches", display_names.NameBatches(executor))
    endpoint = f"/api/feishu/bots/{bot.id}/display-names"
    get = client.get(endpoint, headers=auth_header)
    assert get.status_code == 200 and not executor.calls
    assert client.post(endpoint, headers=auth_header, json={"user_ids": ["ou_foreign"]}).status_code == 422
    refresh = client.post(endpoint, headers=auth_header)
    assert refresh.status_code == 200 and refresh.headers["cache-control"] == "no-store"
    assert refresh.json()["status"] == "pending" and len(executor.calls) == 1
    assert executor.calls[0][1][2] == ["ou_alice"]
    client.get(endpoint, headers=auth_header)
    assert len(executor.calls) == 1
    cached = service.application_client(app)
    cached._names["ou_alice"] = ("Example", 10**12)
    ready = client.get(endpoint, headers=auth_header).json()
    assert ready["status"] == "ready" and ready["names"] == {"ou_alice": "Example"}
    assert client.get(endpoint).status_code == 401
    operator = db_session.query(User).filter_by(username="testuser").one()
    bot.user_id = operator.id + 999
    db_session.commit()
    assert client.post(endpoint, headers=auth_header).status_code == 404
    assert len(executor.calls) == 1


@pytest.mark.parametrize("change", ["route", "token", "tenant", "bot_identity", "transport", "route_identity"])
def test_scope_and_token_changes_are_fenced_before_returning_names(
    client, auth_header, db_session, display_bot, monkeypatch, change
):
    bot, app = display_bot
    cached = service.application_client(app)
    cached._names["ou_alice"] = ("Hidden after route change", 10**12)

    def change_route(*args):
        if change == "route":
            bot.group_id = "oc_another_route"
        elif change == "token":
            db_session.query(User).filter_by(username="testuser").one().token_version += 1
        elif change == "tenant":
            app.tenant_key = "another-tenant"
        elif change == "bot_identity":
            app.bot_open_id = "ou_another_bot"
        elif change == "transport":
            app.transport = "websocket"
        else:
            from lockbot.backend.app.feishu.models import FeishuGroupRoute

            db_session.query(FeishuGroupRoute).filter_by(bot_id=bot.id).one().id += 100
        db_session.commit()

    monkeypatch.setattr(display_names.batches, "submit", change_route)
    response = client.post(f"/api/feishu/bots/{bot.id}/display-names", headers=auth_header)
    assert response.status_code == (401 if change == "token" else 409)
    assert "Hidden after route change" not in response.text


def test_batch_dispatch_occurs_after_releasing_the_database_transaction(
    client, auth_header, db_session, display_bot, monkeypatch
):
    bot, _ = display_bot
    original, sessions = display_names._scope, []

    def capture(db, *args):
        result = original(db, *args)
        sessions.append(db)
        return result

    def dispatch(*args):
        assert sessions and not sessions[0].in_transaction()

    monkeypatch.setattr(display_names, "_scope", capture)
    monkeypatch.setattr(display_names.batches, "submit", dispatch)
    response = client.post(f"/api/feishu/bots/{bot.id}/display-names", headers=auth_header)
    assert response.status_code == 200


def test_actor_selection_is_bounded_and_excludes_other_identity_revisions(db_session, display_bot, monkeypatch):
    import hashlib

    from lockbot.backend.app.resource_auth.models import ResourceBinding

    bot, _ = display_bot
    for index in range(40):
        db_session.add(
            ResourceBinding(
                public_id=f"fixture-{index}",
                platform="feishu",
                external_user_id=f"ou_user{index}",
                bound_bot_id=bot.id,
                bound_bot_uuid=bot.uuid,
                bound_identity_revision=bot.identity_revision,
                credential_digest=hashlib.sha256(str(index).encode()).hexdigest(),
                label="private-binding-label",
            )
        )
    db_session.add(
        ResourceBinding(
            public_id="stale-fixture",
            platform="feishu",
            external_user_id="ou_stale_identity",
            bound_bot_id=bot.id,
            bound_bot_uuid="another-bot-uuid",
            bound_identity_revision=bot.identity_revision,
            credential_digest=hashlib.sha256(b"stale").hexdigest(),
        )
    )
    db_session.commit()
    monkeypatch.setattr(display_names, "_log_actors", lambda *args: ([], False))
    identities, truncated = display_names._actors(db_session, bot, 1)
    assert len(identities) == 32 and truncated
    assert identities[0] == "ou_alice" and "ou_stale_identity" not in identities
    assert "private-binding-label" not in str(identities)


def test_command_actor_derivation_never_scans_private_command_body_or_system_prose(display_bot):
    from lockbot.backend.app.bots.router import _write_log

    bot, _ = display_bot
    _write_log(bot.id, "[ou_visible] alias n1 ou_private_body", category="command")
    _write_log(bot.id, "[ou_system_prose] lock n1", category="system")
    identities, incomplete = display_names._log_actors(bot.id, 1)
    assert identities == ["ou_visible"] and not incomplete


def test_job_completion_between_status_snapshots_returns_ready_labels(client, auth_header, display_bot, monkeypatch):
    bot, _ = display_bot

    def completed(platform_client):
        platform_client._names["ou_alice"] = ("Completed name", 10**12)
        return set()

    monkeypatch.setattr(display_names.batches, "pending", completed)
    response = client.get(f"/api/feishu/bots/{bot.id}/display-names", headers=auth_header)
    assert response.status_code == 200
    assert response.json()["status"] == "ready" and response.json()["names"] == {"ou_alice": "Completed name"}
