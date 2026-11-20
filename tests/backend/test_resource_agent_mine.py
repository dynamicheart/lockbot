"""Own-resource projection from the served package and real resource serializers."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import subprocess
import sys
import time

import pytest

from backend.test_packaged_cli_traffic import packaged_cli  # noqa: F401
from backend.test_resource_agent_helper import KEY, case, helper  # noqa: F401
from backend.test_resource_api_resources import ACTOR, api  # noqa: F401


@pytest.mark.parametrize(
    "api", [{"bot_type": "QUEUE_DEVICE", "cluster_configs": {"node-10": ["P800"] * 5}}], indirect=True
)
def test_packaged_mine_separates_exact_own_card_holdings_from_queued_cards(packaged_cli, api):
    run, connection, transport = packaged_cli
    now = int(time.time())

    def user(name, duration=7200):
        return {"user_id": name, "start_time": now, "duration": duration, "source": "api"}

    with api["bot"]._state_guard():
        cards = api["bot"].state.bot_state["node-10"]
        for card in cards:
            card.update(status="exclusive", current_users=[user("neighbor")], booking_list=[])
        cards[2]["current_users"] = [user(ACTOR)]
        cards[3]["booking_list"] = [user(ACTOR, 14400)]
        cards[4]["current_users"] = [user(ACTOR + "-other")]
    before = connection.read_bytes()
    output, events = run("--connection", str(connection), "resources", "--mine")
    summary = output["result"]
    assert [row["device"] for row in summary["holdings"]] == [2]
    assert [row["device"] for row in summary["bookings"]] == [3]
    held, booked = summary["holdings"][0], summary["bookings"][0]
    assert held["node"] == booked["node"] == "node-10"
    assert held["model"] == "P800" and held["mode"] == "exclusive"
    assert held["remaining_time"] in {"2h", "1h 59m"}
    assert booked["queue_rank"] == 1 and booked["claimable_by_you"] is False
    assert booked["mode"] is None
    assert not {"remaining_seconds", "expires_at", "remaining_time"} & booked.keys()
    assert "resources" not in summary and summary["scope"] == "own"
    assert summary["actor"] == summary["context"]["actor"]
    assert summary["snapshot_fresh_for_write"] is True
    assert [row["path"] for row in events] == ["/api/agent-skill", "/api/agent/resources"]
    exact, _ = run("--connection", str(connection), "resources", "--mine", "--exact-lease-times")
    assert 7100 < exact["result"]["holdings"][0]["remaining_seconds"] <= 7200
    assert exact["result"]["holdings"][0]["expires_at"]
    assert "remaining_seconds" not in exact["result"]["bookings"][0]
    full, _ = run("--connection", str(connection), "resources")
    assert len(full["result"]["resources"][0]["devices"]) == 5
    assert "holdings" not in full["result"] and "scope" not in full["result"]
    assert connection.read_bytes() == before
    assert all(row["method"] == "GET" for row in transport.events)


def test_packaged_mine_preserves_unknown_times_and_excludes_foreign_platform(case):
    case.state.holding = {"external_user_id": "alice"}
    case.state.booking = [{"external_user_id": "alice", "expires_at": "2030-01-02T00:00:00Z"}]
    argv = [
        sys.executable,
        "-S",
        str(case.root / helper.SCRIPT_PATH),
        "--connection",
        str(case.path),
        "resources",
        "--mine",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)["result"]
    assert summary["holdings"][0]["remaining_time"] is None
    assert summary["bookings"][0]["queue_rank"] is None
    assert summary["bookings"][0]["claimable_by_you"] is None
    assert "expires_at" not in summary["bookings"][0]
    assert KEY not in result.stdout + result.stderr
    case.state.holding["platform"] = "feishu"
    case.state.booking[0]["external_user_id"] = "alice-other"
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    summary = json.loads(result.stdout)["result"]
    assert summary["holdings"] == summary["bookings"] == []
    assert all(method == "GET" for _, method, *_ in case.state.requests)
