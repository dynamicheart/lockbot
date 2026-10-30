"""Read-only provider diagnostics. Observations never authorize migration execution."""

from datetime import datetime, timezone

from fastapi import HTTPException

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.connectors import connectors
from lockbot.backend.app.feishu.models import FeishuApplication
from lockbot.backend.app.feishu.service import application_client
from lockbot.backend.app.platform_applications.service import fingerprint
from lockbot.core.platforms.feishu import FeishuAPIError


def _identity(app):
    return (app.id, app.app_id, app.tenant_key, app.bot_open_id, app.transport, fingerprint(app, "feishu"))


def target_readiness(db, bot_id, body, *, actor_id, token_version):
    bot, app = db.get(Bot, bot_id), db.get(FeishuApplication, body.application_id)
    if bot is None or bot.is_deleted:
        raise HTTPException(404, "Bot not found")
    if str(bot.platform).lower() != "infoflow":
        raise HTTPException(409, "Migration source must use Infoflow")
    if app is None:
        raise HTTPException(404, "Feishu application not found")
    expected = _identity(app)
    source = (bot.uuid, bot.identity_revision, bot.platform)
    blockers = [
        "publication_and_usable_range_unverified",
        "receiver_end_to_end_unverified",
        "migration_execution_disabled",
    ]
    if bot.utilization_config:
        blockers.append("metrics_clock_authority_unverified")
    checks = {"provider_bot_identity": "unverified", "provider_bot_membership": "unverified"}
    receiver = connectors.snapshot(app.id) if app.transport == "websocket" else None
    if not app.tenant_key or not app.bot_open_id:
        blockers.append("target_application_identity_unknown")
    else:
        try:
            client = application_client(app)
            if client.bot_identity() != app.bot_open_id:
                checks["provider_bot_identity"] = "mismatch"
                blockers.append("provider_bot_identity_mismatch")
            else:
                checks["provider_bot_identity"] = "matched"
                present = client.bot_in_group(body.chat_id)
                checks["provider_bot_membership"] = "present" if present is True else "absent"
                if present is not True:
                    blockers.append("provider_bot_not_in_target_group")
        except FeishuAPIError:
            # Do not return upstream bodies, proxy addresses, access tokens or credential details.
            blockers.append("provider_check_unavailable")
    db.rollback()
    actor = db.get(User, actor_id)
    if (
        actor is None
        or actor.role != "super_admin"
        or actor.must_change_password
        or actor.effective_token_version != token_version
    ):
        raise HTTPException(403, "Current super-administrator authority required")
    current, current_bot = db.get(FeishuApplication, body.application_id), db.get(Bot, bot_id)
    if (
        current is None
        or _identity(current) != expected
        or current_bot is None
        or current_bot.is_deleted
        or (current_bot.uuid, current_bot.identity_revision, current_bot.platform) != source
    ):
        checks = {key: "invalidated" for key in checks}
        blockers.append("target_or_source_changed_during_probe")
    return {
        "bot_id": bot_id,
        "application_id": body.application_id,
        "chat_id": body.chat_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "can_commit": False,
        "checks": checks,
        "blockers": blockers,
        "receiver_observation": receiver,
        "receiver_observation_is_end_to_end_proof": False,
    }
