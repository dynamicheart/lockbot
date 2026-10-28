# ruff: noqa: F811
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.resource_api.usage import UsageDeclaration
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_api_resources import _clear_cache, api  # noqa: F401


def command(client, api, text, **extra):
    return client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": text, **extra})


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_each_bot_advertises_only_supported_examples(client, api):
    response = client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"]).json()
    examples = response["command_examples"]
    assert set(examples) == set(response["supported_operations"])
    if response["bot_type"] == "DEVICE":
        assert "dev0" in examples["lock"] and "book" not in examples
    elif response["bot_type"] == "QUEUE":
        assert "book" in examples and "slock" not in examples
    else:
        assert "slock" in examples and "book" not in examples


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_book_response_and_resource_state_tell_agent_to_explicitly_claim(client, api):
    booked = command(client, api, "book n1 30m", agent_label="queue-agent", purpose="Test queue")
    assert booked.status_code == 200 and "not ownership" in booked.json()["next_step"]
    path = f"/api/bots/{api['bot_id']}/resources"
    node = client.get(path, headers=api["headers"]).json()["resources"][0]
    assert node["claimable_by_you"] is True and node["claim_command"] == "lock n1"
    assert not node["holders"]
    other = client.get(path, headers=api["other_headers"]).json()["resources"][0]
    assert other["claimable_by_you"] is False and other["claim_command"] is None
    assert command(client, api, node["claim_command"]).status_code == 200
    node = client.get(path, headers=api["headers"]).json()["resources"][0]
    assert node["holders"][0]["external_user_id"] == "alice"
    assert node["holders"][0]["usage"]["purpose"] == "Test queue"
    assert node["claimable_by_you"] is False


def test_api_release_physically_removes_stale_task(client, api, db_session):
    command(client, api, "lock n1 1h", purpose="Task")
    assert db_session.query(UsageDeclaration).count() == 1
    command(client, api, "free n1", purpose="Finished")
    assert db_session.query(UsageDeclaration).count() == 0


def test_housekeeping_removes_native_release_but_keeps_new_current_task(
    client, api, db_session, session_factory, monkeypatch
):
    from lockbot.backend.app.resource_api import usage_cleanup

    monkeypatch.setattr(usage_cleanup, "SessionLocal", session_factory)
    monkeypatch.setattr(usage_cleanup, "bot_manager", api["manager"])
    command(client, api, "lock n1 1h", purpose="Old task")
    command(client, api, "lock n2 1h", purpose="Current task")
    api["bot"].unlock("alice", "free n1")  # native grammar, no metadata hook
    assert db_session.query(UsageDeclaration).count() == 2
    result = usage_cleanup.cleanup_usage_once()
    db_session.expire_all()
    assert result["removed"] == 1
    remaining = db_session.query(UsageDeclaration).one()
    assert remaining.resource_id == "n2" and remaining.purpose == "Current task"


def test_housekeeping_expired_and_revoked_declarations_are_removed(
    client, api, db_session, session_factory, monkeypatch
):
    from lockbot.backend.app.resource_api import usage_cleanup

    monkeypatch.setattr(usage_cleanup, "SessionLocal", session_factory)
    monkeypatch.setattr(usage_cleanup, "bot_manager", api["manager"])
    command(client, api, "lock n1 1h", purpose="Expired")
    row = db_session.query(UsageDeclaration).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert usage_cleanup.cleanup_usage_once()["removed"] == 1
    command(client, api, "lock n2 1h", purpose="Revoked")
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.revoked_at = datetime.utcnow()
    db_session.commit()
    assert usage_cleanup.cleanup_usage_once()["removed"] == 1
    assert db_session.query(UsageDeclaration).count() == 0


def test_mutation_allows_one_verification_poll_but_not_a_polling_bypass(client, api, monkeypatch):
    from lockbot.backend.app.resource_api.traffic import resource_tier_gate

    resource_tier_gate.active = resource_tier_gate.booking = resource_tier_gate.discovery = 60
    path = f"/api/bots/{api['bot_id']}/resources"
    assert client.get(path, headers=api["headers"]).status_code == 200
    assert client.get(path, headers=api["headers"]).status_code == 429
    assert command(client, api, "lock n1 1h", purpose="Verify after mutation").status_code == 200
    assert client.get(path, headers=api["headers"]).status_code == 200
    assert client.get(path, headers=api["headers"]).status_code == 429
    assert command(client, api, "query").status_code == 200
    assert client.get(path, headers=api["headers"]).status_code == 429


def test_resource_name_query_does_not_reset_poll_interval(client, api):
    from lockbot.backend.app.resource_api.traffic import resource_tier_gate

    resource_tier_gate.active = resource_tier_gate.booking = resource_tier_gate.discovery = 60
    path = f"/api/bots/{api['bot_id']}/resources"
    assert client.get(path, headers=api["headers"]).status_code == 200
    assert command(client, api, "n1").status_code == 200
    assert client.get(path, headers=api["headers"]).status_code == 429
