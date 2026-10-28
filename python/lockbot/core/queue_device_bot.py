"""FIFO reservations and exclusive locks for individual devices, using the queue engine."""

from lockbot.core.base_bot import BaseLockBot, BotState
from lockbot.core.device_bot import DeviceBot
from lockbot.core.i18n import t
from lockbot.core.io import create_or_load_device_state
from lockbot.core.queue_bot import QueueBot


def create_or_load_queue_device_state(config=None):
    state, clamped = create_or_load_device_state(config)
    for devices in state.values():
        for device in devices:
            device.setdefault("booking_list", [])
    return state, clamped


class QueueDeviceBot(QueueBot):
    """Keep DEVICE persistence and syntax; each card has QUEUE fairness and claim windows."""

    class _state_class(BotState):
        _loader = staticmethod(create_or_load_queue_device_state)

    _node_list_pattern = r"([\w-]+)((\s*[,，、]\s*([\w-])+)*)"

    def _resource_state(self):
        # References, not copies: the shared queue engine mutates the persisted card entries.
        return {
            f"{node}:dev{card['dev_id']}": card for node, devices in self.state.bot_state.items() for card in devices
        }

    def parse_command(self, user_id, command_key, command, parsing_duration=False):
        ok, error, nodes, devices, duration = DeviceBot.parse_command(
            self, user_id, command_key, command, parsing_duration
        )
        keys = [f"{node}:dev{index}" for node, indexes in zip(nodes, devices, strict=True) for index in indexes]
        return ok, error, keys, duration

    def _resource_matches_filter(self, key, selected):
        node = key.rsplit(":dev", 1)[0]
        return selected is None or node == selected or (isinstance(selected, list) and node in selected)

    def _display_node(self, key):
        if ":dev" not in key:
            return super()._display_node(key)
        node, index = key.rsplit(":dev", 1)
        label = f"{super()._display_node(node)} dev{index}"
        if self.config.get_val("DISPLAY_DEVICE_MODEL"):
            label += f" ({self.config.get_val('CLUSTER_CONFIGS')[node][int(index)]})"
        return label

    def _check_acquisition_limit_locked(self, user_id, node_keys, limit):
        # Preserve the existing API's per-node cap, including bookings on any card.
        nodes = [key.rsplit(":dev", 1)[0] for key in node_keys]
        return BaseLockBot._check_acquisition_limit_locked(self, user_id, nodes, limit)

    def do_opkick(self, target_user, node_key=None, dev_ids=None, reason=""):
        # DeviceBot already targets exact cards and leaves booking_list untouched.
        return DeviceBot.do_opkick(self, target_user, node_key=node_key, dev_ids=dev_ids, reason=reason)

    def _help_commands(self):
        node = next(iter(self.config.get_val("CLUSTER_CONFIGS")))
        sections = [
            ("help.rule3_lock_exclusive", f"lock {node} dev0 2h"),
            ("help.section2_title", f"free {node} dev0"),
            ("help.section_kickout_title", f"kickout {node} dev0"),
            ("help.section_booking_title", f"book {node} dev0 2h"),
            ("help.section_cancel_booking_title", f"free {node} dev0"),
            ("help.section_take_title", f"take {node} dev0 2h"),
            ("help.section_kicklock_title", f"kicklock {node} dev0"),
        ]
        text = "".join(t(title, config=self.config) + f"    {command}\n" for title, command in sections)
        text += t("help.free_all", config=self.config)
        text += t("help.section_help_title_queue", config=self.config)
        text += t("help.section_query_title_queue", config=self.config)
        return text + t("help.query_at_bot", config=self.config) + f"    {node}\n\n"
