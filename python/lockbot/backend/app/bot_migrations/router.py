"""Explicit super-admin migration workflow, disabled until deployment acceptance."""

import math
import os
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations.coordinator import CutoverCoordinator, MigrationBlocked
from lockbot.backend.app.bot_migrations.execution_proofs import SignedDeliveryValidator
from lockbot.backend.app.bot_migrations.metrics_policy import reviewed_metrics_policy
from lockbot.backend.app.bot_migrations.models import JOURNAL_VERSION, BotMigration, MigrationManagementPolicy
from lockbot.backend.app.bot_migrations.schemas import ChallengeRequest, PhaseRequest, PlanRequest
from lockbot.backend.app.bot_migrations.target_proof import _now, create_target_challenge
from lockbot.backend.app.bots.manager import InstanceRetiring, bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.feishu.models import FeishuApplication

router = APIRouter(tags=["Administrator migration"], include_in_schema=False)


def authority(request, user):
    if os.environ.get("LOCKBOT_ENABLE_PLATFORM_MIGRATION") != "1":
        raise HTTPException(503, detail={"code": "migration_execution_disabled"})
    version = getattr(request.state, "auth_token_version", None)
    if type(version) is not int:
        raise HTTPException(403, detail={"code": "current_super_administrator_required"})
    return {"actor_id": user.id, "token_version": version}


def blocked(exc):
    if isinstance(exc, InstanceRetiring):
        return HTTPException(409, detail={"code": "migration_transition_busy"})
    code = exc.code
    status = 403 if code in {"current_super_administrator_required", "current_review_authority_required"} else 409
    return HTTPException(status, detail={"code": code})


def operation_row(db, operation_id, auth):
    CutoverCoordinator._actor(db, **auth)
    operation = db.get(BotMigration, operation_id)
    if operation is None:
        raise HTTPException(404, detail={"code": "operation_not_found"})
    if operation.schema_version != JOURNAL_VERSION:
        raise MigrationBlocked("journal_version_unsupported")
    if operation.phase not in {
        "planned",
        "freezing",
        "checkpointed",
        "switched",
        "activating",
        "aborting",
        "completed",
        "aborted",
    }:
        raise MigrationBlocked("journal_phase_unsupported")
    return operation


def status(db, operation_id, auth):
    db.expire_all()
    operation = operation_row(db, operation_id, auth)
    bot, app = db.get(Bot, operation.bot_id), db.get(FeishuApplication, operation.target_application_id)
    result = CutoverCoordinator._result(operation)
    action = "resume"
    if operation.phase in {"completed", "aborted"}:
        action = operation.phase
    elif operation.phase == "checkpointed" and operation.metrics_not_before and _now() < operation.metrics_not_before:
        action = "wait_metrics"
    elif operation.phase in {"planned", "checkpointed", "switched", "activating"}:
        stage = "target" if operation.phase in {"switched", "activating"} else "source"
        try:
            SignedDeliveryValidator(operation_id, **auth)._latest(db, operation, stage)
        except MigrationBlocked:
            action = "target_challenge" if stage == "target" else "source_challenge"
    return {
        **result,
        "source_revision": operation.source_revision,
        "target_revision": operation.source_revision + 1,
        "target_digest": operation.target_digest,
        "target_application_id": operation.target_application_id,
        "target_application_name": app.name if app else None,
        "target_chat_id": operation.target_chat_id,
        "bot_name": bot.name if bot else None,
        "next_action": action,
        "can_resume": action == "resume",
        "can_abort": operation.phase in {"planned", "freezing", "checkpointed", "aborting"},
    }


def coordinator(db, operation_id, auth):
    operation = operation_row(db, operation_id, auth)
    policy = db.get(MigrationManagementPolicy, operation_id)
    if policy is None:
        raise MigrationBlocked("management_execution_policy_missing")
    bot = db.get(Bot, operation.bot_id)
    reviewed = reviewed_metrics_policy(db, bot)
    if reviewed is not None and (
        policy.maximum_clock_skew_seconds is None
        or reviewed["maximum_clock_skew_seconds"] > policy.maximum_clock_skew_seconds
    ):
        raise MigrationBlocked("reviewed_metrics_clock_bound_changed")
    bound = policy.maximum_clock_skew_seconds
    validator = SignedDeliveryValidator(operation_id, **auth)

    # A renewable deployment review can tighten but never enlarge the committed
    # wait bound. The data plane remains browser/agent -> independent gateway.
    def metrics_validator(session, current, source):
        fresh = reviewed_metrics_policy(session, current)
        return fresh is not None and fresh["maximum_clock_skew_seconds"] <= bound

    return CutoverCoordinator(
        sessionmaker(bind=db.get_bind()),
        bot_manager,
        clock=time.time,
        target_validator=validator,
        switch_handoff=validator.handoff,
        metrics_validator=metrics_validator,
        maximum_clock_offset=bound,
    )


