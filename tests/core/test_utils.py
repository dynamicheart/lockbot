import time
from unittest.mock import patch

import pytest
from lockbot.core.config import Config
from lockbot.core.utils import (
    apply_max_duration_limit,
    create_user_info,
    duration_to_seconds,
    find_user_info,
    format_duration,
    is_first_user,
    remaining_duration,
    remove_user_info,
)


@pytest.fixture
def default_user_info():
    """Default user info with None values for user_id, timestamp, and duration."""
    return {"user_id": None, "start_time": None, "duration": None}


@patch.object(Config, "get")
def test_create_user_info(mock_get, default_user_info):
    """Test create_user_info generates a user info dict with correct fields."""
    mock_get.return_value = default_user_info

    user_id = 123
    duration = 3600
    user_info = create_user_info(user_id, duration)

    assert user_info["user_id"] == user_id
    assert user_info["duration"] == duration

    current_timestamp = int(time.time())
    assert user_info["start_time"] is not None
    assert abs(user_info["start_time"] - current_timestamp) < 2

    custom_timestamp = 1672531199
    user_info = create_user_info(user_id, duration, start_time=custom_timestamp)

    assert user_info["start_time"] == custom_timestamp

    assert mock_get.call_count == 2
    mock_get.assert_any_call("DEFAULT_USER_INFO")


def test_find_user_info():
    """Test find_user_info returns matching user or None."""
    user_info_list = [{"user_id": 1}, {"user_id": 2}, {"user_id": 3}]
    assert find_user_info(user_info_list, 2) == {"user_id": 2}
    assert find_user_info(user_info_list, 4) is None


def test_is_first_user():
    # Empty list
    assert is_first_user([], "user1") is False

    # First user matches
    assert is_first_user([{"user_id": "user1"}, {"user_id": "user2"}], "user1") is True

    # First user does not match
    assert is_first_user([{"user_id": "user1"}, {"user_id": "user2"}], "user2") is False

    # Single user, matches
    assert is_first_user([{"user_id": "user3"}], "user3") is True

    # Single user, does not match
    assert is_first_user([{"user_id": "user3"}], "user4") is False


def test_remove_user_info():
    """Test remove_user_info removes the specified user from the list."""
    user_info_list = [{"user_id": 1}, {"user_id": 2}, {"user_id": 3}]
    remove_user_info(user_info_list, 2)
    assert len(user_info_list) == 2
    assert {"user_id": 2} not in user_info_list


def test_duration_to_seconds():
    """Test duration_to_seconds converts time units to seconds."""
    assert duration_to_seconds(1, "d") == 86400
    assert duration_to_seconds(1, "h") == 3600
    assert duration_to_seconds(1, "m") == 60
    with pytest.raises(AssertionError):
        duration_to_seconds(1, "s")


def test_format_duration():
    """Test format_duration returns human-readable duration strings."""
    assert format_duration(86400) == "1.0 天"
    assert format_duration(3600) == "1.0 小时"
    assert format_duration(60) == "1 分钟"


@patch("time.time", return_value=10000)
def test_remaining_duration(mock_time):
    """Test remaining_duration calculates time left correctly."""
    start_timestamp = 5000
    duration = 6000
    remaining = remaining_duration(start_timestamp, duration)
    assert remaining == 1000


@pytest.mark.parametrize(
    "timestamp_offset, duration, max_duration, expected_duration",
    [
        # elapsed=4000, remaining=1000, 1000 <= 3000, no adjustment
        (4000, 5000, 3000, 5000),
        # elapsed=1000, remaining=4000, 4000 > 3000, adjusted to 4000
        (1000, 5000, 3000, 4000),
        # elapsed=1000, remaining=1000, 1000 <= 3000, no adjustment
        (1000, 2000, 3000, 2000),
        # max_duration <= 0, no limit applied
        (10000, 20000, -1, 20000),
    ],
)
def test_apply_max_duration_limit_with_mock(timestamp_offset, duration, max_duration, expected_duration):
    """Test apply_max_duration_limit adjusts duration based on max limit."""
    fixed_now = 1_000_000
    user = {"user_id": "test_user", "start_time": fixed_now - timestamp_offset, "duration": duration}
    users = [user]

    with patch("time.time", return_value=fixed_now):
        apply_max_duration_limit(users, max_duration)

    actual_duration = users[0]["duration"]
    assert actual_duration == expected_duration, f"Expected duration {expected_duration}, got {actual_duration}"
