# ruff: noqa: F811
"""Exact expected target + two source authentications + independent admin review."""

import copy
import json
from datetime import datetime

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations import principal_pairing as pairing
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.declaration_reenrollment import declaration_reenrollment_plan
from lockbot.backend.app.bot_migrations.models import MigrationPrincipalPair, MigrationTargetProof
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.feishu.service import accept_sdk_event
from lockbot.backend.app.resource_api.usage import UsageDeclaration, holding_proof
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_migration_target_nonce import challenge, subject  # noqa: F401


@pytest.fixture
def pair(challenge, db_session, monkeypatch):
    _, _, bot_id, app_id, delivery, message, clock = challenge
    monkeypatch.setattr(pairing, "_now", lambda: datetime.utcfromtimestamp(clock[0]))
    monkeypatch.setattr(pairing, "_budget", pairing.IngressBudget(app_limit=60, actor_limit=6))
    bot = db_session.get(Bot, bot_id)
    key = credentials.generate_credential()
    binding = ResourceBinding(
        public_id=credentials.generate_public_id(),
        platform="infoflow",
        external_user_id="alice",
        bound_bot_id=bot_id,
        bound_bot_uuid=bot.uuid,
        bound_identity_revision=bot.identity_revision,
        credential_digest=credentials.digest_credential(key),
    )
    db_session.add(binding)
    db_session.commit()
    operation = db_session.get(MigrationTargetProof, delivery["proof_id"]).operation_id
    created = pairing.create_pairing(db_session, operation, source_key=key, expected_target_open_id="ou_person")
    clock[0] += 0.1
    message["event"]["message"]["content"] = json.dumps({"text": "@_user_1 " + created["command"]})
    message["event"]["message"]["create_time"] = str(int(clock[0] * 1000))
    return operation, created, key, binding.id, bot_id, app_id, message, clock


def deliver(db, pair):
    return accept_sdk_event(db, db.get(FeishuApplication, pair[5]), pair[6])


def confirm(db, pair):
    return pairing.confirm_pairing_source(
        db, pair[1]["pairing_id"], source_key=pair[2], expected_target_open_id="ou_person"
    )


def review(db, pair):
    return pairing.review_pairing(
        db,
        pair[1]["pairing_id"],
        actor_id=1,
        token_version=0,
        exact_source_user_id="alice",
        exact_target_open_id="ou_person",
    )


def test_expected_target_reply_source_reauthentication_then_admin_review(db_session, pair):
    assert deliver(db_session, pair)["accepted"]
    assert not deliver(db_session, pair)["accepted"]
    assert confirm(db_session, pair)["source_confirmed"]
    assert review(db_session, pair)["reviewed"]
    links = pairing.load_reviewed_links(db_session, pair[0], actor_id=1, token_version=0)
    assert len(links) == 1 and (links[0].source_user_id, links[0].target_open_id) == ("alice", "ou_person")
    assert links[0].source_account_proof != links[0].target_group_sender_proof
    assert db_session.query(ResourceBinding).count() == 1 and db_session.query(FeishuGroupRoute).count() == 0
    row = db_session.get(MigrationPrincipalPair, pair[1]["pairing_id"])
    assert pair[2] not in repr(row.__dict__) and pair[1]["command"].split()[1] not in repr(row.__dict__)
    assert db_session.get(Bot, pair[4]).platform == "Infoflow"


def test_wrong_first_sender_never_claims_the_intended_pairing(db_session, pair):
    wrong = copy.deepcopy(pair[6])
    wrong["event"]["sender"]["sender_id"]["open_id"] = "ou_attacker"
    app = db_session.get(FeishuApplication, pair[5])
    assert not accept_sdk_event(db_session, app, wrong)["accepted"]
    assert db_session.get(MigrationPrincipalPair, pair[1]["pairing_id"]).status == "pending"
    assert deliver(db_session, pair)["accepted"]


@pytest.mark.parametrize("step", ["before_target", "before_source_confirm", "before_review", "before_load"])
def test_source_revocation_invalidates_every_pre_freeze_stage(db_session, pair, step):
    if step != "before_target":
        assert deliver(db_session, pair)["accepted"]
    if step in {"before_review", "before_load"}:
        confirm(db_session, pair)
    if step == "before_load":
        review(db_session, pair)
    db_session.get(ResourceBinding, pair[3]).revoked_at = datetime.utcnow()
    db_session.commit()
    if step == "before_target":
        assert not deliver(db_session, pair)["accepted"]
    else:
        action = {
            "before_source_confirm": lambda: confirm(db_session, pair),
            "before_review": lambda: review(db_session, pair),
            "before_load": lambda: pairing.load_reviewed_links(db_session, pair[0], actor_id=1, token_version=0),
        }[step]
        with pytest.raises(MigrationBlocked, match="source_pairing_authority"):
            action()


