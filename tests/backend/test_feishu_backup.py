import sqlite3
import uuid

import pytest
from lockbot.backend.app.bots.encryption import decrypt, encrypt
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.backup import read_application_metadata, restore_route
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute


def archived():
    return {
        "_feishu_application": {
            "id": 900,
            "app_id": "cli_test",
            "name": "App",
            "tenant_key": "tenant",
            "bot_open_id": "ou_bot",
            "app_secret": encrypt("source-secret"),
            "verification_token": "",
            "encrypt_key": "",
            "transport": "websocket",
            "chat_id": "oc_group",
        }
    }


def make_bot(db):
    bot = Bot(
        uuid=str(uuid.uuid4()),
        user_id=1,
        name="Restored bot",
        bot_type="QUEUE",
        platform="Feishu",
        group_id="oc_group",
        webhook_url="",
        aes_key="",
        token="",
        cluster_configs='["node1"]',
        config_overrides="{}",
        status="recovery_required",
    )
    db.add(bot)
    db.flush()
    return bot


def test_restore_maps_application_by_identity_not_backup_numeric_id(db_session):
    bot = make_bot(db_session)
    restore_route(db_session, bot, archived())
    db_session.commit()
    app = db_session.query(FeishuApplication).one()
    route = db_session.query(FeishuGroupRoute).one()
    assert app.id != 900
    assert decrypt(app.app_secret) == "source-secret"
    assert (route.bot_id, route.application_id, route.chat_id) == (bot.id, app.id, "oc_group")
    # Keep a rotated target credential instead of replacing it with an older archive secret.
    app.app_secret = encrypt("rotated-target-secret")
    db_session.commit()
    restore_route(db_session, bot, archived())
    db_session.commit()
    assert decrypt(app.app_secret) == "rotated-target-secret"


def test_restore_refuses_cross_tenant_application_collision(db_session):
    app = FeishuApplication(
        app_id="cli_test",
        name="Other tenant",
        tenant_key="other",
        bot_open_id="ou_bot",
        app_secret=encrypt("other-secret"),
        verification_token="",
        encrypt_key="",
        transport="websocket",
    )
    db_session.add(app)
    db_session.flush()
    bot = make_bot(db_session)
    with pytest.raises(ValueError, match="identity conflicts"):
        restore_route(db_session, bot, archived())
    assert db_session.query(FeishuGroupRoute).count() == 0
    assert decrypt(app.app_secret) == "other-secret"


def test_old_archive_has_no_application_metadata():
    connection = sqlite3.connect(":memory:")
    assert read_application_metadata(connection, 1) is None
    connection.close()


def test_restore_does_not_implicitly_enable_new_membership_policy(db_session):
    from lockbot.backend.app.feishu.membership import greeting_enabled

    value = archived()
    value["_feishu_application"]["unbound_greeting_enabled"] = True
    bot = make_bot(db_session)
    restore_route(db_session, bot, value)
    db_session.commit()
    application = db_session.query(FeishuApplication).one()
    assert greeting_enabled(db_session, application.id) is False
