"""Native panel grants and reports use original ORM bindings and live bot snapshots."""

# ruff: noqa: F811

import json
from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.personal_panel import native
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.settings.models import SiteSetting

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture
def panel(api, db_session, session_factory, monkeypatch):
    ensure_instance_id(db_session)
    monkeypatch.setattr(native, "bot_manager", api["manager"])
    provider = native.NativeProvider({"issuer": "https://identity.example.invalid", "session_factory": session_factory})
    credential = api["headers"]["Authorization"][7:]
    return provider, provider.authenticate(credential)


def _binding(db_session, grant):
    db_session.expire_all()
    return db_session.query(ResourceBinding).filter(ResourceBinding.public_id == grant["ref"]).one()


def test_original_confirmed_binding_makes_stable_keyless_grant(panel, api, db_session, session_factory):
    provider, grant = panel
    row = _binding(db_session, grant)
    assert set(grant) == {"issuer", "platform", "realm", "subject", "scope", "ref"}
    assert all(isinstance(value, str) for value in grant.values())
    assert grant["subject"] == "alice" and grant["platform"] == "infoflow"
    assert grant["ref"] == row.public_id
    assert api["headers"]["Authorization"][7:] not in json.dumps(grant)
    assert row.credential_digest not in json.dumps(grant)
    assert provider.validate(grant)
    assert native.NativeProvider({**provider.config, "session_factory": session_factory}).validate(grant)
    assert provider.authenticate(api["headers"]["Authorization"][7:]) == grant
    assert row.last_used_at is None


@pytest.mark.parametrize("credential", ["", "invalid", "lbk_operator", "eyJ.management.jwt"])
def test_non_personal_keys_are_rejected(panel, credential):
    provider, _ = panel
    with pytest.raises(native.ProviderError):
        provider.authenticate(credential)


def test_pending_binding_cannot_login(panel):
    provider, _ = panel
    pending = service.create_binding(label="pending-agent")
    with pytest.raises(native.ProviderError):
        provider.authenticate(pending["credential"])


@pytest.mark.parametrize(
    "change",
    [
        "revoke",
        "expire",
        "revision",
        "bound_revision",
        "uuid",
        "delete",
        "disable",
        "subject",
        "platform",
        "instance",
        "missing_binding",
    ],
)
def test_persisted_grant_rechecks_live_original_authority(panel, db_session, change):
    provider, grant = panel
    row = _binding(db_session, grant)
    bot = db_session.get(Bot, row.bound_bot_id)
    if change == "revoke":
        row.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    elif change == "expire":
        row.credential_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    elif change == "revision":
        bot.identity_revision += 1
    elif change == "bound_revision":
        row.bound_identity_revision -= 1
    elif change == "uuid":
        bot.uuid = "replacement-bot-uuid"
    elif change == "delete":
        bot.is_deleted = True
    elif change == "disable":
        bot.resource_auth_enabled = False
    elif change == "subject":
        row.external_user_id = "different-actor"
    elif change == "platform":
        row.platform = "feishu"
    elif change == "instance":
        db_session.get(SiteSetting, "resource_api_instance_id").value = "replacement-instance"
    else:
        db_session.delete(row)
    db_session.commit()
    assert not provider.validate(grant)
    with pytest.raises(native.ProviderError):
        provider.report(grant)


@pytest.mark.parametrize("field", sorted(native.FIELDS))
def test_forged_grant_fields_are_rejected(panel, field):
    provider, grant = panel
    assert not provider.validate({**grant, field: "forged"})


def test_missing_instance_identity_is_not_created_by_login(panel, api, db_session):
    provider, grant = panel
    db_session.delete(db_session.get(SiteSetting, "resource_api_instance_id"))
    db_session.commit()
    assert not provider.validate(grant)
    with pytest.raises(native.ProviderError, match="instance_identity_unavailable"):
        provider.authenticate(api["headers"]["Authorization"][7:])
    assert db_session.get(SiteSetting, "resource_api_instance_id") is None


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_live_nonempty_snapshot_returns_only_own_holdings_and_reservations(panel, api, client):
    provider, grant = panel
    state = api["bot"].state.bot_state
    device = isinstance(state["n1"], list)
    own_target = "n1 dev0" if device else "n1"
    other_target = "n1 dev1" if device else "n2"
    for target, headers in ((own_target, api["headers"]), (other_target, api["other_headers"])):
        response = client.post(
            f"/api/bots/{api['bot_id']}/commands", headers=headers, json={"command": f"lock {target} 1h"}
        )
        assert response.status_code == 200, response.text
    queue = "book" in api["bot"].supported_commands()
    if queue:
        response = client.post(
            f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": f"book {other_target} 1h"}
        )
        assert response.status_code == 200, response.text
    before = api["bot"].state_snapshot()[1]
    result = provider.report(grant)
    assert len(result) == 1 and result[0]["status"] == "ok"
    assert result[0]["group_id"] and result[0]["source_at"]
    holds = [allocation for allocation in result[0]["allocations"] if allocation["kind"] == "hold"]
    assert len(holds) == 1 and holds[0]["node"] == "n1"
    assert holds[0]["device"] == ("0" if device else None)
    assert datetime.fromisoformat(holds[0]["expires_at"]) > datetime.now(timezone.utc)
    bookings = [allocation for allocation in result[0]["allocations"] if allocation["kind"] == "reservation"]
    assert len(bookings) == int(queue)
    assert api["bot"].state_snapshot()[1] == before
    assert "bob" not in json.dumps(result)


