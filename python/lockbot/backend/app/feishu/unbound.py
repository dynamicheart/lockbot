"""Bounded, best-effort guidance for verified mentions in an unassociated group."""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from sqlalchemy.orm import sessionmaker

from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute

logger = logging.getLogger(__name__)


def hint_text(factory, chat_id):
    from lockbot.backend.app.settings.router import _get_setting

    with factory() as db:
        base = (_get_setting(db, "platform_url") or "").strip().rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and not any(char.isspace() for char in base)
        )
    except ValueError:
        valid = False
    location = f"\n后台：{base}" if valid else "\n请联系管理员获取本站后台地址。"
    return (
        "本群尚未关联资源机器人。请管理员在本站后台创建飞书机器人，选择企业应用并填写本群 Chat ID。"
        f"{location}\n资源群 Chat ID：{chat_id}"
    )


def fingerprint(app):
    # Compare persisted routing/credential identity without putting secrets in queued work.
    values = [
        getattr(app, key)
        for key in (
            "id",
            "app_id",
            "tenant_key",
            "bot_open_id",
            "app_secret",
            "verification_token",
            "encrypt_key",
            "transport",
        )
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


class UnboundHints:
    def __init__(self, *, executor=None, clock=time.monotonic, capacity=16):
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-unbound")
        self.clock = clock
        self.capacity = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.messages, self.groups, self.apps = OrderedDict(), OrderedDict(), OrderedDict()
        self.last_global = None

    def enqueue(self, db, app, message, *, tenant, allow_unknown_tenant=False, membership_event_id=None):
        # Only router/service callers that just normalized the actual event may enter here.
        now = self.clock()
        app_id, chat_id, message_id = app.id, message["group_id"], message["message_id"]
        if not isinstance(chat_id, str) or not 0 < len(chat_id) <= 128:
            db.rollback()
            return False
        key, group = (app_id, message_id), (app_id, chat_id)
        job = (app_id, fingerprint(app), tenant, allow_unknown_tenant, chat_id, now, membership_event_id)
        factory = sessionmaker(bind=db.get_bind())
        # No request DB transaction can overlap worker token lookup or sending.
        db.rollback()
        with self.lock:
            if (
                (key in self.messages and now - self.messages[key] < 600)
                or (group in self.groups and now - self.groups[group] < 60)
                or (app_id in self.apps and now - self.apps[app_id] < 10)
                or (self.last_global is not None and now - self.last_global < 1)
                or not self.capacity.acquire(blocking=False)
            ):
                return False
            for cache, item, bound in ((self.messages, key, 2048), (self.groups, group, 512), (self.apps, app_id, 128)):
                cache[item] = now
                cache.move_to_end(item)
                while len(cache) > bound:
                    cache.popitem(last=False)
            self.last_global = now
        try:
            self.executor.submit(self._deliver, factory, job)
        except Exception:
            self.capacity.release()
            return False
        return True

    def _current(self, factory, job, *, build_client=False):
        app_id, expected, tenant, allow_unknown, chat_id, _accepted_at, membership_event_id = job
        with factory() as db:
            app = db.get(FeishuApplication, app_id)
            if app is None or fingerprint(app) != expected:
                return None
            if membership_event_id:
                from lockbot.backend.app.feishu.membership import greeting_enabled
                from lockbot.backend.app.feishu.models import FeishuGroupMembership

                observation = db.get(FeishuGroupMembership, (app_id, chat_id))
                if (
                    not greeting_enabled(db, app_id)
                    or observation is None
                    or observation.event_id != membership_event_id
                    or observation.status != "present"
                    or observation.tenant_key != tenant
                    or observation.app_id != app.app_id
                ):
                    return None
            if app.tenant_key != tenant and not (allow_unknown and app.transport == "websocket" and not app.tenant_key):
                return None
            if db.query(FeishuGroupRoute.id).filter_by(application_id=app_id, chat_id=chat_id).first():
                return None
            if build_client:
                from lockbot.backend.app.feishu.service import application_client

                return application_client(app)  # Construction/cache lookup performs no HTTP.
            return True

    def _deliver(self, factory, job):
        app_id, _expected, _tenant, _allow_unknown, chat_id, accepted_at, _membership_event_id = job
        try:
            if self.clock() - accepted_at > 30:
                return
            client = self._current(factory, job, build_client=True)
            if client is None:
                return
            # Cold-token refresh can block; recheck identity/route after it, with no DB
            # transaction held during either refresh or the final message POST.
            client.token()
            if self.clock() - accepted_at > 30 or not self._current(factory, job):
                return
            text = hint_text(factory, chat_id)
            client.send(
                {
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                }
            )
        except Exception:
            # Ambiguous delivery is never replayed; no command, bind code or platform body logged.
            logger.warning("Feishu unassociated-group hint was not confirmed for application %s", app_id)
        finally:
            self.capacity.release()


unbound_hints = UnboundHints()


def queue_hint(db, app, message, *, tenant, allow_unknown_tenant=False, membership_event_id=None):
    return unbound_hints.enqueue(
        db,
        app,
        message,
        tenant=tenant,
        allow_unknown_tenant=allow_unknown_tenant,
        membership_event_id=membership_event_id,
    )
