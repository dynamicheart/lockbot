"""One callback per enterprise application, routed only to explicitly configured groups."""

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import get_current_user, require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute, FeishuReceivedMessage
from lockbot.backend.app.feishu.service import application_identity
from lockbot.backend.app.platform_applications.service import credentials_for, runtime_guard
from lockbot.backend.app.rate_limit import limiter
from lockbot.core.handler import BINDING_COMMANDS, execute_command, redact_command_text
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient
from lockbot.core.platforms.feishu_events import InvalidFeishuEvent, normalize_message, verify_http

router = APIRouter(prefix="/api/feishu", tags=["Feishu"])
logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu")
_capacity = threading.BoundedSemaphore(64)


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9]+$", max_length=128)
    app_secret: SecretStr
    tenant_key: str = Field(default="", max_length=128)
    verification_token: SecretStr = SecretStr("")
    encrypt_key: SecretStr = SecretStr("")
    transport: Literal["http", "websocket"] = "websocket"
    unbound_greeting_enabled: bool = False

    @field_validator("app_secret")
    @classmethod
    def required_secret(cls, value):
        if not value.get_secret_value().strip() or len(value.get_secret_value()) > 512:
            raise ValueError("A non-empty application credential is required")
        return value

    @field_validator("verification_token", "encrypt_key")
    @classmethod
    def bounded_secret(cls, value):
        if len(value.get_secret_value()) > 512:
            raise ValueError("Application credentials must not exceed 512 characters")
        return value

    @model_validator(mode="after")
    def http_requires_token(self):
        if self.transport == "http" and not self.verification_token.get_secret_value().strip():
            raise ValueError("HTTP callbacks require a Verification Token")
        if self.transport == "http" and not self.tenant_key.strip():
            raise ValueError("HTTP callbacks require the enterprise tenant key")
        return self


class ApplicationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    app_secret: SecretStr | None = None
    unbound_greeting_enabled: bool | None = None

    @field_validator("app_secret")
    @classmethod
    def validate_secret(cls, value):
        return ApplicationCreate.required_secret(value) if value is not None else value


def _application_audit(db, user, request, app, action):
    """Sensitive application operations fail closed if their audit cannot be committed."""
    from lockbot.backend.app.audit.models import AuditLog

    db.add(
        AuditLog(
            operator_id=user.id,
            operator_username=user.username,
            operator_role=user.role,
            action=action,
            target_type="feishu_application",
            target_id=app.id,
            target_name=app.name,
            ip_address=request.client.host if request.client else None,
            result="success",
        )
    )
    db.flush()


def _recover_application_receiver(db, application_id):
    """After failed management writes, reconnect only from the persisted database state."""
    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.service import invalidate_application_clients

    db.rollback()
    factory = sessionmaker(bind=db.get_bind())
    with factory() as current:
        app = current.get(FeishuApplication, application_id)
        if app is None:
            return
        invalidate_application_clients(
            app.app_id, secret=credentials_for(app)["app_secret"], credential_guard=runtime_guard(app)
        )
        reconnect = app.transport == "websocket"
    if reconnect:
        connectors.connect(application_id, factory=factory)


