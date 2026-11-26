from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fake_adapter import FakeAdapter
from lockbot.core.config import Config
from lockbot.core.device_bot import DeviceBot
from lockbot.core.node_bot import NodeBot
from lockbot.core.overnight import LeaseLimit, latest_overnight_window, window_settings
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot


def instant(date, clock, zone="Asia/Shanghai"):
    return int(datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=ZoneInfo(zone)).timestamp())


@pytest.mark.parametrize(
    "clock,minutes,accepted",
    [
        ("21:50", 600, False),
        ("22:00", 660, True),
        ("22:00", 720, False),
        ("02:00", 420, True),
        ("08:50", 1440, False),
        ("08:50", 240, True),
        ("08:50", 10, True),
        ("09:00", 600, False),
    ],
)
def test_regular_entitlement_and_overnight_exception(clock, minutes, accepted):
    now = instant("2026-12-31", clock)
    config = Config({"OVERNIGHT_ENABLED": True})
    limit = LeaseLimit.for_config(config, now, 4 * 3600)
    assert limit.rejects(None, minutes * 60) is not accepted


@pytest.mark.parametrize("start,end", [("17:59", "09:00"), ("22:00", "10:01"), ("00:00", "00:00"), ("18:60", "10:00")])
def test_invalid_window(start, end):
    with pytest.raises(ValueError):
        window_settings({"OVERNIGHT_START": start, "OVERNIGHT_END": end})


@pytest.mark.parametrize("start,end", [("18:00", "10:00"), ("20:00", "08:00"), ("23:59", "00:00")])
def test_year_boundary(start, end):
    config = Config({"OVERNIGHT_ENABLED": True, "OVERNIGHT_START": start, "OVERNIGHT_END": end})
    assert LeaseLimit.for_config(config, instant("2026-12-31", start), 3600).cutoff == instant("2027-01-01", end)


def test_spring_forward_uses_actual_instants():
    zone = "America/New_York"
    config = Config({"OVERNIGHT_ENABLED": True, "OVERNIGHT_TIMEZONE": zone})
    now = instant("2026-03-07", "22:00", zone)
    assert LeaseLimit.for_config(config, now, -1).remaining_limit == 34 * 3600


def test_idle_check_pause_does_not_enable_overnight_leases():
    config = Config({"OVERNIGHT_PAUSE_IDLE_CHECK": True})
    assert LeaseLimit.for_config(config, instant("2026-12-31", "22:00"), 3600).cutoff is None


@pytest.mark.parametrize(
    "day,clock,active",
    [
        ("2026-11-27", "21:59", False),
        ("2026-11-27", "22:00", True),
        ("2026-11-28", "14:00", True),
        ("2026-11-29", "14:00", True),
        ("2026-11-30", "08:59", True),
        ("2026-11-30", "09:00", False),
    ],
)
def test_weekend_has_one_cutoff(day, clock, active):
    config = Config({"OVERNIGHT_ENABLED": True})
    now = instant(day, clock)
    monday = instant("2026-11-30", "09:00")
    limit = LeaseLimit.for_config(config, now, -1)
    assert limit.cutoff == (monday if active else None)
    if active or clock == "09:00":
        assert latest_overnight_window(config, now) == (monday, True)


def test_weekend_respects_both_the_normal_maximum_and_monday_cutoff():
    config = Config({"OVERNIGHT_ENABLED": True})
    now = instant("2026-11-29", "22:00")
    capped = LeaseLimit.for_config(config, now, 4 * 3600)
    assert capped.remaining_limit == 4 * 3600
    assert capped.rejects(None, 5 * 3600)
    assert not capped.rejects(None, 4 * 3600)
    unlimited = LeaseLimit.for_config(config, now, -1)
    assert unlimited.remaining_limit == 11 * 3600
    assert not unlimited.rejects(None, 11 * 3600)
    assert unlimited.rejects(None, 11 * 3600 + 1)
    holder = {"start_time": now, "duration": 11 * 3600}
    assert unlimited.rejects(holder, 60)


