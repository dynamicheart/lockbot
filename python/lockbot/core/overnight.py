"""Administrator-configured overnight exceptions to the regular lease ceiling."""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lockbot.core.i18n import t
from lockbot.core.utils import format_duration


def window_settings(config):
    start = config.get("OVERNIGHT_START", "22:00")
    end = config.get("OVERNIGHT_END", "09:00")
    for value, minimum, maximum in [(start, "18:00", "23:59"), (end, "00:00", "10:00")]:
        if not isinstance(value, str) or not re.fullmatch(r"[0-2]\d:[0-5]\d", value) or not minimum <= value <= maximum:
            raise ValueError(f"Overnight time must be HH:MM between {minimum} and {maximum}")
    name = config.get("OVERNIGHT_TIMEZONE", "Asia/Shanghai")
    try:
        zone = ZoneInfo(name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("OVERNIGHT_TIMEZONE must name an IANA timezone") from exc
    return time.fromisoformat(start), time.fromisoformat(end), zone


def latest_overnight_cutoff(config, now):
    """Return the end of the latest window that has started, even after it ends."""
    if not config.get_val("OVERNIGHT_ENABLED"):
        return None
    start, end, zone = window_settings(
        {k: config.get_val(k) for k in ("OVERNIGHT_START", "OVERNIGHT_END", "OVERNIGHT_TIMEZONE")}
    )
    day = datetime.fromtimestamp(now, zone).date()
    if now < datetime.combine(day, start, zone).timestamp():
        day -= timedelta(days=1)
    return int(datetime.combine(day + timedelta(days=1), end, zone).timestamp())


def overnight_cutoff(config, now):
    cutoff = latest_overnight_cutoff(config, now)
    return cutoff if cutoff is not None and now < cutoff else None


@dataclass(frozen=True)
class LeaseLimit:
    now: int
    maximum: int
    cutoff: int | None

    def rejects(self, holder, duration):
        if self.maximum <= 0:
            return False
        previous_expiry = holder["start_time"] + holder["duration"] if holder else self.now
        expiry = previous_expiry + duration
        if holder and "overnight_cutoff" in holder and expiry > holder["overnight_cutoff"]:
            return True
        if expiry <= self.now + self.maximum:
            return False
        return self.cutoff is None or expiry > self.cutoff or previous_expiry < self.now

    def record(self, holder):
        if self.cutoff and self.maximum > 0 and holder["start_time"] + holder["duration"] > self.now + self.maximum:
            holder.setdefault("overnight_cutoff", self.cutoff)

    def error(self, bot, user, holder=None, *, shared=False):
        config = bot.config
        text = t(
            "error.slock_max_duration_exceeded" if shared else "error.lock_max_duration_exceeded",
            config=config,
            max_duration=format_duration(self.maximum, config=config),
            whitelist_hint=bot._whitelist_hint(),
        )
        cutoff = (holder or {}).get("overnight_cutoff", self.cutoff)
        if cutoff:
            zone = ZoneInfo(config.get_val("OVERNIGHT_TIMEZONE"))
            text += t(
                "error.overnight_cutoff",
                config=config,
                cutoff=datetime.fromtimestamp(cutoff, zone).isoformat(timespec="minutes"),
            )
        return bot.show_error(user, text)
