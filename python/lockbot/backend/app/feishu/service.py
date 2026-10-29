import hashlib
import threading
from collections import OrderedDict
from dataclasses import replace
from weakref import WeakSet

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient
from lockbot.core.platforms.feishu_events import FeishuIdentity, InvalidFeishuEvent, normalize_message

_clients = OrderedDict()
_clients_lock = threading.Lock()
_client_references = WeakSet()
_credential_generations = OrderedDict()


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
        if app.app_id in _credential_generations and _credential_generations[app.app_id] != key[1]:
            raise FeishuAPIError("Application credentials changed; refresh before retrying")
        if key not in _clients:
            _clients[key] = FeishuClient(app.app_id, secret)
            _client_references.add(_clients[key])
        _clients.move_to_end(key)
        while len(_clients) > 32:
            _clients.popitem(last=False)
        return _clients[key]


def invalidate_application_clients(app_id, *, secret=""):
    """Invalidate only this app, including references retained by running resource bots."""
    with _clients_lock:
        _credential_generations[app_id] = hashlib.sha256(secret.encode()).hexdigest() if secret else None
        _credential_generations.move_to_end(app_id)
        while len(_credential_generations) > 256:
            _credential_generations.popitem(last=False)
        for client in list(_client_references):
            if client.app_id == app_id:
                client.replace_application_secret(secret)
        for key in list(_clients):
            if key[0] == app_id:
                _clients.pop(key)


def _routed_application(bot, db):
    route = db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first()
    if route is None or bot.group_id != route.chat_id:
        raise ValueError("Feishu bot requires an explicit application and group route")
    app = db.get(FeishuApplication, route.application_id)
    if app is None:
        raise ValueError("Feishu application not found")
    return app


def bot_application_config(bot, db):
    app = _routed_application(bot, db)
    return {"_FEISHU_CLIENT": application_client(app), "_FEISHU_APPLICATION_ID": app.id}


def cached_bot_client(bot, db):
    """Use only this bot's routed app cache; cold/restarted caches stay unknown."""
    from cryptography.fernet import InvalidToken

    if str(bot.platform).lower() != "feishu":
        return None
    try:
        app = _routed_application(bot, db)
        secret = encryption.decrypt(app.app_secret)
        key = (app.app_id, hashlib.sha256(secret.encode()).hexdigest())
    except (ValueError, InvalidToken):
        return None
    with _clients_lock:
        return _clients.get(key)


def decorate_log_actors(entries, client):
    """Metadata only, after privacy projection. Preserve stored/downloaded message text."""
    import re

    if client is None:
        return entries
    result = []
    for entry in entries:
        value = dict(entry)
        message = value.get("message")
        match = re.match(r"^\[(ou_[A-Za-z0-9_-]+)\]\s", message) if isinstance(message, str) else None
        if value.get("category") == "command" and match:
            identity = match[1]
            actor = {"platform": "feishu", "external_user_id": identity}
            label = client.cached_user_name(identity)
            if label:
                actor["display_name"] = label
            value["actor"] = actor
        result.append(value)
    return result


def accept_sdk_event(db, app, data):
    """Only the private official-SDK pipe may establish an initially unknown tenant.

    It must first pass app identity, own-bot mention and an explicitly authorized
    group route. An unassociated-group hint may use the verified candidate identity
    without persisting it. HTTP never calls this function and cannot bootstrap a tenant.
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
            from lockbot.backend.app.feishu.unbound import queue_hint

            queue_hint(db, app, candidate, tenant=tenant, allow_unknown_tenant=True)
            return {"accepted": False}
        db.query(FeishuApplication).filter_by(id=app.id, tenant_key="").update({"tenant_key": tenant})
        db.commit()
        db.refresh(app)
    return accept_event(db, app, data)
