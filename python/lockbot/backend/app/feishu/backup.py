"""Restore group routes by stable application identity, never by archived integer IDs."""

import re

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute


def read_application_metadata(connection, bot_id):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"feishu_applications", "feishu_group_routes"}.issubset(tables):
        return None
    row = connection.execute(
        "SELECT a.*, r.chat_id FROM feishu_applications a "
        "JOIN feishu_group_routes r ON r.application_id = a.id WHERE r.bot_id = ?",
        (bot_id,),
    ).fetchone()
    return dict(row) if row else None


def restore_route(db, bot, archived, source_key=None):
    if (bot.platform or "").lower() != "feishu":
        db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).delete()
        return
    data = archived.get("_feishu_application")
    if not isinstance(data, dict):
        raise ValueError("Feishu application and group route are missing from this archive")
    app_id = data.get("app_id", "")
    tenant = data.get("tenant_key", "")
    bot_identity = data.get("bot_open_id", "")
    transport = data.get("transport", "")
    if (
        not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id)
        or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", bot_identity)
        or not isinstance(tenant, str)
        or len(tenant) > 128
        or transport not in ("http", "websocket")
        or data.get("chat_id") != bot.group_id
        or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", bot.group_id or "")
    ):
        raise ValueError("Invalid archived Feishu application identity")
    application = db.query(FeishuApplication).filter_by(app_id=app_id).first()
    if application:
        if application.bot_open_id != bot_identity or (application.tenant_key and application.tenant_key != tenant):
            raise ValueError("Feishu application identity conflicts with this deployment")
        # Keep the target's working (possibly rotated) secrets and transport.
    else:
        from lockbot.backend.app.backup.service import _decrypt_field

        secrets = {}
        for key in ("app_secret", "verification_token", "encrypt_key"):
            plain, valid = _decrypt_field(data.get(key, ""), source_key)
            if not valid:
                raise ValueError("Cannot decrypt archived Feishu application credentials")
            secrets[key] = encryption.encrypt(plain)
        if not secrets["app_secret"] or (transport == "http" and (not secrets["verification_token"] or not tenant)):
            raise ValueError("Archived Feishu application credentials are incomplete")
        application = FeishuApplication(
            app_id=app_id,
            name=str(data.get("name") or app_id)[:128],
            tenant_key=tenant,
            bot_open_id=bot_identity,
            transport=transport,
            **secrets,
        )
        db.add(application)
        db.flush()
    conflict = db.query(FeishuGroupRoute).filter_by(application_id=application.id, chat_id=bot.group_id).first()
    if conflict is not None and conflict.bot_id != bot.id:
        raise ValueError("Feishu group is already assigned to another resource bot")
    route = db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first()
    if route:
        route.application_id, route.chat_id = application.id, bot.group_id
    else:
        db.add(FeishuGroupRoute(application_id=application.id, chat_id=bot.group_id, bot_id=bot.id))
