"""Queue lock bot with booking list and auto-take functionality."""

import re
import time
from contextlib import suppress

from lockbot.core.command_result import completed_reply
from lockbot.core.i18n import t
from lockbot.core.io import (
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.node_bot import NodeBot
from lockbot.core.utils import (
    create_user_info,
    find_user_info,
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
        return ["lock", "unlock", "free", "kickout", "kicklock", "help", "h", "book", "take", "query", "alias"]

    def lock(self, user_id, command, *, acquisition_limit=None):
        """
        Lock nodes
        """
        parse_ok, error_reply, node_keys, duration = self._parse_resource_command(user_id, "lock", command, True)
        command_has_duration = bool(re.search(r"\s([0-9]+\.?[0-9]*)([dhm])\s*$", command))
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        whitelist = self._duration_whitelist
        with self._state_guard():
            self._check_acquisition_limit_locked(user_id, node_keys, acquisition_limit)
            nodes = [self._resource_state()[node_key] for node_key in node_keys]
            policy_error = self._resource_policy_locked(
                user_id, node_keys, claim_duration=duration if command_has_duration else None
            )
            if policy_error:
                return policy_error
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
            delayed = set()
            group_recipients = set()

            if max_dur > 0 and user_id not in whitelist:
                for node in nodes:
                    booking_info = find_user_info(node["booking_list"], user_id)
                    if not command_has_duration and booking_info:
                        check_duration = booking_info["duration"]
                    else:
                        check_duration = duration
                    user_info = find_user_info(node["current_users"], user_id)
                    if user_info:
                        check_duration += user_info["duration"]
                        start_time = user_info["start_time"]
                    else:
                        start_time = timestamp
                    if remaining_duration(start_time, check_duration) > max_dur:
                        return self.show_error(
                            user_id,
                            t(
                                "error.lock_max_duration_exceeded",
                                config=self.config,
                                max_duration=format_duration(max_dur, config=self.config),
                                whitelist_hint=self._whitelist_hint(),
                            ),
                        )

            for node in nodes:
                node["status"] = "exclusive"
                booking_info = find_user_info(node["booking_list"], user_id)
                remove_user_info(node["booking_list"], user_id)

                if not command_has_duration and booking_info:
                    total_duration = booking_info["duration"]
                else:
                    total_duration = duration

                user_info = find_user_info(node["current_users"], user_id)
                if user_info or (booking_info and total_duration > booking_info["duration"]):
                    for booking_user in node["booking_list"]:
                        delayed.add(booking_user["user_id"])
                        if self._include_group_reminder(booking_user):
                            group_recipients.add(booking_user["user_id"])
                if not user_info:
                    user_info = create_user_info(user_id, total_duration, timestamp, config=self.config)
                else:
                    total_duration += user_info["duration"]

                user_info["duration"] = total_duration
                user_info["is_notified"] = False
                node["current_users"] = [user_info]

            observed_at = int(time.time())
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            self._save_and_notify()
            snapshot = self._capture_usage_rows(self._resource_state())

        usage = self._render_usage(snapshot, observed_at=observed_at)
        content = self._msg_with_usage("success.resource_locked", usage=usage)
        users_to_notify = {user_id, *group_recipients}
        if len(users_to_notify) > 1:
            notice = t("notify.wait_time_increased", config=self.config)
            if isinstance(content, list):
                content.append(notice)
            else:
                content += notice
        reply = self.adapter.build_reply(content, list(users_to_notify))
        return completed_reply(
            reply, "lock", reaction_safe=not delayed, queue_delay_recipients=sorted(group_recipients - {user_id})
        )

    def slock(self, user_id, command):
        return self.show_error(user_id, t("error.slock_not_supported", config=self.config))

    def book(self, user_id, command, *, acquisition_limit=None):
        """
        book nodes
        """
        parse_ok, error_reply, node_keys, duration = self._parse_resource_command(user_id, "book", command, True)
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        whitelist = self._duration_whitelist
        with self._state_guard():
            self._check_acquisition_limit_locked(user_id, node_keys, acquisition_limit)
            nodes = [self._resource_state()[node_key] for node_key in node_keys]
            policy_error = self._resource_policy_locked(user_id, node_keys)
            if policy_error:
                return policy_error
            if any(
                find_user_info(node["current_users"], user_id) or find_user_info(node["booking_list"], user_id)
                for node in nodes
            ):
                return self.show_error(user_id, self._msg_with_usage("error.already_locked", sep="\n"))

            timestamp = int(time.time())

            if max_dur > 0 and user_id not in whitelist and duration > max_dur:
                return self.show_error(
                    user_id,
                    t(
                        "error.lock_max_duration_exceeded",
                        config=self.config,
                        max_duration=format_duration(max_dur, config=self.config),
                        whitelist_hint=self._whitelist_hint(),
                    ),
                )

            for node in nodes:
                user_info = create_user_info(user_id, duration, timestamp, config=self.config)
                node["booking_list"].append(user_info)

            reply = self.adapter.build_reply(self._msg_with_usage("success.booking_added"), [user_id])
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            self._save_and_notify()
            return completed_reply(reply, "book", reaction_safe=False)

    def take(self, user_id, command):
        """
        take nodes
        """
        parse_ok, error_reply, node_keys, duration = self._parse_resource_command(user_id, "take", command, True)
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        whitelist = self._duration_whitelist
        with self._state_guard():
            resources = self._resource_state()
            nodes = [resources[node_key] for node_key in node_keys]
            policy_error = self._resource_policy_locked(user_id, node_keys, claim_duration=duration)
            if policy_error:
                return policy_error
            if any(find_user_info(node["current_users"], user_id) for node in nodes):
                return self.show_error(user_id, self._msg_with_usage("error.locked_user_cannot_take", sep="\n"))

            timestamp = int(time.time())
            users_to_notify = set()
            users_to_notify.add(user_id)
            if max_dur > 0 and user_id not in whitelist and remaining_duration(timestamp, duration) > max_dur:
                return self.show_error(
                    user_id,
                    t(
                        "error.lock_max_duration_exceeded",
                        config=self.config,
                        max_duration=format_duration(max_dur, config=self.config),
                        whitelist_hint=self._whitelist_hint(),
                    ),
                )

            requested = set(node_keys)
            affected_resources = {key: node for key, node in resources.items() if key in requested}
            before = self._capture_usage_rows(affected_resources)

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

                user_info["duration"] = total_duration
                user_info["is_notified"] = False
                node["current_users"] = [user_info]

            observed_at = int(time.time())
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            self._save_and_notify()
            after = self._capture_usage_rows(affected_resources)

        content = t("success.take_success_by", config=self.config, user_id=user_id)
        content += t("label.before_take", config=self.config) + self._render_usage(before, observed_at=timestamp)
        content += t("label.after_take", config=self.config) + self._render_usage(after, observed_at=observed_at)
        return self.adapter.build_reply(content, list(users_to_notify))

    def kicklock(self, user_id, command):
        """
        kicklock nodes
        """
        parse_ok, error_reply, node_keys, _ = self._parse_resource_command(user_id, "kicklock", command)
        if not parse_ok:
            return error_reply

        with self._state_guard():
            users = set([user_id])
            content = t("success.kicklock_cleared", config=self.config, user_id=user_id)
            content += self._msg_with_usage("label.before_release")
            result = self._clear_current_locks(node_keys)
            users.update(result["affected_users"])
            content += self._msg_with_usage("label.after_release")
            reply = self.adapter.build_reply(content, list(users))
            log_to_file(user_id, "kicklock", node_keys, config=self.config)
            self._save_and_notify()
            return reply

    def _clear_current_locks(self, node_keys):
        """Caller holds the state lock; bookings are deliberately untouched."""
        missing = [key for key in node_keys if key not in self._resource_state()]
        if missing:
            raise ValueError("Unknown resources: " + ", ".join(missing))
        freed, users = [], set()
        for key in node_keys:
            node = self._resource_state()[key]
            if node["current_users"]:
                freed.append(key)
                users.update(user["user_id"] for user in node["current_users"])
            node["status"] = "idle"
            node["current_users"] = []
        return {"ok": True, "freed": freed, "affected_users": sorted(users)}

    def do_kicklock(self, node_keys, reason=""):
        """OP resource release with native kicklock semantics, preserving the whole queue."""
        notify_reply = None
        with self._state_guard():
            result = self._clear_current_locks(node_keys)
            if result["freed"]:
                text = t("opkick.notify_kicked", config=self.config, node_key="\n  ".join(result["freed"]))
                if reason:
                    text += t("opkick.notify_kicked_reason", config=self.config, reason=reason)
                with suppress(Exception):
                    notify_reply = self.adapter.build_reply(text, result["affected_users"])
            log_to_file("api", "kicklock", node_keys, config=self.config)
            self._save_and_notify()
        # Slow delivery must not hold the shared resource-state lock.
        if notify_reply is not None:
            with suppress(Exception):
                self.adapter.send(notify_reply)
        return result

    def do_opkick(self, target_user, node_key=None, dev_ids=None, reason="", *, expected_holding_version=None):
        """API-driven operator kick for QueueBot: only releases current_users, preserves booking_list."""
        with self._state_guard():
            if expected_holding_version is not None:
                self._check_op_holding_version(expected_holding_version, node_key, None, target_user)
            if node_key:
                target_nodes = [node_key]
            else:
                target_nodes = [
                    k
                    for k, v in self._resource_state().items()
                    if any(u["user_id"] == target_user for u in v["current_users"])
                ]
            if not target_nodes:
                raise ValueError(f"User {target_user} has no active locks")

            freed = []
            for nk in target_nodes:
                node = self._resource_state().get(nk)
                if not node:
                    continue
                if find_user_info(node["current_users"], target_user):
                    remove_user_info(node["current_users"], target_user)
                    if not node["current_users"]:
                        node["status"] = "idle"
                    freed.append(nk)

            if not freed:
                raise ValueError(f"User {target_user} has no active locks")

            # Build the notification under the lock; send it after releasing the lock so a slow
            # or retrying adapter cannot block the non-reentrant state lock or the expiry sweep.
            notify_reply = None
            try:
                notify_msg = t("opkick.notify_kicked", config=self.config, node_key="\n  ".join(freed))
                if reason:
                    notify_msg += t("opkick.notify_kicked_reason", config=self.config, reason=reason)
                notify_reply = self.adapter.build_reply(notify_msg, [target_user])
            except Exception:
                notify_reply = None

            log_to_file("api", "opkick", freed, config=self.config)
            self._save_and_notify()

        if notify_reply is not None:
            with suppress(Exception):
                self.adapter.send(notify_reply)
        return {"ok": True, "freed": freed}

    def _help_header(self):
        parts = []
        parts.append(t("help.title", config=self.config))
        parts.append(t("help.section1_title", config=self.config))
        parts.append(
            t(
                "help.rule1_no_renewal" if self.config.get_val("FORBID_RELOCK") else "help.rule1_default_duration",
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
        parts.append(f"    {example_node0}\n")

        if self.config.get_val("ALLOW_USER_ALIAS"):
            parts.append(t("alias.help_section", config=self.config, node=example_node0, num=8))

        parts.append("\n")
        return "".join(parts)

    def _check_and_notify(self) -> float | None:
        """
        Check resource expiration and booking timeouts, release expired resources,
        and send notifications. Persist state only when changes occur.

        Returns: seconds until next interesting event, or None if no active locks/bookings.
        """
        queue_advanced = False
        state_changed = False
        alerts = []
        user_ids = set()
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")
        TIME_ALERT = self.config.get_val("TIME_ALERT")

        TIME_TO_LOCK = 5 * 60

        expired_users = []

        pending_keys = []

        def notify_first_booking_user(node_key, node, timestamp):
            """Notify the first booked user for this node."""
            nonlocal queue_advanced, state_changed

            if not node["booking_list"]:
                return

            first_user = node["booking_list"][0]
            # Claim time changes; preserve the booking incarnation used by task metadata.
            first_user.setdefault("booked_start_time", first_user.get("start_time", 0))
            first_user["is_notified"] = True
            first_user["start_time"] = timestamp
            queue_advanced = True
            state_changed = True
            if self._include_group_reminder(first_user):
                user_ids.add(first_user["user_id"])
                pending_keys.append(node_key)

        with self._state_guard():
            # 1. Release resources
            for node_key, node in self._resource_state().items():
                # === 1. Check if current_users have expired ===
                if node["status"] != "idle":
                    removed_users_id = []
                    for user_info in node["current_users"]:
                        remaining_time = remaining_duration(user_info["start_time"], user_info["duration"])
                        if remaining_time <= 0:
                            removed_users_id.append(user_info["user_id"])
                            state_changed = True

                            # Send expiry notification only if early warning was never sent.
                            # When EARLY_NOTIFY=True and warning fired on time, is_notified=True → silent release.
                            # When EARLY_NOTIFY=False, is_notified is always False → always notify here.
                            # Fallback: EARLY_NOTIFY=True but scheduler delayed past expiry → notify here instead.
                            if not user_info["is_notified"] and self._include_group_reminder(user_info):
                                user_ids.add(user_info["user_id"])
                                alerts.append((user_info["user_id"], node_key, node["status"], remaining_time))

                        if EARLY_NOTIFY and not user_info["is_notified"] and 0 < remaining_time <= TIME_ALERT:
                            user_info["is_notified"] = True
                            state_changed = True
                            if self._include_group_reminder(user_info):
                                user_ids.add(user_info["user_id"])
                                alerts.append((user_info["user_id"], node_key, node["status"], remaining_time))

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
                            if self._include_group_reminder(first_book_user):
                                expired_users.append((first_book_user["user_id"], node_key))
                                user_ids.add(first_book_user["user_id"])
                            state_changed = True
                            notify_first_booking_user(node_key, node, now)

            pending = self._pending_booking_rows(pending_keys)
            if state_changed:
                save_bot_state_to_file(self.state.bot_state, config=self.config)

            # Compute next wakeup after mutations
            min_next = float("inf")
            now_ts = int(time.time())
            for node in self._resource_state().values():
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

        content = self._render_lease_alerts(alerts) if alerts else ""
        if queue_advanced and (expired_users or pending):
            content += self._render_booking_reminder(expired_users, pending, TIME_TO_LOCK)
        if content:
            msg = self.adapter.build_reply(content, list(user_ids))
            try:
                self.adapter.send(msg)
            except Exception:
                self.logger.exception("Failed to send alert for bot %s", self.config.get_val("BOT_NAME"))

        return max(1.0, min_next) if min_next != float("inf") else None

    def _usage_rows(self, nodes):
        """Display-only rows; stored resources and their queues remain independent."""
        return nodes.items()

    def _pending_booking_rows(self, keys):
        if not keys:
            return []
        nodes = self._resource_state()
        selected = {key: nodes[key] for key in keys}
        rows = []
        for key, node in self._usage_rows(selected):
            user = node["booking_list"][0]
            rows.append((key, user["user_id"], user["duration"]))
        return rows

    def _render_booking_reminder(self, expired_users, pending, claim_seconds):
        text = t(
            "notify.resource_available_header",
            config=self.config,
            timeout=format_duration(claim_seconds, config=self.config),
        )
        if expired_users:
            text += t("notify.booking_expired_header", config=self.config)
            for user_id, node_key in expired_users:
                text += t("notify.booking_timeout_cancelled", config=self.config, user_id=user_id, node_key=node_key)
            text += "\n"
        if pending:
            text += t("notify.pending_bookings_header", config=self.config)
            for key, user_id, seconds in pending:
                duration = format_duration(seconds, config=self.config)
                text += f"  - {self._display_node(key)} {user_id} {duration}\n"
        return text + "\n"

    def _usage_labels(self, resource_key):
        return None, self._display_node(resource_key)

    def _capture_usage_rows(self, resources):
        """Group the display rows and detach only the fields used by the reply."""
        return tuple(
            (
                key,
                node["status"],
                tuple(
                    (user["user_id"], user["start_time"], user["duration"]) for user in node.get("current_users", ())
                ),
                tuple((user["user_id"], user["duration"]) for user in node.get("booking_list", ())),
            )
            for key, node in self._usage_rows(resources)
        )

    def _current_usage(self, node_filter=None):
        rows = self._capture_usage_rows(self._resource_state())
        return self._render_usage(rows, node_filter)

    def _render_usage(self, rows, node_filter=None, *, observed_at=None):
        usage_info = ""
        previous_group = None
        for node_key, status, holders, bookings in rows:
            if not self._resource_matches_filter(node_key, node_filter):
                continue
            group, display_name = self._usage_labels(node_key)
            indent = "  " if group else ""
            if group and group != previous_group:
                usage_info += f"{group}\n"
            previous_group = group
            if status == "idle":
                usage_info += f"{indent}{display_name} {t('status.idle', config=self.config)}\n"
            else:
                for user_idx, (user_id, start, seconds) in enumerate(holders):
                    node_name = display_name if user_idx == 0 else ""
                    duration = format_duration(remaining_duration(start, seconds, now=observed_at), config=self.config)
                    usage_info += f"{indent}{node_name} {user_id}  {duration}\n"

            if bookings:
                queue_indent = indent + "  " if group else ""
                usage_info += queue_indent + t("label.queue_list", config=self.config)
                accumulated_wait = max(
                    (remaining_duration(start, seconds, now=observed_at) for _, start, seconds in holders), default=0
                )
                for index, (user_id, seconds) in enumerate(bookings, start=1):
                    usage_info += queue_indent + t(
                        "label.queue_item",
                        config=self.config,
                        index=index,
                        user_id=user_id,
                        duration=format_duration(seconds, config=self.config),
                        wait_time=format_duration(accumulated_wait, config=self.config),
                    )
                    accumulated_wait += seconds
        return usage_info + "\n"
