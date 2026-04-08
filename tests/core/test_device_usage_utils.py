import pytest
from lockbot.core.device_usage_utils import (
    get_current_usage,
    group_idle_devices,
    group_locked_devices,
    render_device_lines,
)


@pytest.fixture(autouse=True)
def mock_time():
    """Mock time.time() to return a fixed value of 1000000."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("time.time", lambda: 1000000)
        yield


@pytest.fixture
def make_device():
    """Factory that creates device status dicts with configurable fields."""

    def _make_device(
        dev_id,
        status="idle",
        user_id=None,
        start_time=None,
        duration=None,
        dev_model="a800",
    ):
        if isinstance(user_id, list):
            users = [
                {"user_id": uid, "start_time": start_time or 999400, "duration": duration or 600} for uid in user_id
            ]
        elif status != "idle" and user_id:
            users = [{"user_id": user_id, "start_time": start_time or 999400, "duration": duration or 600}]
        else:
            users = []

        return {
            "dev_id": dev_id,
            "status": status,
            "dev_model": dev_model,
            "current_users": users,
        }

    return _make_device


def test_group_locked_and_idle_and_render(make_device):
    """Test group locked and idle and render (homogeneous node)."""
    node_status = [make_device(i, status="exclusive", user_id="张三", duration=600, dev_model="a800") for i in range(8)]
    locked = group_locked_devices(node_status)
    idle = group_idle_devices(node_status, exclude_indices={i for _, ids in locked for i in ids})
    output = render_device_lines(node_status, locked, idle)

    assert any("张三" in line for line in output)
    assert any("dev0-7" in line or "dev0" in line for line in output)
    # Homogeneous node (all a800) — model should NOT be shown
    assert not any("a800" in line for line in output)


def test_get_current_usage_with_all_options(make_device):
    """Test get current usage with hetero node (auto-detect hetero warning)."""
    bot_state = {
        "nodeA": [make_device(i, status="exclusive", user_id="张三", duration=600, dev_model="a800") for i in range(8)]
    }
    output = get_current_usage("nodeA", bot_state, {})
    assert "张三" in output
    assert "dev0-7" in output or "dev0" in output
    # Homogeneous node — no hetero warning
    assert "❗️【注意nodeA的GPU顺序】" not in output


def test_get_current_usage_hetero_warning(make_device):
    """Test hetero warning auto-appears for mixed-model node."""
    bot_state = {
        "nodeA": [
            make_device(0, status="exclusive", user_id="张三", dev_model="a800"),
            make_device(1, status="exclusive", user_id="张三", dev_model="v100"),
        ]
    }
    output = get_current_usage("nodeA", bot_state, {})
    assert "❗️【注意nodeA的GPU顺序】" in output


def test_mixed_users_and_models(make_device):
    """Test mixed users and models (heterogeneous node — models shown)."""
    node_status = [
        make_device(0, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(1, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(2, status="exclusive", user_id="李四", dev_model="a800"),
        make_device(3, status="exclusive", user_id="张三", dev_model="v100"),
        make_device(4, status="idle", dev_model="a800"),
        make_device(5, status="idle", dev_model="v100"),
    ]

    locked = group_locked_devices(node_status)
    idle = group_idle_devices(node_status, exclude_indices={i for _, ids in locked for i in ids})
    lines = render_device_lines(node_status, locked, idle)

    assert any("dev0-1" in line and "张三" in line for line in lines)
    assert any("dev2" in line and "李四" in line for line in lines)
    assert any("dev3" in line and "张三" in line and "v100" in line for line in lines)
    assert any("dev4" in line and "空闲" in line and "a800" in line for line in lines)
    assert any("dev5" in line and "空闲" in line and "v100" in line for line in lines)


def test_shared_device_multiple_users(make_device):
    """Test shared device multiple users."""
    node_status = [
        {
            "dev_id": 0,
            "status": "shared",
            "dev_model": "a800",
            "current_users": [
                {"user_id": "张三", "start_time": 999400, "duration": 600},
                {"user_id": "李四", "start_time": 999400 - 100, "duration": 1500},
            ],
        }
    ]
    locked = group_locked_devices(node_status)
    assert len(locked) == 1

    idle = group_idle_devices(node_status, set())
    lines = render_device_lines(node_status, locked, idle)
    assert "空闲" not in "".join(lines)
    assert any("dev0" in line and "张三" in line for line in lines)
    assert any("dev0" not in line and "李四" in line for line in lines)


def test_locked_devices_different_timestamps(make_device):
    """Test locked devices different timestamps."""
    node_status = [
        make_device(0, status="exclusive", user_id="张三", start_time=999300),
        make_device(1, status="exclusive", user_id="张三", start_time=999500),
    ]
    locked = group_locked_devices(node_status)
    assert len(locked) == 2
    lines = render_device_lines(node_status, locked, [])
    assert any("dev0" in line for line in lines)
    assert any("dev1" in line for line in lines)


def test_idle_devices_split_if_non_continuous(make_device):
    """Test idle devices split if non continuous."""
    node_status = [
        make_device(0, status="idle", dev_model="a800"),
        make_device(1, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(2, status="idle", dev_model="a800"),
        make_device(3, status="idle", dev_model="a800"),
    ]
    locked = group_locked_devices(node_status)
    idle = group_idle_devices(node_status, exclude_indices={1})
    assert len(idle) == 2
    lines = render_device_lines(node_status, locked, idle)
    assert any("dev0" in line and "空闲" in line for line in lines)
    assert any("dev2" in line and "空闲" in line for line in lines)


def test_mixed_lock_conditions(make_device):
    """Test mixed lock conditions (heterogeneous — models shown)."""
    node_status = [
        make_device(0, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(1, status="exclusive", user_id="李四", dev_model="a800"),
        make_device(2, status="exclusive", user_id="张三", dev_model="v100"),
        make_device(3, status="shared", user_id="张三", dev_model="a800"),
    ]

    locked = group_locked_devices(node_status)
    assert len(locked) == 4
    lines = render_device_lines(node_status, locked, [])
    assert any("dev0" in line and "张三" in line for line in lines)
    assert any("dev1" in line and "李四" in line for line in lines)
    assert any("dev2" in line and "v100" in line for line in lines)
    assert any("dev3" in line and "共享" in line for line in lines)


def test_idle_devices_same_model_split(make_device):
    """Test idle devices same model split (homogeneous — no model shown)."""
    node_status = [
        make_device(0, status="idle", dev_model="a800"),
        make_device(1, status="idle", dev_model="a800"),
        make_device(2, status="exclusive", user_id="张三"),
        make_device(3, status="exclusive", user_id="张三"),
        make_device(4, status="idle", dev_model="a800"),
        make_device(5, status="idle", dev_model="a800"),
    ]
    locked = group_locked_devices(node_status)
    idle = group_idle_devices(node_status, exclude_indices={2, 3})
    lines = render_device_lines(node_status, locked, idle)

    assert any("dev0-1" in line for line in lines)
    assert any("dev4-5" in line for line in lines)
    assert not any("dev0-5" in line for line in lines)
    # Homogeneous — no model shown
    assert not any("a800" in line for line in lines)


def test_shared_device_multiple_users_with_continuous_devices(make_device):
    """Test shared device multiple users with continuous devices."""
    node_status = [
        {
            "dev_id": 0,
            "status": "shared",
            "dev_model": "a800",
            "current_users": [
                {"user_id": "张三", "start_time": 999400, "duration": 600},
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
        {
            "dev_id": 1,
            "status": "shared",
            "dev_model": "a800",
            "current_users": [
                {"user_id": "张三", "start_time": 999400, "duration": 600},
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
    ]
    locked = group_locked_devices(node_status)
    assert len(locked) == 1

    idle = group_idle_devices(node_status, set())
    lines = render_device_lines(node_status, locked, idle)
    assert any("dev0-1" in line and "张三" in line for line in lines)
    assert "dev0-1" not in "".join(line for line in lines if "李四" in line)
    assert "dev0-1" not in "".join(line for line in lines if "王五" in line)

    user_order = []
    for line in lines:
        if "张三" in line:
            user_order.append("张三")
        elif "李四" in line:
            user_order.append("李四")
        elif "王五" in line:
            user_order.append("王五")

    assert user_order == ["张三", "李四", "王五"]


def test_shared_device_multiple_users_with_modified_timestamp(make_device):
    """Test shared device multiple users with modified timestamp."""
    node_status = [
        {
            "dev_id": 0,
            "status": "shared",
            "dev_model": "a800",
            "current_users": [
                {"user_id": "张三", "start_time": 999400, "duration": 600},
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
        {
            "dev_id": 1,
            "status": "shared",
            "dev_model": "a800",
            "current_users": [
                {"user_id": "张三", "start_time": 999400, "duration": 600},
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
    ]

    node_status[1]["current_users"][1]["start_time"] = 999100

    locked = group_locked_devices(node_status)
    assert len(locked) == 2

    idle = group_idle_devices(node_status, set())
    lines = render_device_lines(node_status, locked, idle)

    assert any("dev0" in line and "张三" in line for line in lines)
    assert any("dev1" in line and "张三" in line for line in lines)
    assert "dev" not in "".join(line for line in lines if "李四" in line)
    assert "dev" not in "".join(line for line in lines if "李四" in line)

    user_order = []
    for line in lines:
        if "张三" in line:
            user_order.append("张三")
        elif "李四" in line:
            user_order.append("李四")
        elif "王五" in line:
            user_order.append("王五")

    assert user_order == ["张三", "李四", "王五", "张三", "李四", "王五"]


def test_mixed_shared_users_and_models(make_device):
    """Test mixed shared users and models (heterogeneous — models shown)."""
    node_status = [
        make_device(0, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(1, status="exclusive", user_id="张三", dev_model="a800"),
        make_device(2, status="exclusive", user_id="李四", dev_model="a800"),
        {
            "dev_id": 3,
            "status": "shared",
            "dev_model": "v100",
            "current_users": [
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
        {
            "dev_id": 4,
            "status": "shared",
            "dev_model": "v100",
            "current_users": [
                {"user_id": "李四", "start_time": 999300, "duration": 1500},
                {"user_id": "王五", "start_time": 999200, "duration": 1200},
            ],
        },
        make_device(5, status="idle", dev_model="a800"),
        make_device(6, status="idle", dev_model="a800"),
        make_device(7, status="idle", dev_model="v100"),
    ]

    locked = group_locked_devices(node_status)
    idle = group_idle_devices(node_status, exclude_indices={i for _, ids in locked for i in ids})
    lines = render_device_lines(node_status, locked, idle)

    assert any("dev0-1" in line and "张三" in line for line in lines)
    assert any("dev2" in line and "李四" in line for line in lines)
    assert any("dev3-4" in line and "李四" in line and "v100" in line for line in lines)
    assert all("dev" not in line and "v100" not in line for line in lines if "王五" in line)
    assert any("dev5-6" in line and "空闲" in line and "a800" in line for line in lines)
    assert any("dev7" in line and "空闲" in line and "v100" in line for line in lines)
