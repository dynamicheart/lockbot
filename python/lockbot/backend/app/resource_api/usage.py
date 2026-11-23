"""Lease-scoped task categories and readers for historical declarations."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from lockbot.backend.app.database import Base
from lockbot.backend.app.resource_api.privacy import visible_declarations as visible_declarations
from lockbot.backend.app.resource_api.task_categories import TASK_CATEGORIES, validate_task_category
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.core.platforms.registry import normalize_platform_name


class UsageDeclaration(Base):
    __tablename__ = "resource_usage_declarations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("resource_bindings.id"), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Existing databases require an empty purpose for new category records.
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    agent_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    holding_proof: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    __table_args__ = (UniqueConstraint("bot_id", "binding_id", "resource_id"),)


def _resource_node(state, resource_id):
    if resource_id in state:
        return state[resource_id]
    node_key, marker, card = resource_id.rpartition(":dev")
    if (
        marker
        and card.isascii()
        and card.isdecimal()
        and str(int(card)) == card
        and isinstance(state.get(node_key), (list, tuple))
    ):
        return next((entry for entry in state[node_key] if entry.get("dev_id") == int(card)), None)
    return None


def holding_proof(state, resource_id, user_id, *, now=None):
    """Tie a declaration to one actual hold/booking, not a reusable node alias."""
    node = _resource_node(state, resource_id)
    if node is None:
        return None
    entries = [node] if isinstance(node, Mapping) else node
    evidence = []
    now = time.time() if now is None else now
    for index, entry in enumerate(entries):
        for holder in entry.get("current_users", []):
            if holder["user_id"] == user_id and holder.get("start_time", 0) + holder.get("duration", 0) > now:
                evidence.append(("hold", index, holder["start_time"], holder.get("holding_id", "legacy")))
        for holder in entry.get("booking_list", []):
            if holder["user_id"] == user_id:
                evidence.append(
                    (
                        "booking",
                        index,
                        holder.get("booked_start_time", holder.get("start_time", 0)),
                        holder.get("holding_id", "legacy"),
                    )
                )
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() if evidence else None


def declaration_revision(row):
    """Opaque comparison value for a private declaration; never an authorization proof."""
    fields = [
        row.id,
        row.bot_id,
        row.binding_id,
        row.resource_id,
        row.holding_proof,
        row.updated_at.isoformat(),
        row.expires_at.isoformat(),
        row.task_category,
        row.purpose,
        row.agent_label,
    ]
    return '"' + hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest() + '"'


def _payload(row, binding):
    return {
        "resource_id": row.resource_id,
        "purpose": row.purpose,
        "task_category": row.task_category,
        "public_task": TASK_CATEGORIES.get(row.task_category),
        "untrusted_text": True,
        "actor": {"platform": binding.platform, "external_user_id": binding.external_user_id},
        "binding_id": binding.public_id,
        "agent_label": row.agent_label,
        "authorization_label": binding.label,
        "revision": declaration_revision(row),
        "updated_at": row.updated_at.isoformat() + "Z",
        "expires_at": row.expires_at.isoformat() + "Z",
    }


def active_declarations(db, bot, now, *, limit=500, offset=0):
    """One bounded query, scoped to the current bot identity and live credentials."""
    return (
        db.query(UsageDeclaration, ResourceBinding)
        .join(ResourceBinding, UsageDeclaration.binding_id == ResourceBinding.id)
        .filter(
            UsageDeclaration.bot_id == bot.id,
            UsageDeclaration.expires_at > now,
            ResourceBinding.bound_bot_id == bot.id,
            ResourceBinding.bound_bot_uuid == bot.uuid,
            ResourceBinding.bound_identity_revision == bot.identity_revision,
            ResourceBinding.platform == normalize_platform_name(bot.platform),
            ResourceBinding.revoked_at.is_(None),
            (ResourceBinding.credential_expires_at.is_(None)) | (ResourceBinding.credential_expires_at > now),
        )
        .order_by(UsageDeclaration.updated_at.desc(), UsageDeclaration.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def declarations_for_state(rows, state, captured_at):
    """Match the actual hold/booking incarnation, not just a resource or user name."""
    return [
        _payload(row, binding)
        for row, binding in rows
        if holding_proof(state, row.resource_id, binding.external_user_id, now=captured_at.timestamp())
        == row.holding_proof
    ]


def attach_declarations(resources, declarations):
    by_owner = {(item["resource_id"], item["actor"]["external_user_id"]): item for item in declarations}
    for resource in resources:
        entries = list(resource["holders"]) + list(resource.get("booking_list", []))
        for entry in entries:
            entry["usage"] = by_owner.get((resource["id"], entry["external_user_id"]))
        for device in resource.get("devices", []):
            for entry in list(device["holders"]) + list(device.get("booking_list", [])):
                entry["usage"] = by_owner.get((device.get("resource_id", resource["id"]), entry["external_user_id"]))


def store_declaration(db, *, bot_id, binding_id, resource_id, proof, now, ttl_seconds, task_category):
    values = {
        "bot_id": bot_id,
        "binding_id": binding_id,
        "resource_id": resource_id,
        "purpose": "",
        "task_category": validate_task_category(task_category),
        "agent_label": None,
        "holding_proof": proof,
        "updated_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    db.execute(
        insert(UsageDeclaration)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[UsageDeclaration.bot_id, UsageDeclaration.binding_id, UsageDeclaration.resource_id],
            set_={
                key: values[key]
                for key in ("purpose", "agent_label", "task_category", "holding_proof", "updated_at", "expires_at")
            },
        )
    )


def prepare_command_usage(bot, actor, word, text):
    """Use the bot's real parser; targets never come from display metadata."""
    if word not in ("lock", "slock", "book"):
        return None
    parsed = bot.parse_command(actor.external_user_id, word, text, True)
    if not parsed[0]:
        return None
    _, state = bot.state_snapshot()
    return {resource: _resource_node(state, resource) for resource in parsed[2]}


