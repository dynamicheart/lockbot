"""Optional bot membership observations; never mutate resource state or routes."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from lockbot.backend.app.bots.models import Bot, BotLog
from lockbot.backend.app.feishu.models import (
    FeishuApplication,
    FeishuGroupMembership,
    FeishuGroupRoute,
    FeishuMembershipEvent,
    FeishuMembershipPolicy,
)
from lockbot.core.platforms.feishu_events import normalize_membership


def greeting_enabled(db, application_id):
    policy = db.get(FeishuMembershipPolicy, application_id)
    return policy is not None and policy.unbound_greeting_enabled is True


def set_greeting_policy(db, application_id, enabled):
    policy = db.get(FeishuMembershipPolicy, application_id)
    if policy is None:
        db.add(FeishuMembershipPolicy(application_id=application_id, unbound_greeting_enabled=enabled))
    else:
        policy.unbound_greeting_enabled = enabled


def bot_option_enabled(bot, key):
    try:
        overrides = json.loads(bot.config_overrides or "{}")
        return isinstance(overrides, dict) and overrides.get(key) is True
    except (TypeError, ValueError):
        return False


def notices_enabled(bot):
    return bot_option_enabled(bot, "FEISHU_MEMBERSHIP_NOTICES")


def invalidate_optional_panel(db, bot_id):
    """The separate queue-panel feature owns its message IDs and generations."""
    from importlib import import_module

    name = "lockbot.backend.app.feishu.query_panel"
    try:
        module = import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return
    module.invalidate_query_panel(db, bot_id)


def membership_status(db, app, bot, chat_id):
    if not notices_enabled(bot):
        return None
    row = db.get(FeishuGroupMembership, (app.id, chat_id))
    if row is None or (row.app_id, row.tenant_key, row.bot_uuid, row.identity_revision) != (
        app.app_id,
        app.tenant_key,
        bot.uuid,
        bot.identity_revision,
    ):
        return None
    return {
        "status": row.status,
        "observed_at": datetime.fromtimestamp(row.event_time_ms / 1000, timezone.utc).isoformat(),
    }


def accept_membership(db, app, data, *, allow_unknown_tenant=False):
    """Called only after HTTP verification or from the official private SDK pipe."""
    from lockbot.backend.app.feishu.connectors import connectors
    from lockbot.backend.app.feishu.service import application_identity
    from lockbot.backend.app.feishu.unbound import fingerprint, queue_hint

    identity = application_identity(app)
    if not identity.tenant_key and allow_unknown_tenant and app.transport == "websocket":
        tenant = data.get("header", {}).get("tenant_key")
        if isinstance(tenant, str) and 0 < len(tenant) <= 128:
            identity = replace(identity, tenant_key=tenant)
    event = normalize_membership(data, identity)
    if event is None:
        return {"accepted": False}
    application_id, expected = app.id, fingerprint(app)
    db.rollback()
    with connectors.application_guard():
        app = db.get(FeishuApplication, application_id)
        if app is None or fingerprint(app) != expected:
            return {"accepted": False}
        route = db.query(FeishuGroupRoute).filter_by(application_id=app.id, chat_id=event["group_id"]).first()
        bot = db.get(Bot, route.bot_id) if route else None
        if route and (
            bot is None or bot.is_deleted or bot.platform.lower() != "feishu" or bot.group_id != route.chat_id
        ):
            return {"accepted": False}
        if (bot is not None and not (notices_enabled(bot) or bot_option_enabled(bot, "FEISHU_QUERY_PANEL"))) or (
            bot is None and not greeting_enabled(db, app.id)
        ):
            return {"accepted": False}
        claim_key = (app.id, event["event_id"])
        if db.get(FeishuMembershipEvent, claim_key) is not None:
            return {"accepted": True, "duplicate": True}
        db.query(FeishuMembershipEvent).filter(
            FeishuMembershipEvent.created_at < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        ).delete()
        db.query(FeishuGroupMembership).filter(
            FeishuGroupMembership.bot_uuid.is_(None),
            FeishuGroupMembership.event_time_ms < event["event_time_ms"] - 30 * 86400 * 1000,
        ).delete()
        db.add(FeishuMembershipEvent(application_id=app.id, event_id=event["event_id"]))
        key = (app.id, event["group_id"])
        state = db.get(FeishuGroupMembership, key)
        # Within a millisecond, removed wins over present. Event IDs provide a stable
        # tie breaker without permitting a late join to erase a newer removal.
        event_order = (event["event_time_ms"], event["status"] == "removed", event["event_id"])
        same_scope = state is not None and (
            state.app_id,
            state.tenant_key,
            state.bot_uuid,
            state.identity_revision,
        ) == (app.app_id, identity.tenant_key, bot.uuid if bot else None, bot.identity_revision if bot else None)
        stale = same_scope and event_order <= (state.event_time_ms, state.status == "removed", state.event_id)
        if not stale:
            if state is None:
                state = FeishuGroupMembership(application_id=app.id, chat_id=event["group_id"])
                db.add(state)
            state.app_id, state.tenant_key = app.app_id, identity.tenant_key
            state.bot_uuid, state.identity_revision = (bot.uuid, bot.identity_revision) if bot else (None, None)
            state.status, state.event_time_ms, state.event_id = (
                event["status"],
                event["event_time_ms"],
                event["event_id"],
            )
            if bot is not None:
                invalidate_optional_panel(db, bot.id)
            if bot is not None and notices_enabled(bot) and event["status"] == "removed":
                # Visible only through the existing scoped bot-log surface. The message
                # contains no raw event, operator contact, task body or other-group ID.
                db.add(
                    BotLog(
                        bot_id=bot.id,
                        category="system",
                        level="WARNING",
                        message="Feishu bot removed from its resource group. Add it back to restore group messaging.",
                    )
                )
        try:
            db.commit()  # Replay claim and visible observation commit together before ack.
        except IntegrityError:
            db.rollback()
            return {"accepted": True, "duplicate": True}
        if not stale and bot is None and event["status"] == "present":
            queue_hint(
                db,
                app,
                {"group_id": event["group_id"], "message_id": "membership:" + event["event_id"]},
                tenant=identity.tenant_key,
                allow_unknown_tenant=allow_unknown_tenant,
                membership_event_id=event["event_id"],
            )
        return {"accepted": True, **({"ignored": "older_observation"} if stale else {})}
