"""Old application inserts omit newly introduced bot columns during rollback."""

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from sqlalchemy import create_engine, text


def test_fresh_schema_accepts_legacy_bot_insert():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    Bot.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO bots
            (user_id,name,bot_type,platform,webhook_url,aes_key,token,cluster_configs,is_deleted,
             status,consecutive_failures,config_overrides)
            VALUES (1,'legacy','NODE','Infoflow','','','','[]',0,'stopped',0,'{}')""")
        )
        assert tuple(
            connection.execute(
                text("SELECT identity_revision,resource_auth_enabled,resource_api_notify_enabled FROM bots")
            ).one()
        ) == (0, 1, 1)
    engine.dispose()