@router.post("/api/bots/{bot_id}/migration-plan")
def plan(
    bot_id: int,
    body: PlanRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    auth = authority(request, user)
    try:
        CutoverCoordinator._actor(db, **auth)
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted:
            raise HTTPException(404, detail={"code": "bot_not_found"})
        reviewed = reviewed_metrics_policy(db, bot)
        service = CutoverCoordinator(sessionmaker(bind=db.get_bind()), bot_manager, clock=time.time)
        service.plan(bot_id, body.target_application_id, body.target_chat_id, operation_id=body.operation_id, **auth)
        db.rollback()
        operation_row(db, body.operation_id, auth)
        if db.get(MigrationManagementPolicy, body.operation_id) is None:
            db.add(
                MigrationManagementPolicy(
                    operation_id=body.operation_id,
                    maximum_clock_skew_seconds=math.ceil(reviewed["maximum_clock_skew_seconds"]) if reviewed else None,
                )
            )
            db.commit()
        return status(db, body.operation_id, auth)
    except (MigrationBlocked, InstanceRetiring) as exc:
        raise blocked(exc) from None


@router.get("/api/bot-migrations/{operation_id}")
def inspect(
    operation_id: str, request: Request, user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    auth = authority(request, user)
    try:
        return status(db, operation_id, auth)
    except MigrationBlocked as exc:
        raise blocked(exc) from None


@router.post("/api/bot-migrations/{operation_id}/target-challenge")
def challenge(
    operation_id: str,
    body: ChallengeRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    auth = authority(request, user)
    try:
        operation_row(db, operation_id, auth)
        result = create_target_challenge(
            db,
            operation_id,
            **auth,
            reviewed_target_digest=body.reviewed_target_digest,
            publication_evidence_ref=body.publication_evidence_ref,
            publication_valid_until=_now() + timedelta(minutes=10),
        )
        return {**result, "status": status(db, operation_id, auth)}
    except MigrationBlocked as exc:
        raise blocked(exc) from None


@router.post("/api/bot-migrations/{operation_id}/resume")
def resume(
    operation_id: str,
    body: PhaseRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    auth = authority(request, user)
    try:
        service = coordinator(db, operation_id, auth)
        db.rollback()
        try:
            service.resume(operation_id, expected_phase=body.expected_phase, **auth)
        except MigrationBlocked as exc:
            # After a committed identity switch, fresh target proof is a normal
            # next step. The response must expose that durable phase, not suggest rollback.
            if exc.code != "fresh_target_epoch_proof_required":
                raise
        return status(db, operation_id, auth)
    except (MigrationBlocked, InstanceRetiring) as exc:
        raise blocked(exc) from None


@router.post("/api/bot-migrations/{operation_id}/abort")
def abort(
    operation_id: str,
    body: PhaseRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    auth = authority(request, user)
    try:
        operation_row(db, operation_id, auth)
        service = CutoverCoordinator(sessionmaker(bind=db.get_bind()), bot_manager, clock=time.time)
        db.rollback()
        service.abort(operation_id, expected_phase=body.expected_phase, **auth)
        return status(db, operation_id, auth)
    except (MigrationBlocked, InstanceRetiring) as exc:
        raise blocked(exc) from None


@router.get("/api/bots/{bot_id}/migration-management")
def management(bot_id: int, request: Request, user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    # Read-only feature discovery must work while execution is disabled. No
    # plan, proof, credential, route, or audit write is performed on page load.
    version = getattr(request.state, "auth_token_version", None)
    auth = {"actor_id": user.id, "token_version": version}
    try:
        CutoverCoordinator._actor(db, **auth)
        bot = db.get(Bot, bot_id)
        if bot is None or bot.is_deleted:
            raise HTTPException(404, detail={"code": "bot_not_found"})
        enabled = os.environ.get("LOCKBOT_ENABLE_PLATFORM_MIGRATION") == "1"
        operation = (
            (
                db.query(BotMigration)
                .filter_by(bot_id=bot_id)
                .order_by(BotMigration.created_at.desc(), BotMigration.id.desc())
                .first()
            )
            if enabled
            else None
        )
        return {
            "enabled": enabled,
            "supported_source": str(bot.platform).lower() == "infoflow",
            "empty_state_only": True,
            "source_uuid": bot.uuid,
            "identity_revision": bot.identity_revision,
            "latest_operation": status(db, operation.id, auth) if operation else None,
        }
    except MigrationBlocked as exc:
        raise blocked(exc) from None
