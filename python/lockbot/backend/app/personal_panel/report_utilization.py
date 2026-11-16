"""Project a fresh, caller-scoped reporter sample to the browser and Agent API."""

from datetime import datetime, timedelta


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant if instant.utcoffset() is not None else None


def utilization_fields(allocation, group, now):
    sample = allocation.get("utilization_sample")
    if allocation.get("kind") != "hold" or group.get("status") != "ok" or not isinstance(sample, dict):
        return {}
    captured = _instant(group.get("source_at"))
    deadline = _instant(sample.get("expires_at"))
    lease_end = _instant(allocation.get("expires_at"))
    if captured is None or deadline is None or not -30 <= (now - captured).total_seconds() <= 300:
        return {}
    deadline = min(deadline, captured + timedelta(seconds=300))
    if lease_end is not None:
        deadline = min(deadline, lease_end)
    if deadline <= max(now, captured):
        return {}
    projected = {"expires_at": deadline.isoformat()}
    for metric in ("compute_percent", "memory_percent"):
        value = sample.get(metric)
        projected[metric] = value if type(value) in (int, float) and 0 <= value <= 100 else None
    return {"utilization_sample": projected}
