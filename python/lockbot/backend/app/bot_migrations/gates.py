"""Fast local admission fence, rebuilt from durable journal before startup."""


def migration_fenced(bot_id):
    from lockbot.backend.app.bots.manager import bot_manager

    return bot_manager.migration_fenced(bot_id)


def identity_still_current(db, bot):
    """Late signing work cannot renew authority from an ORM row retained before a cutover."""
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from lockbot.backend.app.bots.models import Bot

    expected = bot.uuid, bot.identity_revision, bot.user_id, False
    with sessionmaker(bind=db.get_bind())() as fresh:
        current = fresh.execute(
            select(Bot.uuid, Bot.identity_revision, Bot.user_id, Bot.is_deleted).where(Bot.id == bot.id)
        ).first()
        return current is not None and tuple(current) == expected
