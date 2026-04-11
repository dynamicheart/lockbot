r"""Device (GPU) lock bot with exclusive and shared locking support."""

import re
import time

from lockbot.core.base_bot import BaseLockBot, BotState
from lockbot.core.device_usage_alert import (
    format_alert_info,
    group_devices_by_node_user_and_mode,
)
from lockbot.core.device_usage_utils import get_current_usage
from lockbot.core.i18n import t
from lockbot.core.io import (
    create_or_load_device_state,
    log_to_file,
    save_bot_state_to_file,
)
from lockbot.core.utils import (
    create_user_info,
    duration_to_seconds,
    find_user_info,
    format_duration,
    remaining_duration,
    remove_user_info,
)

# Regex building blocks for command parsing
_DEV_NODE_LIST = r"([\w\d]+)((\s*[,，、]\s*([\w\d])+)*)"  # node1,node2,...
_DEV_SPEC = r"(\s+dev\s*((([0-9]+(\s*[,，、]\s*[0-9]+)*)|([0-9]+-[0-9]+))))"  # dev 0,1,2 or dev 0-3
_DURATION = r"([0-9]+\.?[0-9]*)([dhm])"  # e.g. 3d, 2.5h, 30m


class DeviceBot(BaseLockBot):
    """
    DeviceBot class
    """

    class _state_class(BotState):
        _loader = staticmethod(create_or_load_device_state)

    def supported_commands(self):
        return ["lock", "slock", "unlock", "free", "kickout", "help", "h", "query"]

    def parse_command(self, user_id, command_key, command, parsing_duration=False):
        """
        parse command
        """
        parse_ok = False
        error_reply = ""
        node_key_list = []
        dev_ids_list = []
        duration = 0

        def _get_return_values():
            return parse_ok, error_reply, node_key_list, dev_ids_list, duration

        if parsing_duration:
            command_pattern = (
                rf"^\s*({command_key})\s+{_DEV_NODE_LIST}"
                rf"{_DEV_SPEC}?\s*(\s{_DURATION})?\s*$"
            )
        else:
            command_pattern = (
                rf"^\s*({command_key})\s+{_DEV_NODE_LIST}"
                rf"{_DEV_SPEC}?\s*$"
            )

        m = re.match(command_pattern, command)
        if not m:
            error_reply = self.print_help(user_id, t("error.invalid_command_format", config=self.config, command=""))
            return _get_return_values()

        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")

        node_key_str = m[2]
        if m[3]:
            node_key_str += m[3]

        node_key_list = [node_key.strip() for node_key in re.split(r"[,，、]", node_key_str)]

        for node_key in node_key_list:
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

        if m[7]:
            dev_str = m[7].strip()
            if "-" in dev_str:
                dev_min_max = dev_str.split("-")
                assert len(dev_min_max) == 2
                dev_min_max = [int(dev_min_max[0]), int(dev_min_max[1])]
                if dev_min_max[0] > dev_min_max[1]:
                    error_reply = self.show_error(
                        user_id,
                        t(
                            "error.dev_id_range_invalid",
                            config=self.config,
                            dev_min=dev_min_max[0],
                            dev_max=dev_min_max[1],
                        ),
                    )
                    return _get_return_values()
                dev_ids = list(range(dev_min_max[0], dev_min_max[1] + 1))
            else:
                dev_ids = [int(id) for id in re.split(r"[,，、]", dev_str)]
                dev_ids = list(set(dev_ids))
            dev_ids_list = [dev_ids] * len(node_key_list)
        else:
            # When dev is not specified, lock all devices on the node
            for node_key in node_key_list:
                dev_ids = list(range(len(cluster_configs[node_key])))
                dev_ids_list.append(dev_ids)

        for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
            num_devs = len(cluster_configs[node_key])
            if not (all([dev_id >= 0 for dev_id in dev_ids]) and all([dev_id < num_devs for dev_id in dev_ids])):
                error_reply = self.show_error(
                    user_id, t("error.dev_id_out_of_range", config=self.config, node_key=node_key, num_devs=num_devs)
                )
                return _get_return_values()

        if parsing_duration:
            if m[13]:
                duration_unit = m[14]
                duration = int(duration_to_seconds(float(m[13]), duration_unit))
            else:
                duration = self.config.get_val("DEFAULT_DURATION")

            if duration == 0:
                error_reply = self.show_error(user_id, t("error.duration_must_be_positive", config=self.config))
                return _get_return_values()

        parse_ok = True
        return _get_return_values()

    def query(self, user_id, node_key=None):
        """
        query current usage
        """
        with self._lock:
            reply = self.adapter.build_reply(
                self._msg_with_usage("query.cluster_usage_title", node_key=node_key), [user_id]
            )
            return reply

    def lock(self, user_id, command):
        """
        Lock specified devices and record the current user's usage. Returns an error if
        the device is exclusively held by another user or in shared mode. If the device
        is already exclusively held by the current user, updates the hold duration.

        Args:
            user_id (str): The user ID of the operator.
            command (str): Command string containing device ID and duration.
                Format: "<device_id> <duration>".

        Returns:
            dict: A dictionary containing:
                - "message": str, the reply message string.
                - "atuserids": list, containing the operator's user ID.
                - "content": str, the reply body including success prompt and current usage.

            If an error occurs, the returned dictionary will contain the error message.
        """
        parse_ok, error_reply, node_key_list, dev_ids_list, duration = self.parse_command(
            user_id, "lock", command, True
        )
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            timestamp = int(time.time())

            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]

                if not all(
                    device["status"] == "idle"
                    or (find_user_info(device["current_users"], user_id) and device["status"] == "exclusive")
                    for device in devices
                ):
                    return self.show_error(
                        user_id, self._msg_with_usage("error.device_in_use_or_shared", node_key=node_key)
                    )

            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]

                for device in devices:
                    device["status"] = "exclusive"

                    total_duration = duration
                    user_info = find_user_info(device["current_users"], user_id)
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
                    device["current_users"] = [user_info]

            reply = self.adapter.build_reply(
                self._msg_with_usage("success.resource_locked", node_key=node_key_list), [user_id]
            )

            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "lock", node_key, dev_ids, duration, config=self.config)

            return reply

    def slock(self, user_id, command):
        """
        slock command
        """
        parse_ok, error_reply, node_key_list, dev_ids_list, duration = self.parse_command(
            user_id, "slock", command, True
        )
        if not parse_ok:
            return error_reply

        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        with self._lock:
            timestamp = int(time.time())
            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]

                if not all(device["status"] != "exclusive" for device in devices):
                    return self.show_error(
                        user_id, self._msg_with_usage("error.device_exclusive_mode", node_key=node_key)
                    )

            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]
                for device in devices:
                    device["status"] = "shared"

                    user_info = find_user_info(device["current_users"], user_id)
                    if not user_info:
                        user_info = create_user_info(user_id, duration, timestamp, config=self.config)
                        if max_dur > 0 and remaining_duration(user_info["start_time"], user_info["duration"]) > max_dur:
                            return self.show_error(
                                user_id,
                                t(
                                    "error.slock_max_duration_exceeded",
                                    config=self.config,
                                    max_duration=format_duration(max_dur, config=self.config),
                                ),
                            )
                        device["current_users"].append(user_info)
                    else:
                        if (
                            max_dur > 0
                            and remaining_duration(user_info["start_time"], user_info["duration"] + duration) > max_dur
                        ):
                            return self.show_error(
                                user_id,
                                t(
                                    "error.slock_max_duration_exceeded",
                                    config=self.config,
                                    max_duration=format_duration(max_dur, config=self.config),
                                ),
                            )
                        user_info["duration"] += duration
                        user_info["is_notified"] = False

            reply = self.adapter.build_reply(
                self._msg_with_usage("success.resource_locked", node_key=node_key_list), [user_id]
            )

            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "slock", node_key_list, dev_ids_list, duration, config=self.config)
            return reply

    def unlock(self, user_id, command):
        """
        unlock command
        """
        # Case 1: Release all resources requested by the user
        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")

        if re.match(r"^\s*(unlock|free)\s*$", command):
            with self._lock:
                for _, devices in self.state.bot_state.items():
                    for device in devices:
                        if device["status"] != "idle":
                            remove_user_info(device["current_users"], user_id)
                            if len(device["current_users"]) == 0:
                                device["status"] = "idle"
                reply = self.adapter.build_reply(self._msg_with_usage("success.resource_released"), [user_id])
                save_bot_state_to_file(self.state.bot_state, config=self.config)
                log_to_file(user_id, "unlock", "all", "all", config=self.config)
                return reply

        m = re.match(r"^\s*(unlock|free)\s+([\w\d]+)(\s*[,，、]\s*([\w\d])+)*\s*$", command)
        if m:
            node_key_str = m[2]
            if m[3]:
                node_key_str += m[3]

            node_key_list = [node_key.strip() for node_key in re.split(r"[,，、]", node_key_str)]
            for node_key in node_key_list:
                if node_key not in cluster_configs:
                    return self.show_error(
                        user_id,
                        t(
                            "error.invalid_node_key",
                            config=self.config,
                            node_key=node_key,
                            valid_keys=str(list(cluster_configs)),
                        ),
                    )

            with self._lock:
                for node_key in node_key_list:
                    devices = self.state.bot_state[node_key]
                    for device in devices:
                        if device["status"] != "idle":
                            remove_user_info(device["current_users"], user_id)
                            if len(device["current_users"]) == 0:
                                device["status"] = "idle"
                reply = self.adapter.build_reply(self._msg_with_usage("success.resource_released"), [user_id])
                save_bot_state_to_file(self.state.bot_state, config=self.config)
                log_to_file(user_id, "unlock", node_key, "all", config=self.config)
                return reply

        # Case 3: Release specific devices requested by the user
        parse_ok, error_reply, node_key_list, dev_ids_list, _ = self.parse_command(user_id, "unlock|free", command)
        if not parse_ok:
            return error_reply

        with self._lock:
            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]
                if not all(find_user_info(device["current_users"], user_id) for device in devices):
                    return self.show_error(
                        user_id, self._msg_with_usage("error.device_not_requested", node_key=node_key)
                    )

            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]
                for device in devices:
                    remove_user_info(device["current_users"], user_id)
                    if len(device["current_users"]) == 0:
                        device["status"] = "idle"

            reply = self.adapter.build_reply(
                self._msg_with_usage("success.resource_released", node_key=node_key_list), [user_id]
            )
            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "unlock", node_key_list, dev_ids_list, config=self.config)
            return reply

    def kickout(self, user_id, command):
        """
        kickout command
        """
        parse_ok, error_reply, node_key_list, dev_ids_list, _ = self.parse_command(user_id, "kickout", command)
        if not parse_ok:
            return error_reply

        with self._lock:
            users = set([user_id])

            content = t("success.resource_force_released", config=self.config, user_id=user_id)
            content += self._msg_with_usage("label.before_release", node_key=node_key_list)

            for node_key, dev_ids in zip(node_key_list, dev_ids_list, strict=True):
                node_status = self.state.bot_state[node_key]
                devices = [node_status[dev_id] for dev_id in dev_ids]
                for device in devices:
                    for user_info in device["current_users"]:
                        users.add(user_info["user_id"])
                    device["status"] = "idle"
                    device["current_users"] = []

            content += self._msg_with_usage("label.after_release", node_key=node_key_list)
            reply = self.adapter.build_reply(content, list(users))

            save_bot_state_to_file(self.state.bot_state, config=self.config)
            log_to_file(user_id, "kickout", node_key_list, dev_ids_list, config=self.config)

            return reply

    def _help_commands(self):
        """
        Return the device-specific command help text (between rules and footer).
        """
        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")
        example_node = next(iter(cluster_configs))

        text = t("help.rule3_lock_modes", config=self.config)
        text += t("help.lock_all_devices_example", config=self.config, node=example_node)
        text += t("help.lock_device_example", config=self.config, node=example_node)
        text += t("help.lock_device_duration_example", config=self.config, node=example_node)
        text += t("help.lock_device_range_example", config=self.config, node=example_node)
        text += t("help.slock_device_range_example", config=self.config, node=example_node)
        text += t("help.section2_title", config=self.config)
        text += t("help.unlock_device_example", config=self.config, node=example_node)
        text += t("help.unlock_device_range_example", config=self.config, node=example_node)
        text += t("help.free_device_range_example", config=self.config, node=example_node)
        text += t("help.free_node_all_example", config=self.config, node=example_node)
        text += t("help.free_all", config=self.config)
        text += t("help.section3_title", config=self.config)
        text += t("help.kickout_device_example", config=self.config, node=example_node)
        text += t("help.kickout_device_range_example", config=self.config, node=example_node)
        text += t("help.kickout_device_range2_example", config=self.config, node=example_node)
        text += t("help.section4_title", config=self.config)
        text += t("help.section5_title", config=self.config)
        text += t("help.query_at_bot", config=self.config)
        text += t("help.query_node_example", config=self.config, node=example_node)
        text += "\n"

        return text

    def _check_and_notify(self):
        """
        Check and notify when resource availability time is running low or resources have been released.
        If EARLY_NOTIFY is set, sends an early notification when remaining time is less than TIME_ALERT.
        Otherwise, notifies after resources are automatically released.

        Returns: None. Modifies bot_state and user_ids as side effects.
        """
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")
        TIME_ALERT = self.config.get_val("TIME_ALERT")

        trigger_time_alert = False
        state_changed = False
        user_ids = set()
        alert_info = self._build_alert_header()

        alert_tuples = []

        with self._lock:
            for node_key, devices in self.state.bot_state.items():
                for device in devices:
                    if device["status"] != "idle":
                        removed_users_id = []
                        for user_info in device["current_users"]:
                            remaining_time = remaining_duration(user_info["start_time"], user_info["duration"])
                            if remaining_time <= 0:
                                removed_users_id.append(user_info["user_id"])
                                state_changed = True
                                if not EARLY_NOTIFY:
                                    trigger_time_alert = True
                                    user_ids.add(user_info["user_id"])
                                    alert_tuples.append(
                                        (
                                            user_info["user_id"],
                                            node_key,
                                            device["dev_id"],
                                            device["status"],
                                            remaining_time,
                                        )
                                    )
                            if EARLY_NOTIFY and not user_info.get("is_notified") and remaining_time <= TIME_ALERT:
                                trigger_time_alert = True
                                user_ids.add(user_info["user_id"])
                                user_info["is_notified"] = True
                                state_changed = True

                                alert_tuples.append(
                                    (
                                        user_info["user_id"],
                                        node_key,
                                        device["dev_id"],
                                        device["status"],
                                        remaining_time,
                                    )
                                )

                        for user_id in removed_users_id:
                            remove_user_info(device["current_users"], user_id)

                        if len(device["current_users"]) == 0:
                            device["status"] = "idle"

            if state_changed:
                save_bot_state_to_file(self.state.bot_state, config=self.config)

        grouped_devices = group_devices_by_node_user_and_mode(alert_tuples)
        for node_key, user_id, (dev_start, dev_end), status, remaining_time in grouped_devices:
            alert_info += format_alert_info(node_key, user_id, dev_start, dev_end, status, remaining_time)

        if trigger_time_alert:
            msg = self.adapter.build_reply(alert_info + "\n", list(user_ids))
            self.adapter.send(msg)

    def _current_usage(self, node_filter=None):
        """
        Get a text description of the current cluster device usage (supports merged display and heterogeneous hints).
        """
        return get_current_usage(node_filter, self.state.bot_state, {}, config=self.config)
