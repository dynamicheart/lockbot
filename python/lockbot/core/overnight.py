"""Overnight and weekend scheduling for leases and utilization checks."""

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


def latest_overnight_window(config, now):
    """Return the cutoff and whether the latest window includes the weekend."""
    start, end, zone = window_settings(
        {k: config.get_val(k) for k in ("OVERNIGHT_START", "OVERNIGHT_END", "OVERNIGHT_TIMEZONE")}
    )
    day = datetime.fromtimestamp(now, zone).date()
    if now < datetime.combine(day, start, zone).timestamp():
        day -= timedelta(days=1)
    weekday = day.weekday()
    weekend = weekday >= 4
    end_day = day + timedelta(days=7 - weekday if weekend else 1)
    return int(datetime.combine(end_day, end, zone).timestamp()), weekend


@dataclass(frozen=True)
class LeaseLimit:
    now: int
    maximum: int
    cutoff: int | None
    weekend: bool = False

    @classmethod
    def for_config(cls, config, now, maximum):
        if config.get_val("OVERNIGHT_ENABLED"):
            cutoff, weekend = latest_overnight_window(config, now)
            if now < cutoff:
                return cls(now, maximum, cutoff, weekend)
        return cls(now, maximum, None)

    @property
    def remaining_limit(self):
        if self.weekend:
            until_cutoff = self.cutoff - self.now
            return min(self.maximum, until_cutoff) if self.maximum > 0 else until_cutoff
        if self.maximum <= 0:
            return -1
        return max(self.maximum, (self.cutoff or self.now) - self.now)

    def rejects(self, holder, duration):
        previous_expiry = holder["start_time"] + holder["duration"] if holder else self.now
        expiry = previous_expiry + duration
        if self.weekend and expiry > self.now + self.remaining_limit:
            return True
        if self.maximum <= 0:
            return False
        if holder and "overnight_cutoff" in holder and expiry > holder["overnight_cutoff"]:
            return True
        if expiry <= self.now + self.maximum:
            return False
        return self.cutoff is None or expiry > self.cutoff or previous_expiry < self.now

    def record(self, holder):
        if (
            not self.weekend
            and self.cutoff
            and self.maximum > 0
            and holder["start_time"] + holder["duration"] > self.now + self.maximum
        ):
            holder.setdefault("overnight_cutoff", self.cutoff)

    def error(self, bot, user, holder=None, *, shared=False):
        config = bot.config
        text = (
            t(
                "error.slock_max_duration_exceeded" if shared else "error.lock_max_duration_exceeded",
                config=config,
                max_duration=format_duration(self.maximum, config=config),
                whitelist_hint=bot._whitelist_hint(),
            )
            if self.maximum > 0
            else ""
        )
        cutoff = (holder or {}).get("overnight_cutoff", self.cutoff)
        if cutoff:
            zone = ZoneInfo(config.get_val("OVERNIGHT_TIMEZONE"))
            text += t(
                "error.overnight_cutoff",
                config=config,
                cutoff=datetime.fromtimestamp(cutoff, zone).isoformat(timespec="minutes"),
            )
        return bot.show_error(user, text.strip())
