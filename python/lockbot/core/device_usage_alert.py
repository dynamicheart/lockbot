"""Device usage alert formatting and notification helpers."""

from lockbot.core.utils import (
    format_access_mode,
    format_duration,
)


def group_devices_by_node_user_and_mode(alert_tuples):
    """
    Group devices by user, lock mode, and node.

    alert_tuples: list of tuple (user_id, node_key, dev_id, status, remaining_time)
    """
    grouped_devices = []
    grouped = {}

    alert_tuples.sort(key=lambda x: (x[0], x[1], x[2]))

    for user_id, node_key, dev_id, status, remaining_time in alert_tuples:
        key = (user_id, node_key, status, remaining_time)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(dev_id)

    # Merge consecutive devices
    for (user_id, node_key, status, remaining_time), devices in grouped.items():
        devices.sort()
        start = None
        end = None
        prev_dev = None

        for current_dev in devices:
            if prev_dev is None:
                start = current_dev
                end = current_dev
            elif current_dev == prev_dev + 1:
                end = current_dev
            else:
                grouped_devices.append((node_key, user_id, (start, end), status, remaining_time))
                start = current_dev
                end = current_dev

            prev_dev = current_dev

        grouped_devices.append((node_key, user_id, (start, end), status, remaining_time))

    return grouped_devices


def format_alert_info(node_key, user_id, start, end, status, remaining_time):
    """Format a single device range alert line."""
    if start == end:
        return f"{node_key} dev{start} {user_id}{format_access_mode(status)}  {format_duration(remaining_time)}\n"
    else:
        return f"{node_key} dev{start}-{end} {user_id}{format_access_mode(status)}  {format_duration(remaining_time)}\n"
