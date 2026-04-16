"""Node lock bot with whole-node exclusive and shared locking support."""

import re
import time

from lockbot.core.base_bot import (
    BaseLockBot,
    BotState,  # noqa: F401  # re-export
)
from lockbot.core.i18n import t
from lockbot.core.io import (
    create_or_load_node_state,
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.utils import (
    create_user_info,
    duration_to_seconds,
    find_user_info,
    format_access_mode,
    format_duration,
    remaining_duration,
    remove_user_info,
)

# Regex building blocks for command parsing
_NODE_LIST = r"([\w\d]+)(\s*[,，]\s*[\w\d]+)*"  # node1,node2,...
_DURATION = r"([0-9]+\.?[0-9]*)([dhm])"  # e.g. 3d, 2.5h, 30m


class NodeBot(BaseLockBot):
    """
    NodeBot class.
    """

    class _state_class(BotState):
        _loader = staticmethod(create_or_load_node_state)

    def supported_commands(self):
        return ["lock", "slock", "unlock", "free", "kickout", "help", "h", "query"]

    def parse_command(self, user_id, command_key, command, parsing_duration=False):
        """
        Parse command
        """
        parse_ok = False
        error_reply = ""
        node_keys = []
        duration = 0

        def _get_return_values():
            return parse_ok, error_reply, node_keys, duration

        if parsing_duration:
            command_pattern = rf"^\s*({command_key})\s+({_NODE_LIST})\s*(\s{_DURATION})?\s*$"
        else:
            command_pattern = rf"^\s*({command_key})\s+({_NODE_LIST})\s*$"

        m = re.match(command_pattern, command)
        if not m:
            error_reply = self.print_help(
                user_id, t("error.invalid_command_format", config=self.config, command=command)
            )
            return _get_return_values()

        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")
        node_keys = [id.strip() for id in re.split(r"[,，]", m[2])]
        for _, node_key in enumerate(node_keys):
            if node_key not in cluster_configs:
                error_reply = self.show_error(
                    user_id,
                    t(
                        "error.invalid_node_key",
                        config=self.config,
                        node_key=node_key,
                        valid_keys=str(list(cluster_configs.keys())),
                    ),
                )
                return _get_return_values()

        node_keys = list(set(node_keys))

        if parsing_duration:
            if m[7]:
                duration_unit = m[7]
                duration = int(duration_to_seconds(float(m[6]), duration_unit))
            else:
                duration = self.config.get_val("DEFAULT_DURATION")

            if duration == 0:
                error_reply = self.show_error(user_id, t("error.duration_must_be_positive", config=self.config))
                return _get_return_values()

        parse_ok = True
        return _get_return_values()

    def query(self, user_id, node_key=None):
        """
        Query usage of a node
        """
        with self._lock:
            content = self._msg_with_usage("query.cluster_usage_title", node_key=node_key)
            return self.adapter.build_reply(content, [user_id])

    def lock(self, user_id, command):
        """
        Lock nodes
        """
        parse_ok, error_reply, node_keys, duration = self.parse_command(user_id, "lock", command, True)
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if not all(
                node["status"] == "idle"
                or (find_user_info(node["current_users"], user_id) and node["status"] == "exclusive")
                for node in nodes
            ):
                return self.show_error(user_id, self._msg_with_usage("error.node_in_use_or_shared"))

            timestamp = int(time.time())
            for node in nodes:
                node["status"] = "exclusive"

                total_duration = duration
                user_info = find_user_info(node["current_users"], user_id)
                if not user_info:
                    user_info = create_user_info(user_id, total_duration, timestamp, config=self.config)
                else:
                    total_duration += user_info["duration"]

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

            reply = self.adapter.build_reply(self._msg_with_usage("success.resource_locked"), [user_id])
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "lock", node_keys, duration, config=self.config)
            return reply

    def slock(self, user_id, command):
        """
        Share lock nodes
        """
        parse_ok, error_reply, node_keys, duration = self.parse_command(user_id, "slock", command, True)
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if not all(node["status"] != "exclusive" for node in nodes):
                return self.show_error(user_id, self._msg_with_usage("error.node_exclusive_mode"))

            timestamp = int(time.time())
            for node in nodes:
                node["status"] = "shared"
                user_info = find_user_info(node["current_users"], user_id)
                if not user_info:
                    user_info = create_user_info(user_id, duration, timestamp, config=self.config)
                    if max_dur > 0 and remaining_duration(user_info["start_time"], user_info["duration"]) > max_dur:
                        msg = t(
                            "error.slock_max_duration_exceeded",
                            config=self.config,
                            max_duration=format_duration(max_dur, config=self.config),
                        )
                        return self.show_error(user_id, msg)
                    node["current_users"].append(user_info)
                else:
                    if (
                        max_dur > 0
                        and remaining_duration(user_info["start_time"], user_info["duration"] + duration) > max_dur
                    ):
                        msg = t(
                            "error.slock_max_duration_exceeded",
                            config=self.config,
                            max_duration=format_duration(max_dur, config=self.config),
                        )
                        return self.show_error(user_id, msg)
                    user_info["duration"] += duration
                    user_info["is_notified"] = False

            reply = self.adapter.build_reply(self._msg_with_usage("success.resource_locked"), [user_id])
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "slock", node_keys, duration, config=self.config)
            return reply

    def unlock(self, user_id, command):
        """
        Unlock nodes
        """
        if re.match(r"^\s*(unlock|free)\s*$", command):
            with self._lock:
                for _, node in self.state.bot_state.items():
                    remove_user_info(node["booking_list"], user_id)
                    if node["status"] != "idle":
                        remove_user_info(node["current_users"], user_id)
                        if len(node["current_users"]) == 0:
                            node["status"] = "idle"
                reply = self.adapter.build_reply(
                    self._msg_with_usage("success.resource_released"),
                    [user_id],
                )
                save_bot_state_to_file(self.state.bot_state, config=self.config)
                log_to_file(user_id, "unlock", "all", config=self.config)
                return reply

        parse_ok, error_reply, node_keys, _ = self.parse_command(user_id, "unlock|free", command)
        if not parse_ok:
            return error_reply

        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            if not all(
                find_user_info(node["current_users"], user_id) or find_user_info(node["booking_list"], user_id)
                for node in nodes
            ):
                return self.show_error(user_id, self._msg_with_usage("error.node_not_requested"))
            for node in nodes:
                remove_user_info(node["current_users"], user_id)
                remove_user_info(node["booking_list"], user_id)
                if len(node["current_users"]) == 0:
                    node["status"] = "idle"
            reply = self.adapter.build_reply(
                self._msg_with_usage("success.resource_released"),
                [user_id],
            )
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "unlock", node_keys, config=self.config)
            return reply

    def kickout(self, user_id, command):
        """
        Kickout nodes
        """
        parse_ok, error_reply, node_keys, _ = self.parse_command(user_id, "kickout", command)
        if not parse_ok:
            return error_reply

        with self._lock:
            nodes = [self.state.bot_state[node_key] for node_key in node_keys]
            users = set([user_id])
            content = t("success.resource_force_released", config=self.config, user_id=user_id)
            content += self._msg_with_usage("label.before_release")
            for node in nodes:
                for user_info in node["current_users"]:
                    users.add(user_info["user_id"])
                for user_info in node["booking_list"]:
                    users.add(user_info["user_id"])
                node["status"] = "idle"
                node["current_users"] = []
                node["booking_list"] = []
            content += self._msg_with_usage("label.after_release")
            reply = self.adapter.build_reply(content, list(users))
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "kickout", node_keys, config=self.config)

            return reply

    def _help_commands(self):
        """Return NodeBot-specific command section for help text."""
        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")
        assert len(cluster_configs) >= 1
        itr = iter(cluster_configs)
        example_node0 = next(itr)
        if len(cluster_configs) <= 1:
            example_node1 = None
        else:
            example_node1 = next(itr)

        reply_info = t("help.rule3_lock_modes", config=self.config)
        reply_info += f"    lock {example_node0}\n"
        reply_info += f"    lock {example_node0} 3d\n"
        if example_node1 is not None:
            reply_info += f"    lock {example_node0},{example_node1} 2h\n"
        reply_info += f"    slock {example_node0} 30m\n"
        reply_info += t("help.section2_title", config=self.config)
        reply_info += f"    unlock {example_node0}\n"
        if example_node1 is not None:
            reply_info += f"    free {example_node0},{example_node1} \n"
        reply_info += t("help.free_all", config=self.config)
        reply_info += t("help.section3_title", config=self.config)
        reply_info += f"    kickout {example_node0} \n"
        if example_node1 is not None:
            reply_info += f"    kickout {example_node0},{example_node1} \n"
        reply_info += t("help.section4_title", config=self.config)
        reply_info += t("help.section5_title", config=self.config)
        reply_info += t("help.query_at_bot", config=self.config)
        reply_info += f"    {example_node0}\n\n"
        return reply_info

    def _check_and_notify(self) -> float | None:
        """
        Check resource expiration, release expired resources, and send notifications.
        Persists state only when changes occur.

        Returns: seconds until next interesting event, or None if no active locks.
        """
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")
        TIME_ALERT = self.config.get_val("TIME_ALERT")

        trigger_time_alert = False
        state_changed = False
        user_ids = set()
        alert_info = self._build_alert_header()

        with self._lock:
            # Release expired resources
            for node_key, node in self.state.bot_state.items():
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

            if state_changed:
                save_bot_state_to_file(self.state.bot_state, config=self.config)

            # Compute next wakeup: scan remaining active users after mutations
            min_next = float("inf")
            for node in self.state.bot_state.values():
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

        if trigger_time_alert:
            msg = self.adapter.build_reply(alert_info + "\n", list(user_ids))
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
                            uid = user_info["user_id"] + format_access_mode(node_status["status"], config=self.config)
                            duration = format_duration(
                                remaining_duration(user_info["start_time"], user_info["duration"]), config=self.config
                            )
                            usage_info += f"{node_name} {uid}  {duration}\n"
            usage_info += "\n"
            return usage_info

        usage_info = ""
        usage_info += _get_current_usage({k: v for k, v in self.state.bot_state.items()})

        return usage_info
