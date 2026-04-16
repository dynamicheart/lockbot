"""Queue lock bot with booking list and auto-take functionality."""

import re
import time

from lockbot.core.i18n import t
from lockbot.core.io import (
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.node_bot import NodeBot
from lockbot.core.utils import (
    create_user_info,
    find_user_info,
    format_access_mode,
    format_duration,
    is_first_user,
    remaining_duration,
    remove_user_info,
)


class QueueBot(NodeBot):
    """
    QueueBot class
    """

    def supported_commands(self):
        return ["lock", "unlock", "free", "kickout", "kicklock", "help", "h", "book", "take", "query"]

    def lock(self, user_id, command):
        """
        Lock nodes
        """
        parse_ok, error_reply, node_keys, duration = self.parse_command(user_id, "lock", command, True)
        command_has_duration = bool(re.search(r"\s([0-9]+\.?[0-9]*)([dhm])\s*$", command))
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if not all(
                (
                    # Condition 1: node is not in use
                    node["status"] == "idle"
                    # and (no booking list or current user is first in queue)
                    and ((not node.get("booking_list")) or is_first_user(node["booking_list"], user_id))
                )
                # Condition 2: current user is already using this node
                or find_user_info(node["current_users"], user_id)
                for node in nodes
            ):
                # If any node fails the above conditions, return error
                return self.show_error(
                    user_id,
                    self._msg_with_usage("error.node_in_use_or_not_your_turn", sep="\n\n"),
                )

            timestamp = int(time.time())
            users_to_notify = set()
            users_to_notify.add(user_id)
            for node in nodes:
                node["status"] = "exclusive"
                booking_info = find_user_info(node["booking_list"], user_id)
                remove_user_info(node["booking_list"], user_id)

                if not command_has_duration and booking_info:
                    total_duration = booking_info["duration"]
                else:
                    total_duration = duration

                if booking_info and total_duration > booking_info["duration"]:
                    for booking_user in node["booking_list"]:
                        users_to_notify.add(booking_user["user_id"])

                user_info = find_user_info(node["current_users"], user_id)
                if not user_info:
                    user_info = create_user_info(user_id, total_duration, timestamp, config=self.config)
                else:
                    total_duration += user_info["duration"]
                    for booking_user in node["booking_list"]:
                        users_to_notify.add(booking_user["user_id"])

                if max_dur > 0 and remaining_duration(user_info["start_time"], total_duration) > max_dur:
                    return self.show_error(
                        user_id,
                        t(
                            "error.lock_max_duration_exceeded",
                            config=self.config,
                            max_duration=format_duration(max_dur, config=self.config),
                        ),
                    )

                user_info["duration"] = total_duration
                user_info["is_notified"] = False
                node["current_users"] = [user_info]

            content = self._msg_with_usage("success.resource_locked")
            if len(users_to_notify) > 1:
                content += t("notify.wait_time_increased", config=self.config)
            reply = self.adapter.build_reply(content, list(users_to_notify))
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            return reply

    def slock(self, user_id, command):
        return self.show_error(user_id, t("error.slock_not_supported", config=self.config))

    def book(self, user_id, command):
        """
        book nodes
        """
        parse_ok, error_reply, node_keys, duration = self.parse_command(user_id, "book", command, True)
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if any(
                find_user_info(node["current_users"], user_id) or find_user_info(node["booking_list"], user_id)
                for node in nodes
            ):
                return self.show_error(user_id, self._msg_with_usage("error.already_locked", sep="\n"))

            timestamp = int(time.time())
            for node in nodes:
                user_info = create_user_info(user_id, duration, timestamp, config=self.config)

                if max_dur > 0 and duration > max_dur:
                    return self.show_error(
                        user_id,
                        t(
                            "error.lock_max_duration_exceeded",
                            config=self.config,
                            max_duration=format_duration(max_dur, config=self.config),
                        ),
                    )

                node["booking_list"].append(user_info)

            reply = self.adapter.build_reply(self._msg_with_usage("success.booking_added"), [user_id])
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            return reply

    def take(self, user_id, command):
        """
        take nodes
        """
        parse_ok, error_reply, node_keys, duration = self.parse_command(user_id, "take", command, True)
        if not parse_ok:
            return error_reply

        content = t("success.take_success_by", config=self.config, user_id=user_id)
        content += self._msg_with_usage("label.before_take")
        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if any(find_user_info(node["current_users"], user_id) for node in nodes):
                return self.show_error(user_id, self._msg_with_usage("error.locked_user_cannot_take", sep="\n"))

            timestamp = int(time.time())
            users_to_notify = set()
            users_to_notify.add(user_id)
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            for node in nodes:
                node["status"] = "exclusive"
                remove_user_info(node["booking_list"], user_id)

                total_duration = duration

                user_info = find_user_info(node["current_users"], user_id)
                if not user_info:
                    user_info = create_user_info(user_id, total_duration, timestamp, config=self.config)
                else:
                    total_duration += user_info["duration"]

                others = [u for u in node["current_users"] if u["user_id"] != user_id]
                for other_user in reversed(others):
                    rem_dur = remaining_duration(other_user["start_time"], other_user["duration"])
                    if rem_dur > 0:
                        other_user["start_time"] = timestamp
                        other_user["duration"] = rem_dur
                        other_user["is_notified"] = False
                        node["booking_list"].insert(0, other_user)
                        users_to_notify.add(other_user["user_id"])
                for user in node["booking_list"]:
                    user["is_notified"] = False
                    users_to_notify.add(user["user_id"])
                if max_dur > 0 and remaining_duration(user_info["start_time"], total_duration) > max_dur:
                    return self.show_error(
                        user_id,
                        t(
                            "error.lock_max_duration_exceeded",
                            config=self.config,
                            max_duration=format_duration(max_dur, config=self.config),
                        ),
                    )

                user_info["duration"] = total_duration
                user_info["is_notified"] = False
                node["current_users"] = [user_info]

            content += self._msg_with_usage("label.after_take")
            reply = self.adapter.build_reply(content, list(users_to_notify))
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            return reply

    def kicklock(self, user_id, command):
        """
        kicklock nodes
        """
        parse_ok, error_reply, node_keys, _ = self.parse_command(user_id, "kicklock", command)
        if not parse_ok:
            return error_reply

        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            users = set([user_id])
            content = t("success.kicklock_cleared", config=self.config, user_id=user_id)
            content += self._msg_with_usage("label.before_release")
            for node in nodes:
                for user_info in node["current_users"]:
                    users.add(user_info["user_id"])
                node["status"] = "idle"
                node["current_users"] = []
            content += self._msg_with_usage("label.after_release")
            reply = self.adapter.build_reply(content, list(users))
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "kicklock", node_keys, config=self.config)

            return reply

    def _help_header(self):
        parts = []
        parts.append(t("help.title", config=self.config))
        parts.append(t("help.section1_title", config=self.config))
        parts.append(
            t(
                "help.rule1_default_duration",
                config=self.config,
                default_duration=format_duration(self.config.get_val("DEFAULT_DURATION"), config=self.config),
            )
        )
        parts.append(t("help.rule2_post_expiry_notification", config=self.config))
        return "".join(parts)

    def _help_commands(self):
        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")
        assert len(cluster_configs) >= 1
        itr = iter(cluster_configs)
        example_node0 = next(itr)
        example_node1 = next(itr) if len(cluster_configs) > 1 else None

        parts = []
        parts.append(t("help.rule3_lock_exclusive", config=self.config))
        parts.append(f"    lock {example_node0}\n")
        parts.append(f"    lock {example_node0} 3d\n")
        if example_node1 is not None:
            parts.append(f"    lock {example_node0},{example_node1} 2h\n")
        parts.append(f"    slock {example_node0} 30m\n")
        parts.append(t("help.section2_title", config=self.config))
        parts.append(f"    unlock {example_node0}\n")
        if example_node1 is not None:
            parts.append(f"    free {example_node0},{example_node1} \n")
        parts.append(t("help.free_all", config=self.config))
        parts.append(t("help.section_kickout_title", config=self.config))
        parts.append(f"    kickout {example_node0} \n")
        if example_node1 is not None:
            parts.append(f"    kickout {example_node0},{example_node1} \n")
        parts.append(t("help.section_booking_title", config=self.config))
        parts.append(f"    book {example_node0} 2h\n")
        parts.append(t("help.section_cancel_booking_title", config=self.config))
        parts.append(f"    free {example_node0}\n")
        parts.append(t("help.section_take_title", config=self.config))
        parts.append(f"    take {example_node0} 2h\n")
        parts.append(t("help.section_kicklock_title", config=self.config))
        parts.append(f"    kicklock {example_node0}\n")
        parts.append(t("help.section_help_title_queue", config=self.config))
        parts.append(t("help.section_query_title_queue", config=self.config))
        parts.append(t("help.query_at_bot", config=self.config))
        parts.append(f"    {example_node0}\n\n")
        return "".join(parts)

    def _check_and_notify(self) -> float | None:
        """
        Check resource expiration and booking timeouts, release expired resources,
        and send notifications. Persist state only when changes occur.

        Returns: seconds until next interesting event, or None if no active locks/bookings.
        """
        trigger_time_alert = False
        trigger_notify_alert = False
        state_changed = False
        user_ids = set()
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")
        TIME_ALERT = self.config.get_val("TIME_ALERT")

        TIME_TO_LOCK = 5 * 60

        expired_users = []

        alert_info = self._build_alert_header()

        # Build the base part of notify_info (resource available + time)
        notify_info_header = t(
            "notify.resource_available_header",
            config=self.config,
            timeout=format_duration(TIME_TO_LOCK, config=self.config),
        )

        expired_notify = ""  # Expired booking content
        pending_notify = ""  # Pending lock booking content

        def notify_first_booking_user(node_key, node, timestamp):
            """Notify the first booked user for this node."""
            nonlocal trigger_notify_alert, state_changed, user_ids, pending_notify

            if not node["booking_list"]:
                return

            first_user = node["booking_list"][0]
            first_user["is_notified"] = True
            first_user["start_time"] = timestamp
            trigger_notify_alert = True
            state_changed = True
            user_ids.add(first_user["user_id"])

            dur = format_duration(first_user["duration"], config=self.config)
            pending_notify += f"  - {node_key} {first_user['user_id']} {dur}\n"

        with self._lock:
            # 1. Release resources
            for node_key, node in self.state.bot_state.items():
                # === 1. Check if current_users have expired ===
                if node["status"] != "idle":
                    removed_users_id = []
                    for user_info in node["current_users"]:
                        remaining_time = remaining_duration(user_info["start_time"], user_info["duration"])
                        if remaining_time <= 0:
                            removed_users_id.append(user_info["user_id"])
                            state_changed = True

                            if not EARLY_NOTIFY:
                                trigger_time_alert = True
                                user_ids.add(user_info["user_id"])

                                uid = user_info["user_id"] + format_access_mode(node["status"], config=self.config)
                                duration = format_duration(remaining_time, config=self.config)
                                alert_info += f"{node_key} {uid}  {duration}\n"

                        if EARLY_NOTIFY and not user_info["is_notified"] and remaining_time <= TIME_ALERT:
                            trigger_time_alert = True
                            user_ids.add(user_info["user_id"])
                            user_info["is_notified"] = True
                            state_changed = True

                            uid = user_info["user_id"] + format_access_mode(node["status"], config=self.config)
                            duration = format_duration(remaining_time, config=self.config)
                            alert_info += f"{node_key} {uid}  {duration}\n"

                    for user_id in removed_users_id:
                        remove_user_info(node["current_users"], user_id)

                    if len(node["current_users"]) == 0:
                        node["status"] = "idle"
                # === 2. If node is idle, check if any booked user needs notification ===
                if node["status"] == "idle" and node["booking_list"]:
                    first_book_user = node["booking_list"][0]
                    now = int(time.time())

                    # Not notified yet, notify directly
                    if not first_book_user.get("is_notified", False):
                        notify_first_booking_user(node_key, node, now)
                    else:
                        # Already notified but timed out without response -> remove and notify next
                        if now - first_book_user["start_time"] >= TIME_TO_LOCK:
                            remove_user_info(node["booking_list"], first_book_user["user_id"])
                            expired_users.append((first_book_user["user_id"], node_key))
                            user_ids.add(first_book_user["user_id"])
                            state_changed = True
                            notify_first_booking_user(node_key, node, now)

            if state_changed:
                save_bot_state_to_file(self.state.bot_state, config=self.config)

            # Compute next wakeup after mutations
            min_next = float("inf")
            now_ts = int(time.time())
            for node in self.state.bot_state.values():
                # Active users
                if node["status"] != "idle":
                    for user_info in node["current_users"]:
                        remaining = remaining_duration(user_info["start_time"], user_info["duration"])
                        if remaining <= 0:
                            continue
                        if EARLY_NOTIFY and not user_info["is_notified"]:
                            next_event = remaining - TIME_ALERT
                        else:
                            next_event = remaining
                        min_next = min(min_next, next_event)
                # Booking list: first notified user may time out after TIME_TO_LOCK
                booking_list = node.get("booking_list", [])
                if booking_list:
                    first = booking_list[0]
                    if first.get("is_notified") and first.get("start_time"):
                        until_timeout = TIME_TO_LOCK - (now_ts - first["start_time"])
                        min_next = min(min_next, max(0.0, until_timeout))
                    else:
                        min_next = min(min_next, 1.0)  # unnotified booking → check soon

        # Aggregate expired bookings into expired_notify
        if expired_users:
            expired_notify = t("notify.booking_expired_header", config=self.config)
            for user_id, node_key in expired_users:
                expired_notify += t(
                    "notify.booking_timeout_cancelled", config=self.config, user_id=user_id, node_key=node_key
                )
            expired_notify += "\n"

        # Assemble final notify_info in strict order
        notify_info = notify_info_header
        if expired_notify:
            notify_info += expired_notify
        if pending_notify:
            notify_info += t("notify.pending_bookings_header", config=self.config) + pending_notify

        # Send messages
        if trigger_time_alert or trigger_notify_alert:
            content = ""
            if trigger_time_alert:
                content += alert_info + "\n"
            if trigger_notify_alert:
                content += notify_info + "\n"
            msg = self.adapter.build_reply(content, list(user_ids))
            try:
                self.adapter.send(msg)
            except Exception:
                self.logger.exception("Failed to send alert for bot %s", self.config.get_val("BOT_NAME"))

        return max(1.0, min_next) if min_next != float("inf") else None

    def _current_usage(self, node_filter=None):
        """
        _current_usage
        """

        def _get_current_usage(nodes):
            """
            _get_current_usage
            """
            usage_info = ""
            for node_key, node_status in nodes.items():
                if node_filter is None or node_key == node_filter:
                    if node_status["status"] == "idle":
                        usage_info += "{:} {}\n".format(node_key, t("status.idle", config=self.config))
                    else:
                        for user_idx, user_info in enumerate(node_status["current_users"]):
                            node_name = f"{node_key}" if user_idx == 0 else ""
                            uid = user_info["user_id"]
                            duration = format_duration(
                                remaining_duration(user_info["start_time"], user_info["duration"]),
                                config=self.config,
                            )
                            usage_info += f"{node_name} {uid}  {duration}\n"

                    # Show queue info (booking_list)
                    booking_list = node_status.get("booking_list", [])
                    if booking_list:
                        usage_info += t("label.queue_list", config=self.config)
                        # Calculate estimated wait time based on max remaining lock time
                        current_locked_time = 0
                        for user_info in node_status.get("current_users", []):
                            remain = remaining_duration(user_info["start_time"], user_info["duration"])
                            if remain > current_locked_time:
                                current_locked_time = remain

                        # Accumulated wait time
                        accumulated_wait = current_locked_time
                        for idx, booking_user in enumerate(booking_list):
                            uid = booking_user["user_id"]
                            dur_sec = booking_user["duration"]
                            wait_time_str = format_duration(accumulated_wait, config=self.config)
                            usage_info += t(
                                "label.queue_item",
                                config=self.config,
                                index=idx + 1,
                                user_id=uid,
                                duration=format_duration(dur_sec, config=self.config),
                                wait_time=wait_time_str,
                            )
                            accumulated_wait += dur_sec
            usage_info += "\n"
            return usage_info

        usage_info = ""
        usage_info += _get_current_usage({k: v for k, v in self.state.bot_state.items()})

        return usage_info