def test_unavailable_group_remains_unknown_with_stable_group_id(panel, api):
    provider, grant = panel
    before = provider.report(grant)[0]
    api["manager"].shutdown_all()
    after = provider.report(grant)[0]
    assert after["status"] == "unknown" and after["allocations"] == []
    assert after["group_id"] == before["group_id"]


def test_revocation_during_snapshot_is_rechecked(panel, api, session_factory, monkeypatch):
    provider, grant = panel
    snapshot = api["bot"].state_snapshot

    def revoke_during_snapshot():
        with session_factory() as db:
            row = db.query(ResourceBinding).filter(ResourceBinding.public_id == grant["ref"]).one()
            row.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        return snapshot()

    monkeypatch.setattr(api["bot"], "state_snapshot", revoke_during_snapshot)
    with pytest.raises(native.ProviderError):
        provider.report(grant)


def test_distinct_bots_default_to_distinct_realms_until_attested(panel, api, db_session):
    provider, grant = panel
    row = _binding(db_session, grant)
    source = db_session.get(Bot, row.bound_bot_id)
    second = Bot(
        user_id=source.user_id,
        uuid="other-stable-bot-uuid",
        name=source.name,
        bot_type="NODE",
        platform="Infoflow",
        webhook_url="",
        cluster_configs="{}",
        identity_revision=0,
    )
    db_session.add(second)
    db_session.commit()
    issued = service.create_binding(label="second-bot-agent")
    service.handle_chat_binding(
        platform="infoflow", external_user_id="alice", bot_id=second.id, action="bind", argument=issued["bind_code"]
    )
    other = provider.authenticate(issued["credential"])
    assert other["subject"] == grant["subject"] and other["realm"] != grant["realm"]
    assert other["scope"] != grant["scope"]
    provider.config["trusted_realms"] = {
        uuid: {"platform": "infoflow", "realm": "verified-app-A-tenant-A"} for uuid in (source.uuid, second.uuid)
    }
    first_attested = provider.authenticate(api["headers"]["Authorization"][7:])
    second_attested = provider.authenticate(issued["credential"])
    assert first_attested["realm"] == second_attested["realm"]
    assert first_attested["scope"] != second_attested["scope"]
    provider.config["trusted_realms"][second.uuid]["realm"] = "verified-app-A-tenant-B"
    assert not provider.validate(second_attested)
    provider.config["trusted_realms"][source.uuid]["platform"] = "feishu"
    with pytest.raises(native.ProviderError, match="identity_namespace_mismatch"):
        provider.authenticate(api["headers"]["Authorization"][7:])


@pytest.mark.parametrize("problem", ["stale", "future", "unknown_holder", "broken_state"])
def test_snapshot_gaps_are_unknown_instead_of_confirmed_empty(panel, api, monkeypatch, problem):
    provider, grant = panel
    captured, state = api["bot"].state_snapshot()
    if problem == "stale":
        captured -= timedelta(minutes=2)
    elif problem == "future":
        captured += timedelta(minutes=2)
    elif problem == "unknown_holder":
        state["n1"]["current_users"] = [{"user_id": None}]
    else:
        state["n1"] = "malformed-state"
    monkeypatch.setattr(api["bot"], "state_snapshot", lambda: (captured, state))
    group = provider.report(grant)[0]
    assert group["status"] == "unknown"
    assert group["group_id"] and group["reason"]


def test_retained_failed_generation_is_unknown_even_with_a_snapshot(panel, api):
    provider, grant = panel
    api["manager"]._make_fatal_error_handler(api["bot_id"])(api["bot_id"])
    assert api["manager"].is_failed(api["bot_id"])
    group = provider.report(grant)[0]
    assert group["status"] == "unknown" and group["source_at"]


def test_only_original_binding_reads_task_on_current_hold(panel, api, client, db_session):
    provider, grant = panel
    url = f"/api/bots/{api['bot_id']}/commands"
    assert client.post(url, headers=api["headers"], json={"command": "lock n1 1h"}).status_code == 200
    row = _binding(db_session, grant)
    _, snapshot = api["bot"].state_snapshot()
    from lockbot.backend.app.resource_api.usage import holding_proof, store_declaration

    now = datetime.now(timezone.utc)
    store_declaration(
        db_session,
        bot_id=api["bot_id"],
        binding_id=row.id,
        resource_id="n1",
        purpose="Synthetic detailed note",
        agent_label="fixture-agent",
        proof=holding_proof(snapshot, "n1", "alice", now=now.timestamp()),
        now=now.replace(tzinfo=None),
        ttl_seconds=3600,
        task_category="unit_testing",
    )
    db_session.commit()
    own = provider.report(grant)[0]["allocations"]
    assert own[0]["own_task"] == {
        "category": native.TASK_CATEGORIES["unit_testing"],
        "note": "Synthetic detailed note",
        "agent_label": "fixture-agent",
    }
    other = provider.authenticate(api["other_headers"]["Authorization"].split()[1])
    assert "Synthetic detailed note" not in json.dumps(provider.report(other))
    assert client.post(url, headers=api["headers"], json={"command": "free n1"}).status_code == 200
    assert client.post(url, headers=api["headers"], json={"command": "lock n1 1h"}).status_code == 200
    assert provider.report(grant)[0]["allocations"][0]["own_task"] is None
