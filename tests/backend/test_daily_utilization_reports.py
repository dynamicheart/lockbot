from contextlib import nullcontext
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from lockbot.backend.app.resource_api import daily_reports as reports
from lockbot.core.config import Config


def frame(timestamp, allocations, *, digest="same"):
    return {
        "timestamp": timestamp,
        "state": {"complete": True, "allocations": allocations},
        "before_digest": "same",
        "after_digest": digest,
        "boundary": None,
    }


def allocation(user, resource, *, deadline=7200, mode="exclusive"):
    return {
        "actor": {"external_user_id": user},
        "resource": resource,
        "mode": mode,
        "start": 0,
        "deadline": deadline,
    }


def test_daily_report_uses_effective_sampled_card_time_and_simple_lines():
    holders = [
        allocation("brief", "a", deadline=600),
        allocation("longer", "b"),
        allocation("shared", "c", mode="shared"),
        allocation("short", "d", deadline=40),
    ]
    frames = [frame(0, holders), frame(7200, [])]
    samples = [("a", 0, 600, 4, 100), ("b", 0, 7200, 4, 60), ("c", 0, 7200, 4, 100)]
    totals = reports.summarize(frames, samples, 0, 7200)
    output = reports.render_report(date(2026, 11, 25), totals)
    assert output.splitlines()[3].startswith("longer")
    assert "60.0%" in output
    assert "\u6570\u636e\u4e0d\u8db3" in output
    assert "< 0.1 \u53f0\u65f6" in output
    assert "|" not in output and "\uff5c" not in output


def test_invalid_journal_boundary_and_partial_day_are_rejected():
    rows = [frame(0, []), frame(1200, [], digest="different")]
    rows[-1]["boundary"] = "session_start"
    assert reports.summarize(rows, [], 0, 1200) is None
    assert reports.summarize(rows[:1], [], 0, 1200) is None


@pytest.mark.parametrize("status", ["accepted", "unknown"])
def test_9am_delivery_is_durable_across_restart(tmp_path, monkeypatch, status):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 11, 26, 9, 0, tzinfo=zone).timestamp()
    day, start, end = reports.report_window(now, "Asia/Shanghai")
    assert day == date(2026, 11, 25)
    assert reports.report_window(now - 1, "Asia/Shanghai") is None
    sent = []

    def deliver(body, group):
        sent.append((body, group))
        return status, "fixture"

    data = SimpleNamespace(
        id=1,
        is_deleted=False,
        status="running",
        uuid="fixture",
        platform="Infoflow",
        bot_type="QUEUE",
        group_id="123",
        identity_revision=1,
    )
    bot = SimpleNamespace(
        adapter=SimpleNamespace(send_broadcast=deliver),
        config=Config({reports.SETTING: True, "_LIFECYCLE_BOT_UUID": data.uuid, "_IDENTITY_REVISION": 1}),
    )
    manager = SimpleNamespace(admit=lambda _bot_id: nullcontext(SimpleNamespace(bot=bot)))
    factory = lambda: nullcontext(SimpleNamespace(get=lambda _model, _id: data))  # noqa: E731
    worker = lambda: reports.DailyReports(  # noqa: E731
        manager, None, factory=factory, clock=lambda: now, database=tmp_path / "reports.db"
    )
    monkeypatch.setattr(reports, "report_available", lambda *_: True)
    monkeypatch.setattr(reports, "read_day_frames", lambda *_: [frame(start, []), frame(end, [])])

    worker().record([(data.uuid, 1, "a", start, start + 60, 4, 30, start)], now)
    worker().send_due(data.id)
    worker().send_due(data.id)
    assert len(sent) == 1 and sent[0][1] == "123"
    assert worker().report_samples(SimpleNamespace(uuid=data.uuid, identity_revision=2), start, end) is None


def test_bot_cannot_enable_daily_report_without_server_datasource(client, auth_header):
    body = {
        "name": "report-fixture",
        "bot_type": "QUEUE",
        "cluster_configs": {"node-a": "Example"},
        "webhook_url": "https://example.invalid/hook",
        "group_id": "123",
    }
    response = client.post("/api/bots", headers=auth_header, json={**body, "config_overrides": {reports.SETTING: True}})
    assert response.status_code == 422
    created = client.post("/api/bots", headers=auth_header, json=body)
    assert created.status_code == 201
    url = f"/api/bots/{created.json()['id']}"
    assert client.get(url, headers=auth_header).json()["daily_utilization_report_available"] is False
    update = client.put(url, headers=auth_header, json={"config_overrides": {reports.SETTING: True}})
    assert update.status_code == 422
    assert "configured server datasource" in update.json()["detail"]