def _own_entries(node, user):
    nodes = node if isinstance(node, list) else [node]
    return [
        (index, kind, item.get("holding_id"), item.get("start_time"), item.get("duration"))
        for index, entry in enumerate(nodes)
        for kind in ("current_users", "booking_list")
        for item in entry.get(kind, [])
        if item["user_id"] == user
    ]


def register_command_usage(db, bot, actor, before, *, task_category=None, task_category_provided=False):
    """Persist preset categories only for targets whose own hold or booking changed.

    Locks and SQLite are distinct stores. If this commit fails, the caller reports
    may_have_executed and requires a read; it must never blindly replay the resource mutation.
    """
    if before is None:
        return
    captured_at, state = bot.state_snapshot()
    now = datetime.utcnow()
    changed = False
    for resource_id, old_node in before.items():
        node = _resource_node(state, resource_id)
        if node is None or _own_entries(old_node, actor.external_user_id) == _own_entries(node, actor.external_user_id):
            continue
        proof = holding_proof(state, resource_id, actor.external_user_id, now=captured_at.timestamp())
        if proof is None:
            continue
        old = (
            db.query(UsageDeclaration)
            .filter_by(bot_id=actor.bot_id, binding_id=actor.binding_record_id, resource_id=resource_id)
            .one_or_none()
        )
        old_proof = holding_proof(
            {resource_id: old_node}, resource_id, actor.external_user_id, now=captured_at.timestamp()
        )
        reuse = old is not None and old.expires_at > now and old.holding_proof == old_proof
        category = old.task_category if reuse and not task_category_provided else task_category
        if category is None and (not reuse or old.task_category is None):
            continue
        store_declaration(
            db,
            bot_id=actor.bot_id,
            binding_id=actor.binding_record_id,
            resource_id=resource_id,
            task_category=category,
            proof=proof,
            now=now,
            ttl_seconds=86400,
        )
        changed = True
    if changed:
        db.query(UsageDeclaration).filter(UsageDeclaration.expires_at <= now).delete(synchronize_session=False)
        db.commit()


def prune_actor_usage(db, bot, actor):
    """Remove only this actor's declarations that no longer match a live hold/booking."""
    captured_at, state = bot.state_snapshot()
    rows = db.query(UsageDeclaration).filter_by(bot_id=actor.bot_id, binding_id=actor.binding_record_id).all()
    removed = False
    for row in rows:
        if (
            row.expires_at <= datetime.utcnow()
            or holding_proof(state, row.resource_id, actor.external_user_id, now=captured_at.timestamp())
            != row.holding_proof
        ):
            db.delete(row)
            removed = True
    if removed:
        db.commit()
