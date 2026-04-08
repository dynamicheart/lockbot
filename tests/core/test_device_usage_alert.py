import pytest
from lockbot.core.device_usage_alert import format_alert_info, group_devices_by_node_user_and_mode


def test_group_devices_by_node_user_and_mode():
    alert_tuples = [
        ("张三", "node1", 1, "shared", 30),
        ("张三", "node1", 2, "shared", 30),
        ("张三", "node1", 4, "shared", 30),
        ("李四", "node1", 1, "shared", 30),
        ("李四", "node1", 2, "shared", 60),
        ("李四", "node1", 3, "exclusive", 60),
        ("张三", "node2", 1, "shared", 30),
    ]
    expected_result = [
        ("node1", "张三", (1, 2), "shared", 30),
        ("node1", "张三", (4, 4), "shared", 30),
        ("node2", "张三", (1, 1), "shared", 30),
        ("node1", "李四", (1, 1), "shared", 30),
        ("node1", "李四", (2, 2), "shared", 60),
        ("node1", "李四", (3, 3), "exclusive", 60),
    ]
    result = group_devices_by_node_user_and_mode(alert_tuples)
    assert result == expected_result

    # Empty input
    assert group_devices_by_node_user_and_mode([]) == []

    # Single device
    alert_tuples_single = [
        ("张三", "node1", 1, "shared", 30),
    ]
    expected_single = [
        ("node1", "张三", (1, 1), "shared", 30),
    ]
    assert group_devices_by_node_user_and_mode(alert_tuples_single) == expected_single


@pytest.mark.parametrize(
    "node_key, user_id, start, end, status, remaining_time, expected_output",
    [
        ("node1", "张三", 1, 1, "shared", 30 * 60, "node1 dev1 张三(共享)  30 分钟\n"),
        ("node1", "张三", 1, 2, "shared", 30 * 60, "node1 dev1-2 张三(共享)  30 分钟\n"),
        ("node1", "李四", 1, 1, "exclusive", 15 * 60, "node1 dev1 李四(独占)  15 分钟\n"),
        ("node2", "王五", 5, 5, "shared", 60 * 60, "node2 dev5 王五(共享)  1.0 小时\n"),
    ],
)
def test_format_alert_info(node_key, user_id, start, end, status, remaining_time, expected_output):
    result = format_alert_info(node_key, user_id, start, end, status, remaining_time)
    assert result == expected_output