@router.post("/applications/{application_id}/reveal")
@limiter.limit("6/minute")
def reveal_application(
    application_id: int,
    request: Request,
    response: Response,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    app = db.get(FeishuApplication, application_id)
    if app is None:
        raise HTTPException(404, "Application not found", headers={"Cache-Control": "no-store"})
    _application_audit(db, user, request, app, "feishu.reveal_secret")
    db.commit()
    return {"app_secret": credentials_for(app)["app_secret"]}


@router.patch("/applications/{application_id}")
def update_application(
    application_id: int,
    body: ApplicationUpdate,
    request: Request,
    response: Response,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.service import invalidate_application_clients

    response.headers["Cache-Control"] = "no-store"
    app = db.get(FeishuApplication, application_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    actor_id, actor_version = user.id, user.effective_token_version
    expected_identity = (app.app_id, app.bot_open_id, app.tenant_key, app.transport, app.app_secret)
    db.rollback()  # Do not retain a SQLite read snapshot across external credential verification.
    secret = body.app_secret.get_secret_value() if body.app_secret is not None else None
    if secret is not None:
        try:
            identity = FeishuClient(expected_identity[0], secret).bot_identity()
        except FeishuAPIError:
            raise HTTPException(502, "Could not verify application credentials") from None
        if identity != expected_identity[1]:
            raise HTTPException(409, "Application bot identity must remain unchanged")

    def refresh_current():
        nonlocal app, user
        db.rollback()
        user = db.get(User, actor_id)
        if user is None or user.role != "super_admin" or user.effective_token_version != actor_version:
            raise HTTPException(403, "Super administrator required")
        app = db.get(FeishuApplication, application_id)
        if app is None:
            raise HTTPException(404, "Application not found")
        if expected_identity != (app.app_id, app.bot_open_id, app.tenant_key, app.transport, app.app_secret):
            raise HTTPException(409, "Application changed; refresh before editing")

    def before_stop():
        refresh_current()
        db.rollback()  # No database read transaction may span receiver termination/join.

    try:
        # Names do not affect receiving; credentials must not leave an old receiver active.
        with (
            connectors.application_change(
                application_id,
                before_stop=before_stop,
                recover=lambda: _recover_application_receiver(db, application_id),
            )
            if secret is not None
            else connectors.application_guard()
        ):
            refresh_current()
            app.name = body.name
            if body.unbound_greeting_enabled is not None:
                from lockbot.backend.app.feishu.membership import set_greeting_policy

                set_greeting_policy(db, app.id, body.unbound_greeting_enabled)
            if secret is not None:
                app.app_secret = encryption.encrypt(secret)
            _application_audit(db, user, request, app, "feishu.update_application")
            db.commit()
            if secret is not None:
                invalidate_application_clients(app.app_id, secret=secret, credential_guard=runtime_guard(app))
                if app.transport == "websocket":
                    try:
                        connectors.connect(application_id, factory=sessionmaker(bind=db.get_bind()))
                    except (ValueError, RuntimeError):
                        return {
                            "id": app.id,
                            "name": app.name,
                            "app_id": app.app_id,
                            "transport": app.transport,
                            "receiver_restart_required": True,
                        }
    except RuntimeError:
        db.rollback()
        raise HTTPException(
            503, "Application receiver requires restart", headers={"Cache-Control": "no-store"}
        ) from None
    return {"id": app.id, "name": app.name, "app_id": app.app_id, "transport": app.transport}


@router.delete("/applications/{application_id}", status_code=204)
def delete_application(
    application_id: int, request: Request, user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.service import invalidate_application_clients

    def check_migration_unused():
        from lockbot.backend.app.bot_migrations.models import BotMigration

        if (
            db.query(BotMigration)
            .filter(
                BotMigration.target_application_id == application_id,
                BotMigration.phase.not_in(("completed", "aborted")),
            )
            .first()
        ):
            raise HTTPException(409, "Application is reserved by an active migration")

    check_migration_unused()
    app = db.get(FeishuApplication, application_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    if db.query(FeishuGroupRoute).filter_by(application_id=application_id).first():
        raise HTTPException(409, "Application is associated with resource bots")
    if db.query(FeishuReceivedMessage).filter_by(application_id=application_id, state="claimed").first():
        raise HTTPException(409, "Application has an in-flight message")
    actor_id, actor_version, expected_app_id = user.id, user.effective_token_version, app.app_id
    db.rollback()

    def check_unused():
        nonlocal app, user
        db.rollback()
        user = db.get(User, actor_id)
        if user is None or user.role != "super_admin" or user.effective_token_version != actor_version:
            raise HTTPException(403, "Super administrator required")
        check_migration_unused()
        app = db.get(FeishuApplication, application_id)
        if app is None or app.app_id != expected_app_id:
            raise HTTPException(409, "Application changed; refresh before deleting")
        if db.query(FeishuGroupRoute).filter_by(application_id=application_id).first():
            raise HTTPException(409, "Application is associated with resource bots")
        if db.query(FeishuReceivedMessage).filter_by(application_id=application_id, state="claimed").first():
            raise HTTPException(409, "Application has an in-flight message")

    def before_stop():
        check_unused()
        db.rollback()

    try:
        with connectors.application_change(
            application_id, before_stop=before_stop, recover=lambda: _recover_application_receiver(db, application_id)
        ):
            check_unused()
            _application_audit(db, user, request, app, "feishu.delete_application")
            db.query(FeishuReceivedMessage).filter_by(application_id=application_id).delete()
            app_id = app.app_id
            from lockbot.backend.app.feishu.models import (
                FeishuGroupMembership,
                FeishuMembershipEvent,
                FeishuMembershipPolicy,
            )

            for model in (FeishuGroupMembership, FeishuMembershipEvent, FeishuMembershipPolicy):
                db.query(model).filter_by(application_id=app.id).delete()
            db.delete(app)
            db.commit()
            invalidate_application_clients(app_id)
    except RuntimeError:
        db.rollback()
        raise HTTPException(503, "Application receiver has not stopped") from None
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/applications")
def applications(response: Response, user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.membership import greeting_enabled

    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "id": row.id,
            "name": row.name,
            "app_id": row.app_id,
            "transport": row.transport,
            "unbound_greeting_enabled": greeting_enabled(db, row.id),
            "receiver": connectors.snapshot(row.id) if row.transport == "websocket" else None,
        }
        for row in db.query(FeishuApplication).order_by(FeishuApplication.id).all()
    ]


@router.post("/applications", status_code=201)
def create_application(
    body: ApplicationCreate,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    actor_id, actor_version = user.id, user.effective_token_version
    try:
        bot_open_id = FeishuClient(body.app_id, body.app_secret.get_secret_value()).bot_identity()
    except FeishuAPIError:
        raise HTTPException(502, "Could not verify Feishu application credentials and bot capability") from None
    db.rollback()  # External verification cannot retain a stale administrator snapshot.
    user = db.get(User, actor_id)
    if user is None or user.role != "super_admin" or user.effective_token_version != actor_version:
        raise HTTPException(403, "Current super administrator authority required")
    row = FeishuApplication(
        name=body.name,
        app_id=body.app_id,
        tenant_key=body.tenant_key,
        bot_open_id=bot_open_id,
        app_secret=encryption.encrypt(body.app_secret.get_secret_value()),
        verification_token=encryption.encrypt(body.verification_token.get_secret_value()),
        encrypt_key=encryption.encrypt(body.encrypt_key.get_secret_value()),
        transport=body.transport,
    )
    db.add(row)
    try:
        db.flush()
        if body.unbound_greeting_enabled:
            from lockbot.backend.app.feishu.membership import set_greeting_policy

            set_greeting_policy(db, row.id, True)
        write_audit_log(
            db,
            user,
            "feishu.create_application",
            target_type="feishu_application",
            target_id=row.id,
            target_name=row.name,
            ip=request.client.host if request.client else None,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Application already configured") from None
    from lockbot.backend.app.feishu.service import invalidate_application_clients

    invalidate_application_clients(
        row.app_id, secret=body.app_secret.get_secret_value(), credential_guard=runtime_guard(row)
    )
    return {"id": row.id, "name": row.name, "app_id": row.app_id, "transport": row.transport}


@router.post("/applications/{application_id}/connect")
def connect_application(
    application_id: int,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.feishu.connectors import connectors

    app = db.get(FeishuApplication, application_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    if app.transport != "websocket":
        raise HTTPException(409, "Application uses HTTP callbacks")
    try:
        connectors.connect(application_id, factory=sessionmaker(bind=db.get_bind()))
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"started": True}


@router.get("/bots/{bot_id}/query-panel")
def query_panel_status(
    bot_id: int, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from lockbot.backend.app.bots.router import _get_user_bot
    from lockbot.backend.app.feishu.models import FeishuQueryPanel

    bot = _get_user_bot(bot_id, user, db)
    if bot.platform.lower() != "feishu":
        raise HTTPException(404, "Feishu bot not found")
    response.headers["Cache-Control"] = "no-store"
    row = db.get(FeishuQueryPanel, bot_id)
    return {
        "enabled": json.loads(bot.config_overrides or "{}").get("FEISHU_QUERY_PANEL", False) is True,
        "status": row.status if row else "not_verified",
        "reason": row.reason if row else "",
        "updated_at": row.updated_at if row else None,
        "edit_count": row.edit_count if row else 0,
        "retry_after": row.retry_after if row else None,
    }


@router.get("/bots/{bot_id}/group")
@limiter.limit("30/minute")
def bot_group_info(
    bot_id: int,
    request: Request,
    refresh: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.bots.router import _get_user_bot
    from lockbot.backend.app.feishu.service import application_client

    bot = _get_user_bot(bot_id, user, db)
    route = db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first()
    if bot.platform.lower() != "feishu" or route is None or route.chat_id != bot.group_id:
        raise HTTPException(404, "Feishu group route not found")
    app = db.get(FeishuApplication, route.application_id)
    if app is None:
        raise HTTPException(404, "Feishu application not found")
    from lockbot.backend.app.feishu.membership import membership_status

    result = {"chat_id": route.chat_id, **application_client(app).group_name(route.chat_id, refresh)}
    observation = membership_status(db, app, bot, route.chat_id)
    if observation is not None:
        result["membership"] = observation
    return result


@router.get("/applications/{application_id}/groups/{chat_id}")
@limiter.limit("30/minute")
def preview_group_info(
    application_id: int,
    chat_id: str,
    request: Request,
    refresh: bool = False,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from lockbot.backend.app.feishu.service import application_client

    if not re.fullmatch(r"oc_[A-Za-z0-9_]+", chat_id):
        raise HTTPException(422, "Invalid Feishu group ID")
    app = db.get(FeishuApplication, application_id)
    if app is None:
        raise HTTPException(404, "Feishu application not found")
    return {"chat_id": chat_id, **application_client(app).group_name(chat_id, refresh)}


def _execute(factory, application_id, message, expected_uuid, expected_revision):
    """Acquire the current instance at execution time; never retain an old bot object."""
    key = (application_id, message["message_id"])
    state = "uncertain"
    try:
        if time.monotonic() - message["_accepted_at"] > 60:
            state = "expired"
            return
        with factory() as db:
            claim = db.get(FeishuReceivedMessage, key)
            bot_id = claim.bot_id
            bot = db.get(Bot, bot_id)
            app = db.get(FeishuApplication, application_id)
            if app is None:
                state = "identity_changed"
                return
            credentials_for(app)
            if not bot or bot.is_deleted or bot.uuid != expected_uuid or bot.identity_revision != expected_revision:
                state = "identity_changed"
                return
        with bot_manager.admit(bot_id) as lease:
            if lease.bot.config.get_val("_IDENTITY_REVISION") != expected_revision:
                state = "identity_changed"
                return
            if lease.bot.adapter.platform_name != "feishu":
                state = "identity_changed"
                return
            if message["group_id"] not in lease.bot.config.get_val("_AUTHORIZED_GROUP_IDS", []):
                state = "scope_refused"
                return
            # The claim is committed before invoking the shared dispatcher. Any exception
            # after this point is uncertain, never an instruction to replay a lock/renewal.
            lease.bot.config.get_val("_FEISHU_CLIENT").warm_user_name(message["user_id"])
            from lockbot.backend.app.feishu.query_panel import query_delivery
            from lockbot.core.command_result import capture_result

            with query_delivery(
                factory, bot_id, application_id, message, expected_uuid, expected_revision, lease.bot
            ) as panel:
                with capture_result() as command_result:
                    command = message["command"]
                    command_word = command.split(None, 1)[0].lower() if command else ""
                    format_error = message.get("command_format_error") or (
                        message.get("_code_block_command")
                        and command_word not in {*lease.bot.supported_commands(), *BINDING_COMMANDS}
                        and command not in lease.bot.config.get_val("CLUSTER_CONFIGS", {})
                    )
                    if format_error:
                        from lockbot.core.i18n import t

                        reply = lease.bot.adapter.build_reply(
                            t("feishu.command_format", config=lease.bot.config) + "\n\n", [message["user_id"]]
                        )
                    else:
                        reply = execute_command(message, lease.bot)
                from lockbot.backend.app.bots.router import _write_log

                try:
                    _write_log(
                        bot_id,
                        f"[{message['user_id']}] "
                        + ("[unsupported rich-text command]" if format_error else redact_command_text(command)),
                        category="command",
                        source="chat",
                    )
                except OSError:
                    logger.warning("Could not record a Feishu command for bot %s", bot_id)
                state = "delivery_uncertain"
                lease.bot.adapter.apply_reply_target(reply, message["group_id"])
                if panel is None or not panel.send(reply):
                    lease.bot.adapter.send_command_reply(reply, message, command_result)
                state = "completed"
    except (BotNotRunning, InstanceRetiring, AdmissionFull):
        state = "unavailable"
    except Exception:
        # No raw event, bind code, platform response or exception value in logs.
        logger.error("Feishu event execution or delivery failed for application %s", application_id)
    finally:
        try:
            with factory() as db:
                claim = db.get(FeishuReceivedMessage, key)
                if claim:
                    claim.state = state
                    if state == "completed":
                        row = db.get(Bot, claim.bot_id)
                        if (
                            row
                            and not row.is_deleted
                            and row.uuid == expected_uuid
                            and row.identity_revision == expected_revision
                        ):
                            from lockbot.backend.app.broadcast.service import record_group_activity

                            row.last_request_at = datetime.utcnow()
                            row.last_user_id = message["user_id"]
                            row.observed_group_ids = message["group_id"]
                            record_group_activity(db, row, message["group_id"])
                    db.commit()
        finally:
            _capacity.release()


def accept_event(db, app, data):
    from lockbot.core.platforms.feishu_events import MEMBERSHIP_EVENTS

    if data.get("header", {}).get("event_type") in MEMBERSHIP_EVENTS:
        from lockbot.backend.app.feishu.membership import accept_membership

        return accept_membership(db, app, data)
    message = normalize_message(data, application_identity(app))
    if message is None:
        return {"accepted": False}
    message["_accepted_at"] = time.monotonic()
    route = db.query(FeishuGroupRoute).filter_by(application_id=app.id, chat_id=message["group_id"]).first()
    if route is None:
        from lockbot.backend.app.feishu.unbound import queue_hint

        queue_hint(db, app, message, tenant=app.tenant_key)
        return {"accepted": False}
    bot = db.get(Bot, route.bot_id)
    if not bot or bot.is_deleted or bot.platform.lower() != "feishu" or bot.group_id != route.chat_id:
        return {"accepted": False}
    from lockbot.backend.app.feishu.traffic import ingress_budget

    if not ingress_budget.claim(app.id, message["user_id"]):
        return {"accepted": False, "reason": "rate_limited"}
    if not _capacity.acquire(blocking=False):
        raise HTTPException(503, "Feishu receiver busy", headers={"Retry-After": "2"})
    submitted = False
    try:
        # Keep a one-day claim window and reject stale messages before claiming (normalizer).
        db.query(FeishuReceivedMessage).filter(
            FeishuReceivedMessage.created_at < datetime.utcnow() - timedelta(days=1)
        ).delete()
        db.add(
            FeishuReceivedMessage(
                application_id=app.id,
                message_id=message["message_id"],
                bot_id=bot.id,
                identity_revision=bot.identity_revision,
                state="claimed",
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"accepted": True, "duplicate": True}
        factory = sessionmaker(bind=db.get_bind())
        _executor.submit(_execute, factory, app.id, message, bot.uuid, bot.identity_revision)
        submitted = True
        return {"accepted": True}
    finally:
        if not submitted:
            _capacity.release()


@router.post("/events/{application_id}")
@limiter.limit("120/minute")
async def receive(application_id: int, request: Request, db: Session = Depends(get_db)):
    app = db.get(FeishuApplication, application_id)
    if app is None or app.transport != "http":
        raise HTTPException(404, "HTTP receiver not configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256 * 1024:
            raise HTTPException(413, "Event too large")
    try:
        data = verify_http(bytes(body), request.headers, application_identity(app))
        if "challenge" in data:
            return data
        return await asyncio.to_thread(accept_event, db, app, data)
    except InvalidFeishuEvent:
        raise HTTPException(401, "Invalid Feishu event") from None
