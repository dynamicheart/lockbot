"""Structured resource writes using the existing allocator and state lock."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import DateTime, ForeignKey, String, Text, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.database import Base, get_db
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api import errors
from lockbot.backend.app.resource_api.commands import AGENT_ALLOWED_COMMANDS
from lockbot.backend.app.resource_api.event_log import EventActor, begin_event
from lockbot.backend.app.resource_api.notifications import notification_target, notify_group, send_queue_delay
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_api.snapshot import QUEUE_CLAIM_WINDOW_SECONDS
from lockbot.backend.app.resource_api.task_categories import TASK_CATEGORIES, validate_task_category
from lockbot.backend.app.resource_api.v2.binding import PersonalGrant, api_error, require_principal, router
from lockbot.backend.app.resource_api.v2.identity import CARD_BOTS, NODE, ResourceIdentity, config_units
from lockbot.backend.app.resource_api.v2.resources import booking_ref, check_budget, fresh_grant
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.core.command_context import resource_command_transaction
from lockbot.core.command_result import capture_result
from lockbot.core.handler import CommandNotAllowed, CommandNotSupported, execute_text_command

logger = logging.getLogger(__name__)
_HEADERS = {"Cache-Control": "no-store"}
Ref = Annotated[str, StringConstraints(pattern=r"^res_[0-9a-f]{32}$")]
PublicTaskCategory = Literal[tuple(TASK_CATEGORIES)]


class CommandReceipt(Base):
    __tablename__ = "agent_v2_command_receipts"

    principal_id: Mapped[str] = mapped_column(ForeignKey("agent_v2_credentials.public_id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[int | None]
    payload: Mapped[str | None] = mapped_column(Text)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["lock", "slock", "book", "free", "label", "keep"]
    resource_refs: list[Ref] = Field(
        default_factory=list,
        max_length=64,
        description="Exact resource references. Required unless locking a specific booking.",
    )
    booking_ref: str | None = Field(
        default=None,
        pattern=r"^book_[0-9a-f]{32}_[0-9a-f]{24}$",
        description="Use with lock to start your ready booking. Omit resource_refs and duration.",
    )
    duration: str | None = Field(default=None, max_length=20, pattern=r"^[0-9]{1,7}(\.[0-9]{1,5})?[dhm]$")
    label: str | None = Field(default=None, max_length=15, pattern=r"^[^\r\n]*$")
    notice_ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{16}$",
        description="The notice_ref from the idle.notice event. Required for keep.",
    )
    task_category: PublicTaskCategory | None = Field(
        default=None, description="Optional public preset category for lock, slock, or book."
    )

    @model_validator(mode="after")
    def exact_targets(self):
        if self.operation == "keep":
            if (
                len(self.resource_refs) != 1
                or self.notice_ref is None
                or self.booking_ref is not None
                or self.duration is not None
                or self.label is not None
                or "task_category" in self.model_fields_set
            ):
                raise ValueError("keep requires one resource_ref and a notice_ref")
            return self
        if self.notice_ref is not None:
            raise ValueError("notice_ref is only valid for keep")
        if self.booking_ref is not None:
            if self.operation != "lock" or self.resource_refs or self.duration is not None:
                raise ValueError("booking_ref requires lock without resource_refs or duration")
        elif not self.resource_refs:
            raise ValueError("resource_refs are required")
        if len(set(self.resource_refs)) != len(self.resource_refs):
            raise ValueError("duplicated resource_refs")
        if self.operation == "label":
            if len(self.resource_refs) != 1 or self.label is None or self.duration is not None:
                raise ValueError("label requires one resource_ref and a label value")
        elif self.label is not None:
            raise ValueError("label is only valid for the label operation")
        if self.operation == "free" and self.duration is not None:
            raise ValueError("free does not take a duration")
        if "task_category" in self.model_fields_set and self.operation not in {"lock", "slock", "book"}:
            raise ValueError("task_category is only valid for lock, slock, or book")
        return self

    def refs(self):
        if self.booking_ref is not None:
            return ["res_" + self.booking_ref.split("_")[1]]
        return self.resource_refs


def _lookup(db, principal, refs):
    rows = list(db.scalars(select(ResourceIdentity).where(ResourceIdentity.ref.in_(refs))))
    if len(rows) != len(refs):
        raise api_error("resource_unavailable", 404)
    by_bot = {row.bot_id for row in rows}
    grants = list(
        db.scalars(
            select(PersonalGrant).where(
                PersonalGrant.principal_id == principal.public_id, PersonalGrant.bot_id.in_(by_bot)
            )
        )
    )
    if len(grants) != len(by_bot):
        raise api_error("resource_unavailable", 404)
    if len(by_bot) != 1:
        raise api_error("cross_bot_batch", 422)
    return grants[0]


class AllocatorCommand:
    """Exact selectors, booking checks, and readback under the allocator lock."""

    def __init__(self, body, principal, rows):
        self.body = body
        self.principal_id = principal.public_id
        self.user_id = principal.external_user_id
        self.rows = rows
        cards = defaultdict(list)
        for row in rows:
            if row.slot != NODE:
                cards[row.node].append(row.slot)
        self.cards_by_node = dict(cards)
        self.result = []

    def text(self):
        word = self.body.operation
        targets = ",".join(dict.fromkeys(row.node for row in self.rows))
        if self.cards_by_node:
            first_node, first_cards = next(iter(self.cards_by_node.items()))
            # The existing parser reads the duration; exact card sets come from cards_by_node.
            targets = f"{first_node} dev{','.join(str(card) for card in first_cards)}"
        return f"{word} {targets}" + (f" {self.body.duration}" if self.body.duration else "")

    def _entry(self, state, row):
        entry = state.get(row.node)
        if row.slot == NODE and isinstance(entry, dict):
            return entry
        if isinstance(entry, list) and 0 <= row.slot < len(entry) and entry[row.slot].get("dev_id") == row.slot:
            return entry[row.slot]
        raise api_error("resource_unavailable", 404)

    def check(self, state):
        self.categories = {}
        for row in self.rows:
            raw = self._entry(state, row)
            if self.body.operation in {"lock", "slock", "book"}:
                for entry in (*raw.get("current_users", ()), *raw.get("booking_list", ())):
                    category = validate_task_category(entry.get("task_category"))
                    if entry["user_id"] == self.user_id and category:
                        self.categories[row.ref] = category
                        break
            if self.body.booking_ref is not None:
                head = next(iter(raw.get("booking_list", [])), None)
                if (
                    raw["status"] != "idle"
                    or head is None
                    or head["user_id"] != self.user_id
                    or booking_ref(row.ref, self.principal_id, head) != self.body.booking_ref
                    or (
                        head.get("is_notified")
                        and datetime.now(timezone.utc).timestamp() >= head["start_time"] + QUEUE_CLAIM_WINDOW_SECONDS
                    )
                ):
                    raise api_error("booking_not_claimable", 409)

    def before_save(self, state):
        if self.body.operation not in {"lock", "slock", "book"}:
            return
        entries = "booking_list" if self.body.operation == "book" else "current_users"
        for row in self.rows:
            category = (
                self.body.task_category
                if "task_category" in self.body.model_fields_set
                else self.categories.get(row.ref)
            )
            for entry in self._entry(state, row).get(entries, ()):
                if entry["user_id"] == self.user_id:
                    if category is None:
                        entry.pop("task_category", None)
                    else:
                        entry["task_category"] = category

    def observe(self, state):
        self.result = []
        for row in self.rows:
            raw = self._entry(state, row)
            own_hold = next((x for x in raw.get("current_users", []) if x["user_id"] == self.user_id), None)
            own_booking = next((x for x in raw.get("booking_list", []) if x["user_id"] == self.user_id), None)
            item = {
                "resource_ref": row.ref,
                "node": row.node,
                "device": None if row.slot == NODE else row.slot,
                "status": raw["status"],
            }
            if own_hold:
                expires = datetime.fromtimestamp(own_hold["start_time"] + own_hold["duration"], tz=timezone.utc)
                item["holding"] = {"expires_at": expires.isoformat(), "mode": raw["status"]}
            if own_booking:
                item["booking_ref"] = booking_ref(row.ref, self.principal_id, own_booking)
            self.result.append(item)


def _execute(bot, allocator, principal, body, refs, db, result, event, idle_recovery=None):
    by_ref = {row.ref: row for row in db.scalars(select(ResourceIdentity).where(ResourceIdentity.ref.in_(refs)))}
    configured = config_units(bot)
    if any(ref not in by_ref or by_ref[ref].bot_id != bot.id for ref in refs):
        raise api_error("resource_unavailable", 404)
    rows = [by_ref[ref] for ref in refs]
    if any(configured.get((row.node, row.slot)) != row.fingerprint for row in rows):
        raise api_error("resource_unavailable", 404)
    if any((row.slot != NODE) != (bot.bot_type in CARD_BOTS) for row in rows):
        raise api_error("resource_unavailable", 404)
    event["requested_resources"] = [
        {"resource_id": row.node, **({"device_ids": [row.slot]} if row.slot != NODE else {})} for row in rows
    ]
    if body.booking_ref is not None and "book" not in allocator.supported_commands():
        raise api_error("command_not_supported", 422)

    if body.operation == "keep":
        row = rows[0]
        resource = (bot.id, row.node, None if row.slot == NODE else row.slot)
        notice = (
            idle_recovery.confirm(
                allocator, bot.id, principal.external_user_id, body.notice_ref, channel="api", resource=resource
            )
            if idle_recovery
            else None
        )
        if notice is None:
            raise api_error("idle_notice_unavailable", 409)
        return 200, {
            "operation": "keep",
            "status": "completed",
            "resource_ref": row.ref,
            "review_at": datetime.fromtimestamp(notice.review_at, tz=timezone.utc).isoformat(),
        }

    if body.operation == "label":
        row = rows[0]
        if not allocator.config.get_val("ALLOW_USER_LABEL"):
            raise api_error("labels_disabled", 403)
        reply = execute_text_command(
            principal.external_user_id,
            f"label {row.node} {body.label}",
            allocator,
            allowed_commands=AGENT_ALLOWED_COMMANDS,
            source="api",
        )
        if not result.committed:
            return 409, {"code": "command_rejected", "message": allocator.adapter.extract_reply_text(reply)[:2048]}
        snapshot_cache.invalidate(bot.id)
        return 200, {
            "operation": "label",
            "status": "completed",
            "resources": [
                {
                    "resource_ref": row.ref,
                    "node": row.node,
                    "device": None if row.slot == NODE else row.slot,
                    "label": body.label.strip(),
                }
            ],
        }

    transaction = AllocatorCommand(body, principal, rows)
    token = resource_command_transaction.set(transaction)
    try:
        reply = execute_text_command(
            principal.external_user_id,
            transaction.text(),
            allocator,
            allowed_commands=AGENT_ALLOWED_COMMANDS,
            source="api",
        )
    finally:
        resource_command_transaction.reset(token)
    if not result.committed:
        return 409, {"code": "command_rejected", "message": allocator.adapter.extract_reply_text(reply)[:2048]}
    snapshot_cache.invalidate(bot.id)
    return 200, {"operation": body.operation, "status": "completed", "resources": transaction.result}


def _notification_command(body, resources):
    word = body.operation
    if word == "label":
        return word, f"label {resources[0]['node']}"
    by_node = defaultdict(list)
    for row in resources:
        node = by_node[row["node"]]
        if row["device"] is not None:
            node.append(str(row["device"]))
    device_sets = {tuple(devices) for devices in by_node.values()}
    if len(device_sets) == 1:
        devices = next(iter(device_sets))
        targets = ",".join(by_node) + (f" dev{','.join(devices)}" if devices else "")
    else:
        targets = " | ".join(f"{node} dev{','.join(devices)}" for node, devices in by_node.items())
    command = f"{word} {targets}" + (f" {body.duration}" if body.duration else "")
    return word, command


def _queue_notification(bot, lease, body, payload, user_id, db):
    word, command = _notification_command(body, payload["resources"])
    try:
        notify_group(
            lease.bot,
            bot_id=bot.id,
            command=command,
            actor_id=user_id,
            command_word=word,
            task_category=body.task_category,
            target=notification_target(bot, lease, bot_manager, sessionmaker(bind=db.get_bind())),
            defer=True,
        )
    except Exception:
        logger.warning("Agent v2 command completed but its group notification could not be queued")


def _start_receipt(db, principal_id, request_id, digest):
    receipt = db.get(CommandReceipt, (principal_id, request_id))
    if receipt and receipt.digest != digest:
        raise api_error("idempotency_key_reused", 409)
    if receipt and receipt.status is None:
        raise api_error("may_have_executed", 409)
    if receipt:
        return receipt, JSONResponse(json.loads(receipt.payload), status_code=receipt.status, headers=_HEADERS)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        delete(CommandReceipt).where(
            CommandReceipt.principal_id == principal_id, CommandReceipt.created_at < now - timedelta(days=1)
        )
    )
    receipt = CommandReceipt(principal_id=principal_id, request_id=request_id, digest=digest, created_at=now)
    db.add(receipt)
    db.commit()
    return receipt, None


@router.post("/commands")
@limiter.limit("60/minute")
def execute_command(
    request: Request,
    body: Command,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")] = None,
):
    """Run an operation on exact authorized resources. Lock can also target one ready booking."""
    principal = require_principal(request, db)
    refs = body.refs()
    grant = _lookup(db, principal, refs)
    principal_id, grant_id, bot_id = principal.public_id, grant.public_id, grant.bot_id
    platform, user_id = principal.platform, principal.external_user_id
    check_budget(principal_id, "write")
    try:
        with bot_manager.admit(bot_id) as lease, authority_guard(bot_id, platform=platform, external_user_id=user_id):
            principal, _, bot = fresh_grant(db, request, principal_id, grant_id)
            request.state.v2_event_actor = EventActor(
                bot_id=bot_id, platform=platform, external_user_id=user_id, binding_public_id=principal_id
            )
            event = begin_event(
                request,
                bot=bot,
                operation="command",
                command=body.operation,
                task_category=body.task_category,
                api_version=2,
            )
            event["requested_resources"] = refs
            receipt = None
            if idempotency_key:
                excluded = {"label"} if body.label is None else set()
                if body.notice_ref is None:
                    excluded.add("notice_ref")
                if "task_category" not in body.model_fields_set:
                    excluded.add("task_category")
                serialized = body.model_dump_json(exclude=excluded)
                body_digest = hashlib.sha256(serialized.encode()).hexdigest()
                try:
                    receipt, replay = _start_receipt(db, principal_id, idempotency_key, body_digest)
                except HTTPException as exc:
                    if exc.detail == {"code": "may_have_executed"}:
                        event["stage"] = "dispatching"
                    raise
                if replay is not None:
                    event["stage"] = "replayed"
                    return replay
            try:
                observer = getattr(request.app.state, "idle_observer", None)
                with capture_result() as result:
                    status, payload = _execute(
                        bot,
                        lease.bot,
                        principal,
                        body,
                        refs,
                        db,
                        result,
                        event,
                        idle_recovery=observer.recovery if observer else None,
                    )
                if not bot_manager.lease_is_current(lease):
                    raise api_error("may_have_executed", 503)
            except (CommandNotAllowed, CommandNotSupported):
                status, payload = 422, {"code": "command_not_supported"}
            except HTTPException as exc:
                status, payload = exc.status_code, exc.detail
            except Exception:
                event["stage"] = "dispatching"
                logger.exception("Agent v2 command outcome unavailable")
                db.rollback()
                raise api_error("may_have_executed", 503) from None
            if status == 200:
                event["stage"] = "committed"
            elif payload == {"code": "may_have_executed"}:
                event["stage"] = "dispatching"
            if receipt is not None:
                receipt.status, receipt.payload = status, json.dumps(payload)
                try:
                    db.commit()
                except Exception:
                    logger.exception("Agent v2 command receipt could not be saved")
                    raise api_error("may_have_executed", 503) from None
            if status == 200 and body.operation != "keep" and bot.resource_api_notify_enabled:
                _queue_notification(bot, lease, body, payload, user_id, db)
            if status == 200 and result.queue_delay_recipients:
                background_tasks.add_task(
                    send_queue_delay, bot_manager, bot_id, lease.generation, result.queue_delay_recipients
                )
            return JSONResponse(payload, status_code=status, headers=_HEADERS)
    except (AdmissionFull, BotNotRunning, InstanceRetiring) as exc:
        raise errors.admission_http_error(exc, bot_id) from None
