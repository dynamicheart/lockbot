import hashlib
import threading
from collections import OrderedDict
from dataclasses import replace

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.core.platforms.feishu import FeishuClient
from lockbot.core.platforms.feishu_events import FeishuIdentity, InvalidFeishuEvent, normalize_message

_clients = OrderedDict()
_clients_lock = threading.Lock()


def application_identity(app):
    return FeishuIdentity(
        app.app_id,
        app.tenant_key,
        app.bot_open_id,
        encryption.decrypt(app.verification_token),
        encryption.decrypt(app.encrypt_key),
    )


def application_client(app):
    secret = encryption.decrypt(app.app_secret)
    key = (app.app_id, hashlib.sha256(secret.encode()).hexdigest())
    with _clients_lock:
        if key not in _clients:
            _clients[key] = FeishuClient(app.app_id, secret)
        _clients.move_to_end(key)
        while len(_clients) > 32:
            _clients.popitem(last=False)
        return _clients[key]


def bot_application_config(bot, db):
    route = db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first()
    if route is None or bot.group_id != route.chat_id:
        raise ValueError("Feishu bot requires an explicit application and group route")
    app = db.get(FeishuApplication, route.application_id)
    if app is None:
        raise ValueError("Feishu application not found")
    return {"_FEISHU_CLIENT": application_client(app), "_FEISHU_APPLICATION_ID": app.id}


def accept_sdk_event(db, app, data):
    """Only the private official-SDK pipe may establish an initially unknown tenant.

    It must first pass app identity, own-bot mention and an explicitly authorized
    group route. HTTP never calls this function and cannot bootstrap a tenant.
    """
    from lockbot.backend.app.feishu.router import accept_event

    if app.transport != "websocket":
        raise InvalidFeishuEvent("WebSocket receiver is disabled")
    if not app.tenant_key:
        tenant = data.get("header", {}).get("tenant_key")
        if not isinstance(tenant, str) or not tenant or len(tenant) > 128:
            raise InvalidFeishuEvent("Missing enterprise identity")
        candidate = normalize_message(data, replace(application_identity(app), tenant_key=tenant))
        if candidate is None:
            return {"accepted": False}
        route = db.query(FeishuGroupRoute).filter_by(application_id=app.id, chat_id=candidate["group_id"]).first()
        if route is None:
            return {"accepted": False}
        db.query(FeishuApplication).filter_by(id=app.id, tenant_key="").update({"tenant_key": tenant})
        db.commit()
        db.refresh(app)
    return accept_event(db, app, data)
