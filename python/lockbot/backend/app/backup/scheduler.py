"""
Backup scheduler — daemon thread for periodic BOS backups.
"""

import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class BackupScheduler:
    """Simple daemon-thread scheduler for periodic backups."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()
        self._started_at: datetime | None = None
        self._last_heartbeat: datetime | None = None
        self._last_error: str = ""
        self._next_run_at: datetime | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started_at = datetime.now()
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="backup-scheduler")
        self._thread.start()
        logger.info("Backup scheduler started")

    def stop(self):
        self._stop_event.set()
        self._reload_event.set()  # Wake up sleeping thread
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Backup scheduler stopped")

    def reload(self):
        """Signal the scheduler to re-read config and recalculate next run."""
        self._reload_event.set()

    def status(self) -> dict:
        """Return scheduler liveness information for admin monitoring."""
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {
            "running": thread_alive and not self._stop_event.is_set(),
            "thread_alive": thread_alive,
            "started_at": self._started_at.isoformat() if self._started_at else "",
            "last_heartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else "",
            "next_run_at": self._next_run_at.isoformat() if self._next_run_at else "",
            "last_error": self._last_error,
        }

    def _run(self):
        while not self._stop_event.is_set():
            self._last_heartbeat = datetime.now()
            try:
                seconds = self._seconds_until_next()
                if seconds is not None:
                    self._next_run_at = datetime.now() + timedelta(seconds=seconds)
                else:
                    self._next_run_at = None
                if seconds is None:
                    # Auto-backup disabled, sleep and wait for reload
                    self._reload_event.wait(timeout=300)
                    self._reload_event.clear()
                    continue

                # Sleep until next fire time (interruptible by reload/stop)
                logger.info("Next backup in %d seconds", seconds)
                triggered = self._reload_event.wait(timeout=seconds)
                self._reload_event.clear()
                if triggered or self._stop_event.is_set():
                    continue  # Config changed or shutting down, recalculate

                # Time to backup — skip if last successful backup was recent
                if self._should_skip():
                    continue
                self._do_backup()
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("Backup scheduler error")
                # Sleep a bit before retry
                self._stop_event.wait(timeout=60)

    def _should_skip(self) -> bool:
        """Skip if last successful backup was less than 20 hours ago (prevents duplicates on restart)."""
        from lockbot.backend.app.backup.service import get_backup_config
        from lockbot.backend.app.database import SessionLocal

        db = SessionLocal()
        try:
            config = get_backup_config(db)
        finally:
            db.close()

        last_time = config.get("backup_last_time", "")
        last_status = config.get("backup_last_status", "")
        if not last_time or last_status != "success":
            return False
        try:
            last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last_dt).total_seconds() < 20 * 3600:
                logger.info("Skipping backup — last success was %s (< 20h ago)", last_time)
                return True
        except ValueError:
            pass
        return False

    def _seconds_until_next(self) -> int | None:
        """Calculate seconds until next backup. Returns None if disabled."""
        from lockbot.backend.app.backup.service import get_backup_config
        from lockbot.backend.app.database import SessionLocal

        db = SessionLocal()
        try:
            config = get_backup_config(db)
        finally:
            db.close()

        if config["backup_auto_enabled"] != "true":
            return None
        if config["backup_method"] != "bos":
            return None

        freq = config["backup_frequency"]  # e.g. "daily:03:00"
        if not freq.startswith("daily:"):
            return None

        time_str = freq[6:]  # "HH:MM"
        try:
            hour, minute = int(time_str[:2]), int(time_str[3:5])
        except (ValueError, IndexError):
            return None

        # Use local time (server timezone) for scheduling
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        return int((target - now).total_seconds())

    def _do_backup(self):
        """Execute backup using a fresh DB session."""
        from lockbot.backend.app.backup.service import run_backup
        from lockbot.backend.app.database import SessionLocal

        db = SessionLocal()
        try:
            result = run_backup(db)
            if result["success"]:
                logger.info("Scheduled backup completed: %s", result["object_key"])
            else:
                logger.error("Scheduled backup failed: %s", result.get("error"))
        finally:
            db.close()


backup_scheduler = BackupScheduler()
