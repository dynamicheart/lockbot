"""One durable queue consumer; the deployment already enforces one process per state store.

Mark a target sending BEFORE network IO. After interruption its result is unknown and
must never be automatically resent. Only untouched pending targets resume after restart.
"""

import logging
import threading
from datetime import datetime, timedelta

from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring, bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.broadcast.models import BroadcastJob, BroadcastTarget, GroupActivity
from lockbot.backend.app.database import SessionLocal

logger = logging.getLogger(__name__)


def record_group_activity(db, bot, group_id):
    group_id = str(group_id)
    if not group_id.isdecimal() or len(group_id) > 20 or int(group_id) <= 0:
        return
    group_id = str(int(group_id))
    row = db.get(GroupActivity, (bot.id, group_id))
    if row is None:
        row = GroupActivity(bot_id=bot.id, group_id=group_id)
        db.add(row)
    row.identity_revision = bot.identity_revision
    row.last_seen_at = datetime.utcnow()


def recent_targets(db, days):
    rows = (
        db.query(Bot, GroupActivity)
        .join(GroupActivity, GroupActivity.bot_id == Bot.id)
        .filter(
            Bot.is_deleted.is_(False),
            Bot.status == "running",
            GroupActivity.identity_revision == Bot.identity_revision,
            GroupActivity.last_seen_at >= datetime.utcnow() - timedelta(days=days),
        )
        .order_by(GroupActivity.last_seen_at.desc(), Bot.id)
        .all()
    )
    seen, targets = set(), []
    for bot, group in rows:
        # Current broadcast delivery is Infoflow only. Never silently use another format.
        if bot.platform.lower() != "infoflow":
            continue
        if group.group_id in seen:
            continue
        seen.add(group.group_id)
        targets.append(
            dict(
                bot_id=bot.id,
                bot_uuid=bot.uuid,
                identity_revision=bot.identity_revision,
                bot_name=bot.name,
                group_id=group.group_id,
            )
        )
    return targets


def recover_interrupted(db):
    db.query(BroadcastTarget).filter_by(status="sending").update(
        {"status": "unknown", "result_code": "interrupted", "completed_at": datetime.utcnow()}
    )
    db.commit()


def process_one(factory=SessionLocal, manager=bot_manager):
    """Attempt at most one recipient. Injectable dependencies keep tests off real IM."""
    with factory() as db:
        job = (
            db.query(BroadcastJob)
            .filter(BroadcastJob.status.in_(["queued", "running"]))
            .order_by(BroadcastJob.submitted_at, BroadcastJob.id)
            .first()
        )
        if job is None:
            return False
        target = (
            db.query(BroadcastTarget).filter_by(job_id=job.id, status="pending").order_by(BroadcastTarget.id).first()
        )
        if target is None:
            db.query(BroadcastTarget).filter_by(job_id=job.id, status="sending").update(
                {"status": "unknown", "result_code": "interrupted", "completed_at": datetime.utcnow()}
            )
            job.status = "completed"
            db.commit()
            return True
        user = db.get(User, job.creator_id)
        if user is None or user.role != "super_admin":
            db.query(BroadcastTarget).filter_by(job_id=job.id, status="pending").update(
                {"status": "skipped", "result_code": "permission_changed"}
            )
            job.status = "completed"
            db.commit()
            return True
        job.status = "running"
        target_id, bot_id = target.id, target.bot_id
        target.status = "sending"
        db.commit()
        try:
            with manager.admit(bot_id) as lease:
                db.expire_all()
                bot = db.get(Bot, bot_id)
                target = db.get(BroadcastTarget, target_id)
                if (
                    bot is None
                    or bot.is_deleted
                    or bot.uuid != target.bot_uuid
                    or bot.identity_revision != target.identity_revision
                ):
                    outcome = ("skipped", "identity_changed")
                else:
                    # Adapter performs ONE bounded HTTP request, no automatic retry.
                    outcome = lease.bot.adapter.send_broadcast(job.message, target.group_id, job.mention_all)
        except (BotNotRunning, InstanceRetiring, AdmissionFull):
            outcome = ("skipped", "bot_unavailable")
        except Exception as exc:
            logger.warning("Broadcast target %s outcome unknown (%s)", target_id, type(exc).__name__)
            outcome = ("unknown", "delivery_uncertain")
        target = db.get(BroadcastTarget, target_id)
        target.status, target.result_code = outcome
        target.completed_at = datetime.utcnow()
        db.commit()
    return True


class BroadcastWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        with SessionLocal() as db:
            recover_interrupted(db)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="broadcast-worker", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(1):
            try:
                process_one()
            except Exception:
                logger.exception("Broadcast worker iteration failed")

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)


broadcast_worker = BroadcastWorker()