@pytest.mark.parametrize(
    "cls,bot_type,operation",
    [
        (NodeBot, "NODE", "lock"),
        (NodeBot, "NODE", "slock"),
        (DeviceBot, "DEVICE", "lock"),
        (DeviceBot, "DEVICE", "slock"),
        (QueueBot, "QUEUE", "lock"),
        (QueueDeviceBot, "QUEUE_DEVICE", "lock"),
    ],
)
def test_overnight_allocation_restart_and_renewal(cls, bot_type, operation, tmp_path, monkeypatch):
    card = bot_type.endswith("DEVICE")
    now = instant("2026-12-31", "22:00")
    monkeypatch.setattr("time.time", lambda: now)
    config = {
        "BOT_ID": "test",
        "BOT_TYPE": bot_type,
        "DATA_DIR": str(tmp_path),
        "CLUSTER_CONFIGS": {"a": ["gpu"]} if card else ["a"],
        "OVERNIGHT_ENABLED": True,
        "DEFAULT_DURATION": 7200,
        "MAX_LOCK_DURATION": 14400,
    }
    bot = cls(config_dict=config, adapter=FakeAdapter())
    target = "a dev0" if card else "a"

    def users(instance):
        entry = instance.state.bot_state["a"]
        return (entry[0] if card else entry)["current_users"]

    getattr(bot, operation)("alice", f"{operation} {target} 12h")
    assert not users(bot)
    getattr(bot, operation)("alice", f"{operation} {target} 11h")
    assert users(bot)[0]["overnight_cutoff"] == instant("2027-01-01", "09:00")
    now += 60
    restored = cls(config_dict=config, adapter=FakeAdapter())
    assert users(restored)[0]["duration"] == 11 * 3600
    now = instant("2027-01-01", "08:55")
    getattr(restored, operation)("alice", f"{operation} {target} 10m")
    assert users(restored)[0]["duration"] == 11 * 3600


@pytest.mark.parametrize("auto_lock", [False, True], ids=["manual", "automatic"])
def test_queue_claim_rechecks_booked_duration(tmp_path, monkeypatch, auto_lock):
    now = instant("2026-12-31", "22:00")
    monkeypatch.setattr("time.time", lambda: now)
    bot = QueueBot(
        config_dict={
            "BOT_ID": "test",
            "BOT_TYPE": "QUEUE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["a"],
            "MAX_LOCK_DURATION": 14400,
            "OVERNIGHT_ENABLED": True,
        },
        adapter=FakeAdapter(),
    )
    bot.book("alice", "book a 8h")
    node = bot.state.bot_state["a"]
    assert node["booking_list"][0]["duration"] == 8 * 3600
    now = instant("2027-01-01", "09:01")
    if auto_lock:
        bot.config.set_val("QUEUE_AUTO_LOCK", True)
        bot._check_and_notify()
        (holding,) = node["current_users"]
        assert holding["start_time"] == now
        assert holding["user_id"] == "alice"
        assert holding["duration"] == 14400
        assert not node["booking_list"]
    else:
        bot.lock("alice", "lock a")
        assert not node["current_users"] and node["booking_list"][0]["user_id"] == "alice"


def test_weekend_lock_preserves_queue_order_and_rechecks_the_cutoff(tmp_path, monkeypatch):
    now = instant("2026-11-29", "22:00")
    monkeypatch.setattr("time.time", lambda: now)
    bot = QueueBot(
        config_dict={
            "BOT_ID": "test",
            "BOT_TYPE": "QUEUE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["a"],
            "MAX_LOCK_DURATION": 12 * 3600,
            "OVERNIGHT_ENABLED": True,
        },
        adapter=FakeAdapter(),
    )
    bot.book("alice", "book a 11h")
    node = bot.state.bot_state["a"]
    bot.lock("bob", "lock a 1h")
    assert not node["current_users"] and node["booking_list"][0]["user_id"] == "alice"
    now += 60
    bot.lock("alice", "lock a")
    assert not node["current_users"] and node["booking_list"][0]["user_id"] == "alice"
    bot.lock("alice", "lock a 10h")
    assert node["current_users"][0]["user_id"] == "alice"
