"""Optional own-message query panels. No resource operation or network retry lives here."""

import json
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta

from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute, FeishuQueryPanel
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuPanelAPIError

# Local conservative rollover, not a promise about the enterprise's editable age.
MAX_EDITS = 19
MAX_AGE = timedelta(minutes=15)
COOLDOWN = timedelta(minutes=5)
_locks_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()


class _Mutex:
    def __init__(self):
        self.lock = threading.Lock()


def invalidate_query_panel(db, bot_id):
    """Invalidate within the caller's transaction, including on membership changes."""
    row = db.get(FeishuQueryPanel, bot_id)
    if row:
        row.generation += 1
        row.message_id = None
        row.status = "invalidated"
        row.reason = "membership_changed"
        row.retry_after = None


def _scope(db, bot_id, application_id, expected_uuid, revision, chat_id):
    bot = db.get(Bot, bot_id)
    app = db.get(FeishuApplication, application_id)
    route = db.query(FeishuGroupRoute).filter_by(application_id=application_id, chat_id=chat_id, bot_id=bot_id).first()
    if (
        not bot
        or bot.is_deleted
        or bot.platform.lower() != "feishu"
        or bot.status != "running"
        or bot.uuid != expected_uuid
        or bot.identity_revision != revision
        or bot.group_id != chat_id
        or not app
        or not route
    ):
        raise FeishuAPIError("Feishu query panel scope changed")
    identity = json.dumps(
        [application_id, app.app_id, app.tenant_key, app.bot_open_id, chat_id, bot.uuid, revision, bot.user_id],
        separators=(",", ":"),
    )
    enabled = json.loads(bot.config_overrides or "{}").get("FEISHU_QUERY_PANEL", False) is True
    return identity, enabled, app.app_id


class QueryPanel:
    def __init__(self, factory, bot_id, application_id, expected_uuid, revision, chat_id, adapter):
        self.factory = factory
        self.args = (bot_id, application_id, expected_uuid, revision, chat_id)
        self.adapter = adapter

    def send(self, reply):
        """True means handled; False delegates the original reply to normal delivery."""
        prepared = self.adapter.query_panel_payload(reply)
        if prepared is None:
            return False
        client, payload = prepared
        now = datetime.utcnow()
        with self.factory() as db:
            scope, enabled, app_id = _scope(db, *self.args)
            if not enabled:
                return False
            if client.app_id != app_id:
                raise FeishuAPIError("Feishu query panel client changed")
            row = db.get(FeishuQueryPanel, self.args[0])
            if row is None:
                row = FeishuQueryPanel(
                    bot_id=self.args[0],
                    scope=scope,
                    generation=0,
                    updated_at=now,
                    status="new",
                    edit_count=0,
                    reason="",
                )
                db.add(row)
            if row.scope != scope:
                row.scope, row.message_id, row.created_at, row.edit_count = scope, None, None, 0
                row.retry_after = None
            if row.retry_after and row.retry_after > now:
                return False  # Restart/in-flight uncertainty or bounded failure cooldown.
            message_id = row.message_id
            if row.edit_count >= MAX_EDITS or not row.created_at or now - row.created_at >= MAX_AGE:
                message_id = None
            row.generation += 1
            generation = row.generation
            row.status = "updating" if message_id else "creating"
            row.updated_at, row.retry_after = now, now + COOLDOWN
            db.commit()  # Release SQLite transaction before any token/API request.
        reason = ""
        uncertain = False
        try:
            if message_id:
                client.update_query_panel(message_id, payload)
                result_id = message_id
            else:
                result_id = client.create_query_panel(payload)
                if result_id is None:
                    reason = "accepted_untracked"
        except FeishuPanelAPIError as exc:
            uncertain = exc.uncertain
            reason = (
                "delivery_uncertain"
                if uncertain
                else {
                    230027: "permission_missing",
                    230011: "message_removed",
                    230110: "message_removed",
                    230075: "message_too_old",
                    230072: "edit_limit",
                    230020: "rate_limited",
                    230002: "not_in_group",
                    230071: "not_sender",
                    230035: "speaking_denied",
                }.get(exc.code, "platform_rejected")
            )
            result_id = None
        except FeishuAPIError:
            uncertain, reason, result_id = True, "delivery_uncertain", None
        with self.factory() as db:
            current_scope, enabled, _ = _scope(db, *self.args)
            row = db.get(FeishuQueryPanel, self.args[0])
            if not row or current_scope != scope or row.scope != scope or row.generation != generation or not enabled:
                raise FeishuAPIError("Feishu query panel changed during delivery")
            row.updated_at, row.reason = datetime.utcnow(), reason
            if result_id:
                row.message_id = result_id
                row.edit_count = row.edit_count + 1 if message_id else 0
                row.created_at = row.created_at if message_id else now
                row.status, row.retry_after = "active", None
            else:
                row.message_id = None
                row.status = "fallback"
            db.commit()
        if reason == "delivery_uncertain" and not message_id:
            # A create may already have been accepted. Never duplicate it automatically.
            raise FeishuAPIError("Feishu query panel delivery uncertain")
        return not reason or reason == "accepted_untracked"


@contextmanager
def query_delivery(factory, bot_id, application_id, message, expected_uuid, revision, bot):
    """Serialize only opted-in full-query snapshots; never acquire the resource state lock."""
    command = message.get("command", "").strip().lower()
    enabled = bot.config.get_val("FEISHU_QUERY_PANEL", False) is True
    if not enabled or command not in ("", "query"):
        yield None
        return
    key = (factory.kw["bind"], bot_id)
    with _locks_guard:
        mutex = _locks.get(key)
        if mutex is None:
            mutex = _Mutex()
            _locks[key] = mutex
    # A busy panel is bounded; don't queue stale snapshots or replay commands.
    if not mutex.lock.acquire(timeout=15):
        raise FeishuAPIError("Feishu query panel is busy")
    try:
        yield QueryPanel(factory, bot_id, application_id, expected_uuid, revision, message["group_id"], bot.adapter)
    finally:
        mutex.lock.release()