@pytest.mark.parametrize("change", ["expired", "disabled", "revision", "binding_digest", "target_scope"])
def test_expiry_identity_and_source_authority_changes_fail_closed(db_session, pair, change):
    assert deliver(db_session, pair)["accepted"]
    if change == "expired":
        pair[7][0] += 121
    elif change == "disabled":
        db_session.get(Bot, pair[4]).resource_auth_enabled = False
    elif change == "revision":
        db_session.get(ResourceBinding, pair[3]).bound_identity_revision += 1
    elif change == "binding_digest":
        db_session.get(ResourceBinding, pair[3]).credential_digest = "f" * 64
    else:
        db_session.get(FeishuApplication, pair[5]).tenant_key = "another-tenant"
    db_session.commit()
    with pytest.raises(MigrationBlocked):
        confirm(db_session, pair)


def test_key_and_exact_target_must_be_confirmed_again(db_session, pair):
    assert deliver(db_session, pair)["accepted"]
    with pytest.raises(MigrationBlocked, match="confirmation_mismatch"):
        pairing.confirm_pairing_source(
            db_session, pair[1]["pairing_id"], source_key=pair[2], expected_target_open_id="ou_other"
        )
    with pytest.raises(MigrationBlocked, match="confirmation_mismatch"):
        pairing.confirm_pairing_source(
            db_session,
            pair[1]["pairing_id"],
            source_key=credentials.generate_credential(),
            expected_target_open_id="ou_person",
        )
    assert confirm(db_session, pair)["source_confirmed"]


def test_admin_cannot_skip_source_confirmation_or_guess_principal_names(db_session, pair):
    assert deliver(db_session, pair)["accepted"]
    with pytest.raises(MigrationBlocked, match="source_confirmation_required"):
        review(db_session, pair)
    confirm(db_session, pair)
    with pytest.raises(MigrationBlocked, match="exact_principal"):
        pairing.review_pairing(
            db_session,
            pair[1]["pairing_id"],
            actor_id=1,
            token_version=0,
            exact_source_user_id="Alice",
            exact_target_open_id="ou_person",
        )
    db_session.get(User, 1).role = "admin"
    db_session.commit()
    with pytest.raises(MigrationBlocked, match="review_authority"):
        review(db_session, pair)


def test_reenrollment_plan_has_only_stable_pointers_no_note_copy_or_ttl_extension(db_session, pair):
    assert deliver(db_session, pair)["accepted"]
    confirm(db_session, pair)
    review(db_session, pair)
    now = pair[7][0]
    state = {
        "node1": {
            "status": "exclusive",
            "current_users": [{"user_id": "alice", "start_time": now - 10, "duration": 300, "holding_id": "a" * 32}],
            "booking_list": [],
        }
    }
    mapped = copy.deepcopy(state)
    mapped["node1"]["current_users"][0]["user_id"] = "ou_person"
    expires = datetime.utcfromtimestamp(now + 60)
    usage = UsageDeclaration(
        bot_id=pair[4],
        binding_id=pair[3],
        resource_id="node1",
        purpose="CONFIDENTIAL_FIXTURE",
        agent_label="PRIVATE_AGENT_FIXTURE",
        task_category="operator_development",
        holding_proof=holding_proof(state, "node1", "alice", now=now),
        updated_at=datetime.utcfromtimestamp(now),
        expires_at=expires,
    )
    db_session.add(usage)
    db_session.commit()
    plan = declaration_reenrollment_plan(db_session, pair[0], state, mapped, actor_id=1, token_version=0, now=now)
    assert len(plan["items"]) == 1 and not plan["automatic_copy"] and not plan["can_commit"]
    assert plan["items"][0]["original_expires_at"] == pytest.approx(now + 60, rel=0, abs=1e-6)
    assert "CONFIDENTIAL_FIXTURE" not in str(plan) and "PRIVATE_AGENT_FIXTURE" not in str(plan)
    assert db_session.query(UsageDeclaration).count() == 1
    db_session.expire_all()
    assert db_session.get(UsageDeclaration, usage.id).binding_id == pair[3]
    assert db_session.get(UsageDeclaration, usage.id).expires_at == expires
    mapped["node1"]["current_users"][0]["holding_id"] = "b" * 32
    stale = declaration_reenrollment_plan(db_session, pair[0], state, mapped, actor_id=1, token_version=0, now=now)
    assert stale["items"] == [] and stale["stale"] == 1


