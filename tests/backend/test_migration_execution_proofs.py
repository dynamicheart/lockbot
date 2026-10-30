"""Real coordinator + signed ingress; no provider mocks masquerading as delivery."""

# ruff: noqa: F811

import copy
import json
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bot_migrations import target_proof
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.execution_proofs import SignedDeliveryValidator
from lockbot.backend.app.bot_migrations.models import BotMigration, MigrationDeliveryHandoff, MigrationTargetProof
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.feishu.service import accept_sdk_event

from backend.test_durable_platform_cutover import subject  # noqa: F401
from backend.test_migration_target_nonce import challenge  # noqa: F401


def fresh_proof(db, operation_id, app_id, template, clock):
    operation = db.get(BotMigration, operation_id)
    proof = target_proof.create_target_challenge(
        db,
        operation_id,
        actor_id=1,
        token_version=0,
        reviewed_target_digest=operation.target_digest,
        publication_evidence_ref="a" * 32,
        publication_valid_until=datetime.utcfromtimestamp(clock[0]) + timedelta(minutes=10),
    )
    clock[0] += 1
    event = copy.deepcopy(template)
    event["header"]["event_id"] = proof["proof_id"]
    event["event"]["message"].update(
        message_id=proof["proof_id"],
        create_time=str(int(clock[0] * 1000)),
        content=json.dumps({"text": "@_user_1 " + proof["command"]}),
    )
    assert accept_sdk_event(db, db.get(FeishuApplication, app_id), event)["accepted"]
    return proof


def test_switch_atomically_hands_off_and_requires_distinct_fresh_target_epoch(db_session, challenge):
    service, manager, bot_id, app_id, initial, message, clock = challenge
    operation_id = db_session.get(MigrationTargetProof, initial["proof_id"]).operation_id
    service.clock = lambda: clock[0]
    validator = SignedDeliveryValidator(operation_id, actor_id=1, token_version=0)
    service.target_validator, service.switch_handoff = validator, validator.handoff
    assert accept_sdk_event(db_session, db_session.get(FeishuApplication, app_id), message)["accepted"]
    assert service.resume(operation_id, actor_id=1, token_version=0)["phase"] == "checkpointed"
    clock[0] += 316
    # A receipt older than two minutes does not bridge the intentional metrics wait.
    with pytest.raises(MigrationBlocked, match="fresh_source_epoch"):
        service.resume(operation_id, actor_id=1, token_version=0)
    db_session.expire_all()
    assert db_session.get(Bot, bot_id).platform == "Infoflow"
    fresh_proof(db_session, operation_id, app_id, message, clock)
    with pytest.raises(MigrationBlocked, match="fresh_target_epoch"):
        service.resume(operation_id, actor_id=1, token_version=0)
    db_session.expire_all()
    operation = db_session.get(BotMigration, operation_id)
    assert operation.phase == "switched" and manager.migration_fenced(bot_id)
    assert db_session.get(Bot, bot_id).identity_revision == 1
    handoff = db_session.get(MigrationDeliveryHandoff, operation_id)
    assert handoff and handoff.target_revision == 1
    assert db_session.query(FeishuGroupRoute).one().bot_id == bot_id
    target = fresh_proof(db_session, operation_id, app_id, message, clock)
    assert target["proof_id"] != handoff.source_proof_id
    assert service.resume(operation_id, actor_id=1, token_version=0)["phase"] == "completed"
    assert not manager.migration_fenced(bot_id)


def test_target_challenge_cannot_be_forged_by_changing_platform_without_handoff(db_session, challenge):
    _, _, bot_id, app_id, initial, _, _ = challenge
    operation_id = db_session.get(MigrationTargetProof, initial["proof_id"]).operation_id
    operation = db_session.get(BotMigration, operation_id)
    bot = db_session.get(Bot, bot_id)
    operation.phase = "switched"
    bot.platform, bot.group_id, bot.identity_revision = "Feishu", "oc_target", 1
    from lockbot.backend.app.bot_migrations.coordinator import source_digest

    operation.target_bot_digest = source_digest(bot)
    db_session.add(FeishuGroupRoute(application_id=app_id, chat_id="oc_target", bot_id=bot_id))
    db_session.commit()
    with pytest.raises(MigrationBlocked, match="handoff"):
        target_proof.create_target_challenge(
            db_session,
            operation_id,
            actor_id=1,
            token_version=0,
            reviewed_target_digest=operation.target_digest,
            publication_evidence_ref="a" * 32,
            publication_valid_until=datetime.utcnow() + timedelta(minutes=10),
        )


def test_failed_handoff_transaction_does_not_publish_route_revision_or_receipt(db_session, challenge):
    service, manager, bot_id, app_id, initial, message, clock = challenge
    operation_id = db_session.get(MigrationTargetProof, initial["proof_id"]).operation_id
    service.clock = lambda: clock[0]
    validator = SignedDeliveryValidator(operation_id, actor_id=1, token_version=0)
    service.target_validator = validator
    assert accept_sdk_event(db_session, db_session.get(FeishuApplication, app_id), message)["accepted"]
    assert service.resume(operation_id, actor_id=1, token_version=0)["phase"] == "checkpointed"
    clock[0] += 316
    fresh_proof(db_session, operation_id, app_id, message, clock)

    def fail_after_receipt(db, operation, bot):
        validator.handoff(db, operation, bot)
        db.flush()
        raise RuntimeError("transaction interrupted")

    service.switch_handoff = fail_after_receipt
    with pytest.raises(RuntimeError, match="transaction interrupted"):
        service.resume(operation_id, actor_id=1, token_version=0)
    db_session.expire_all()
    bot = db_session.get(Bot, bot_id)
    assert bot.platform == "Infoflow" and bot.identity_revision == 0 and bot.api_key == "FAKE_OP"
    assert db_session.get(BotMigration, operation_id).phase == "checkpointed"
    assert db_session.get(MigrationDeliveryHandoff, operation_id) is None
    assert db_session.query(FeishuGroupRoute).count() == 0
    assert manager.migration_fenced(bot_id)
