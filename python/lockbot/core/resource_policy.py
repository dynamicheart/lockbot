"""Bot-wide acquisition policy, independent from transport and API machine budgets."""

import time


def resource_entries(state):
    return {
        key: entry
        for node, value in state.items()
        for key, entry in (
            [(f"{node}:dev{card['dev_id']}", card) for card in value] if isinstance(value, list) else [(node, value)]
        )
    }


def policy_for_state(state, config, user_id=None, now=None):
    now = time.time() if now is None else now
    claimed = set()
    for key, entry in resource_entries(state).items():
        live = [u for u in entry.get("current_users", []) if u.get("start_time", 0) + u.get("duration", 0) > now]
        booked = [
            u for u in entry.get("booking_list", []) if not u.get("is_notified") or u.get("start_time", 0) + 300 > now
        ]
        if any(u.get("user_id") == user_id for u in live + booked):
            claimed.add(key)
    return {
        "forbid_relock": bool(config.get_val("FORBID_RELOCK", False)),
        "max_resources_per_user": config.get_val("MAX_RESOURCES_PER_USER", -1),
        "resource_unit": "card" if config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"} else "node",
        "claimed_resource_count": len(claimed),
        "_claimed_resources": sorted(claimed),
    }


def public_policy(state, config, user_id=None, now=None):
    policy = policy_for_state(state, config, user_id, now)
    policy.pop("_claimed_resources")
    if user_id is None:
        policy.pop("claimed_resource_count")
    return policy