@pytest.mark.parametrize("conflict", ["source", "target"])
def test_reviewed_mapping_cannot_merge_source_or_target_principals(db_session, pair, conflict):
    assert deliver(db_session, pair)["accepted"]
    confirm(db_session, pair)
    review(db_session, pair)
    source, target, key = "alice", "ou_other", pair[2]
    if conflict == "target":
        source, target, key = "bob", "ou_person", credentials.generate_credential()
        bot = db_session.get(Bot, pair[4])
        db_session.add(
            ResourceBinding(
                public_id=credentials.generate_public_id(),
                platform="infoflow",
                external_user_id=source,
                bound_bot_id=bot.id,
                bound_bot_uuid=bot.uuid,
                bound_identity_revision=bot.identity_revision,
                credential_digest=credentials.digest_credential(key),
            )
        )
        db_session.commit()
    second = pairing.create_pairing(db_session, pair[0], source_key=key, expected_target_open_id=target)
    message = copy.deepcopy(pair[6])
    message["event"]["sender"]["sender_id"]["open_id"] = target
    message["event"]["message"]["message_id"] = "second-proof-event"
    message["event"]["message"]["content"] = json.dumps({"text": "@_user_1 " + second["command"]})
    assert accept_sdk_event(db_session, db_session.get(FeishuApplication, pair[5]), message)["accepted"]
    pairing.confirm_pairing_source(db_session, second["pairing_id"], source_key=key, expected_target_open_id=target)
    with pytest.raises(MigrationBlocked, match="conflicts"):
        pairing.review_pairing(
            db_session,
            second["pairing_id"],
            actor_id=1,
            token_version=0,
            exact_source_user_id=source,
            exact_target_open_id=target,
        )
    assert db_session.query(MigrationPrincipalPair).filter_by(status="reviewed").count() == 1


@pytest.mark.parametrize("change", ["password", "session", "expired_binding"])
def test_reviewed_links_do_not_outlive_current_authority(db_session, pair, change):
    assert deliver(db_session, pair)["accepted"]
    confirm(db_session, pair)
    review(db_session, pair)
    if change == "password":
        db_session.get(User, 1).must_change_password = True
    elif change == "session":
        db_session.get(User, 1).token_version = 1
    else:
        db_session.get(ResourceBinding, pair[3]).credential_expires_at = datetime.utcfromtimestamp(pair[7][0] - 1)
    db_session.commit()
    with pytest.raises(MigrationBlocked):
        pairing.load_reviewed_links(db_session, pair[0], actor_id=1, token_version=0)


def test_pairing_namespace_is_not_an_execution_or_management_api(client, admin_header, db_session, pair):
    for action in ("migration-pairing", "migration-pair-review", "migration-confirm"):
        assert client.post(f"/api/bots/{pair[4]}/{action}", headers=admin_header, json={}).status_code == 404
    from lockbot.backend.app.feishu.router import accept_event as unverified

    assert not unverified(db_session, db_session.get(FeishuApplication, pair[5]), pair[6])["accepted"]
    assert db_session.get(MigrationPrincipalPair, pair[1]["pairing_id"]).status == "pending"


def test_pairing_can_resume_from_durable_records_in_a_new_session(db_session, session_factory, pair):
    assert deliver(db_session, pair)["accepted"]
    with session_factory() as restored:
        assert confirm(restored, pair)["source_confirmed"]
        assert review(restored, pair)["reviewed"]
        links = pairing.load_reviewed_links(restored, pair[0], actor_id=1, token_version=0)
        assert len(links) == 1 and links[0].target_open_id == "ou_person"


def test_source_op_key_or_missing_target_identity_cannot_create_pairing(db_session, pair):
    with pytest.raises(MigrationBlocked, match="exact_target_open_id"):
        pairing.create_pairing(db_session, pair[0], source_key=pair[2], expected_target_open_id="")
    with pytest.raises(MigrationBlocked, match="source_pairing_authority"):
        pairing.create_pairing(db_session, pair[0], source_key="FAKE_OP", expected_target_open_id="ou_person")
