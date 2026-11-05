"""A key can discover its principal's bindings without acquiring their authority."""

# ruff: noqa: F811
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_api_resources import api  # noqa: F401


def second_binding(client, api):
    bot = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "second group",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"node": "gpu"},
        },
    ).json()
    issued = client.post("/api/resource-auth/bindings", json={"label": "private agent label"}).json()
    service.handle_chat_binding(
        platform="infoflow",
        external_user_id="alice",
        bot_id=bot["id"],
        action="bind",
        argument=issued["bind_code"],
    )
    client.put(f"/api/bots/{bot['id']}/resource-auth", headers=api["auth_header"], json={"enabled": False})
    return bot, issued


def test_directory_discovers_same_principal_without_secrets_or_cross_bot_authority(client, api):
    bot, issued = second_binding(client, api)
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    assert "own_binding_directory" in context["server"]["capabilities"]
    response = client.get(context["links"]["bindings"], headers=api["headers"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["actor"] == {"platform": "infoflow", "external_user_id": "alice"}
    assert data["discovery_only"] is True and data["credential_retrieval_supported"] is True
    assert [b["bot"]["id"] for b in data["bindings"]] == [api["bot_id"], bot["id"]]
    assert [b["current"] for b in data["bindings"]] == [True, False]
    assert data["bindings"][1]["binding_id"] == issued["binding_id"]
    assert data["bindings"][1]["agent_access_enabled"] is False
    for secret in (
        issued["credential"],
        issued["bind_code"],
        api["headers"]["Authorization"][7:],
        "credential_digest",
        "private agent label",
        "bob",
    ):
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "Authorization" in response.headers["vary"]
    assert client.get(f"/api/bots/{bot['id']}/resources", headers=api["headers"]).status_code == 401
    assert (
        client.get("/api/agent/bindings", headers=api["other_headers"]).json()["bindings"][0]["bot"]["id"]
        == api["bot_id"]
    )
    assert len(client.get("/api/agent/bindings", headers=api["other_headers"]).json()["bindings"]) == 1


@pytest.mark.parametrize("change", ["revoked", "expired", "uuid", "revision", "deleted", "platform", "subject"])
def test_directory_excludes_invalid_or_unrelated_bindings(client, api, db_session, change):
    bot, issued = second_binding(client, api)
    row = db_session.query(ResourceBinding).filter_by(public_id=issued["binding_id"]).one()
    model = db_session.get(Bot, bot["id"])
    if change == "revoked":
        row.revoked_at = datetime.utcnow()
    elif change == "expired":
        row.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "uuid":
        model.uuid = "replacement-identity"
    elif change == "revision":
        model.identity_revision += 1
    elif change == "deleted":
        model.is_deleted = True
    elif change == "platform":
        row.platform = "feishu"
    else:
        row.external_user_id = "other-user"
    db_session.commit()
    data = client.get("/api/agent/bindings", headers=api["headers"]).json()
    assert len(data["bindings"]) == 1 and data["bindings"][0]["current"] is True
    recovered = client.post("/api/agent/bindings/recover", headers=api["headers"]).json()
    assert len(recovered["bindings"]) == 1
    assert issued["credential"] not in str(recovered)


@pytest.mark.parametrize("token", ["", "lbr_unknown", "lbk_operator", "foreign.jwt"])
def test_directory_requires_personal_credential(client, token):
    headers = {"Authorization": "Bearer " + token} if token else {}
    assert client.get("/api/agent/bindings", headers=headers).status_code == 401
    assert client.post("/api/agent/bindings/recover", headers=headers).status_code == 401


def test_directory_rejects_revoked_source_and_shares_read_budget(client, api, db_session, monkeypatch):
    from lockbot.backend.app.resource_api.traffic import credential_budget

    monkeypatch.setattr(credential_budget, "read_limit", 1)
    assert client.get("/api/agent/context", headers=api["headers"]).status_code == 200
    assert client.get("/api/agent/bindings", headers=api["headers"]).status_code == 429
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.revoked_at = datetime.utcnow()
    db_session.commit()
    assert client.get("/api/agent/bindings", headers=api["headers"]).status_code == 401
    assert client.post("/api/agent/bindings/recover", headers=api["headers"]).status_code == 401


@pytest.mark.parametrize("change", ["expired", "uuid", "revision", "deleted"])
def test_recovery_rejects_invalid_source_without_backfilling(client, api, db_session, change):
    binding = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    bot = db_session.get(Bot, api["bot_id"])
    binding.credential_encrypted = None
    if change == "expired":
        binding.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif change == "uuid":
        bot.uuid = "replacement"
    elif change == "revision":
        bot.identity_revision += 1
    else:
        bot.is_deleted = True
    db_session.commit()
    assert client.post("/api/agent/bindings/recover", headers=api["headers"]).status_code == 401
    db_session.expire_all()
    assert binding.credential_encrypted is None


def test_directory_is_in_public_schema(client):
    paths = client.get("/api/public-openapi.json").json()["paths"]
    assert paths["/api/agent/bindings"]["get"]["security"] == [{"AgentKey": []}]
    assert paths["/api/agent/bindings/recover"]["post"]["security"] == [{"AgentKey": []}]


def test_recovery_returns_distinct_group_keys_only_for_authenticated_identity(client, api, db_session):
    bot, issued = second_binding(client, api)
    response = client.post("/api/agent/bindings/recover", headers=api["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "Authorization" in response.headers["vary"]
    data = response.json()
    assert data["actor"]["external_user_id"] == "alice"
    keys = {entry["bot"]["id"]: entry["credential"] for entry in data["bindings"]}
    assert keys == {api["bot_id"]: api["headers"]["Authorization"][7:], bot["id"]: issued["credential"]}
    for entry in data["bindings"]:
        assert entry["credential_recoverable"] is True
        context = client.get("/api/agent/context", headers={"Authorization": "Bearer " + entry["credential"]}).json()
        assert context["bot"]["uuid"] == entry["bot"]["uuid"]
    rows = db_session.query(ResourceBinding).all()
    assert all(row.credential_encrypted and row.credential_encrypted != keys.get(row.bound_bot_id) for row in rows)
    other = client.post(
        "/api/agent/bindings/recover", headers=api["other_headers"], json={"external_user_id": "alice"}
    ).json()
    assert other["actor"]["external_user_id"] != "alice"
    assert len(other["bindings"]) == 1
    assert other["bindings"][0]["credential"] == api["other_headers"]["Authorization"][7:]


def test_legacy_keys_backfill_on_recovery_without_rotation_or_rebinding(client, api, db_session):
    bot, issued = second_binding(client, api)
    rows = db_session.query(ResourceBinding).all()
    before = {row.id: (row.credential_digest, row.bound_bot_uuid, row.revoked_at) for row in rows}
    for row in rows:
        row.credential_encrypted = None
    db_session.commit()
    assert client.get("/api/agent/context", headers=api["headers"]).status_code == 200
    first = client.post("/api/agent/bindings/recover", headers=api["headers"]).json()
    assert first["bindings"][0]["credential"] == api["headers"]["Authorization"][7:]
    assert first["bindings"][1]["credential"] is None
    assert first["bindings"][1]["credential_recoverable"] is False
    second_headers = {"Authorization": "Bearer " + issued["credential"]}
    assert client.post("/api/agent/bindings/recover", headers=second_headers).status_code == 200
    final = client.post("/api/agent/bindings/recover", headers=api["headers"]).json()
    assert final["bindings"][1]["credential"] == issued["credential"]
    db_session.expire_all()
    after = {
        row.id: (row.credential_digest, row.bound_bot_uuid, row.revoked_at) for row in db_session.query(ResourceBinding)
    }
    assert after == before


@pytest.mark.parametrize("damage", ["corrupt", "swapped"])
def test_bad_recovery_copy_does_not_hide_other_groups_or_return_another_key(client, api, db_session, damage):
    bot, issued = second_binding(client, api)
    rows = db_session.query(ResourceBinding).filter_by(external_user_id="alice").order_by(ResourceBinding.id).all()
    rows[1].credential_encrypted = "invalid" if damage == "corrupt" else rows[0].credential_encrypted
    db_session.commit()
    data = client.post("/api/agent/bindings/recover", headers=api["headers"]).json()
    assert data["bindings"][0]["credential"] == api["headers"]["Authorization"][7:]
    assert data["bindings"][1]["credential"] is None
    assert (
        client.get("/api/agent/context", headers={"Authorization": "Bearer " + issued["credential"]}).status_code == 200
    )


@pytest.mark.parametrize("namespace", ["same_app", "other_app", "other_tenant", "missing_route", "missing_tenant"])
def test_feishu_binding_directory_pins_application_and_tenant(client, api, db_session, namespace):
    from lockbot.backend.app.bots import encryption
    from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute

    second, _ = second_binding(client, api)
    application = None
    for index, bot_id in enumerate((api["bot_id"], second["id"])):
        bot = db_session.get(Bot, bot_id)
        bot.platform = "Feishu"
        if index == 0 or namespace in ("other_app", "other_tenant"):
            application = FeishuApplication(
                app_id=f"cli_review_namespace_{index}",
                name=f"Fixture {index}",
                tenant_key=""
                if namespace == "missing_tenant"
                else (f"fixture-tenant-{index}" if namespace == "other_tenant" else "fixture-tenant"),
                bot_open_id=f"ou_fixture_bot_{index}",
                app_secret=encryption.encrypt("FAKE_REVIEW_SECRET"),
                verification_token="",
                encrypt_key="",
                transport="http",
            )
            db_session.add(application)
            db_session.flush()
        if namespace != "missing_route":
            db_session.add(
                FeishuGroupRoute(application_id=application.id, chat_id=f"oc_fixture_{index}", bot_id=bot_id)
            )
        for binding in db_session.query(ResourceBinding).filter_by(bound_bot_id=bot_id):
            binding.platform = "feishu"
    db_session.commit()
    expected = {api["bot_id"], second["id"]} if namespace == "same_app" else {api["bot_id"]}
    for method, url in ((client.get, "/api/agent/bindings"), (client.post, "/api/agent/bindings/recover")):
        response = method(url, headers=api["headers"])
        assert response.status_code == 200
        assert {entry["bot"]["id"] for entry in response.json()["bindings"]} == expected


@pytest.mark.parametrize("change", ["revoked", "expired", "uuid", "revision", "deleted", "credential", "source"])
def test_recovery_rechecks_binding_changes_during_decryption(client, api, db_session, monkeypatch, change):
    from lockbot.backend.app.resource_auth import credentials

    _, issued = second_binding(client, api)
    original = credentials.recover_credential
    changed = False

    def revoke_after_read(binding):
        nonlocal changed
        value = original(binding)
        if binding.public_id == issued["binding_id"] and not changed:
            changed = True
            victim = db_session.query(ResourceBinding).filter_by(public_id=binding.public_id).one()
            bot = db_session.get(Bot, victim.bound_bot_id)
            if change == "revoked":
                victim.revoked_at = datetime.utcnow()
            elif change == "expired":
                victim.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
            elif change == "uuid":
                bot.uuid = "replacement-during-read"
            elif change == "revision":
                bot.identity_revision += 1
            elif change == "deleted":
                bot.is_deleted = True
            elif change == "credential":
                victim.credential_digest = credentials.digest_credential(credentials.generate_credential())
            else:
                source = (
                    db_session.query(ResourceBinding)
                    .filter_by(bound_bot_id=api["bot_id"], external_user_id="alice")
                    .one()
                )
                source.revoked_at = datetime.utcnow()
            db_session.commit()
        return value

    monkeypatch.setattr(credentials, "recover_credential", revoke_after_read)
    response = client.post("/api/agent/bindings/recover", headers=api["headers"])
    assert changed
    assert response.status_code == 401
    assert issued["credential"] not in response.text
