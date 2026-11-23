# ruff: noqa: F811
import pytest
from lockbot.backend.app.resource_api.usage import UsageDeclaration

from backend.test_resource_api_resources import api  # noqa: F401


def command(client, api, text, headers=None, **metadata):
    return client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=headers or api["headers"], json={"command": text, **metadata}
    )


def resources(client, api, headers=None):
    result = client.get(f"/api/bots/{api['bot_id']}/resources", headers=headers or api["headers"])
    assert result.status_code == 200, result.text
    return {item["id"]: item for item in result.json()["resources"]}


def test_one_call_registers_same_users_independent_agent_tasks(client, api):
    assert (
        command(
            client,
            api,
            "lock n1 1h",
            task_category="operator_development",
            agent_label="dev-agent",
            purpose="Develop feature A",
        ).status_code
        == 200
    )
    assert (
        command(
            client, api, "lock n2 30m", task_category="unit_testing", agent_label="test-agent", purpose="Regression B"
        ).status_code
        == 200
    )
    state = resources(client, api)
    assert state["n1"]["holders"][0]["usage"]["agent_label"] is None
    assert state["n1"]["holders"][0]["usage"]["task_category"] == "operator_development"
    assert state["n2"]["holders"][0]["usage"]["task_category"] == "unit_testing"
    assert state["n1"]["holders"][0]["usage"]["actor"]["external_user_id"] == "alice"


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_booking_task_survives_later_claim_without_a_second_declaration_call(client, api):
    assert command(client, api, "lock n1 1h").status_code == 200
    assert (
        command(
            client,
            api,
            "book n1 30m",
            headers=api["other_headers"],
            task_category="performance_testing",
        ).status_code
        == 200
    )
    assert (
        resources(client, api, api["other_headers"])["n1"]["booking_list"][0]["usage"]["task_category"]
        == "performance_testing"
    )
    assert command(client, api, "free n1").status_code == 200
    # Real scheduler notification rewrites the claim clock; it must retain the booked task.
    booking = api["bot"].state.bot_state["n1"]["booking_list"][0]
    original_start = booking["start_time"]
    from unittest.mock import patch

    with patch("lockbot.core.queue_bot.time.time", return_value=original_start + 90):
        api["bot"]._check_and_notify()
    assert booking["start_time"] == original_start + 90
    assert booking["booked_start_time"] == original_start
    assert (
        resources(client, api, api["other_headers"])["n1"]["booking_list"][0]["usage"]["task_category"]
        == "performance_testing"
    )
    assert command(client, api, "lock n1", headers=api["other_headers"]).status_code == 200
    usage = resources(client, api, api["other_headers"])["n1"]["holders"][0]["usage"]
    assert usage["task_category"] == "performance_testing"


def test_renewal_keeps_legacy_category_without_copying_task_details(client, api, seed_legacy_task_details):
    assert command(client, api, "lock n1 1h").status_code == 200
    seed_legacy_task_details(api, "legacy project", "legacy agent", task_category="debugging")
    assert command(client, api, "lock n1 2h").status_code == 200
    usage = resources(client, api)["n1"]["holders"][0]["usage"]
    assert usage["task_category"] == "debugging"
    assert usage["purpose"] == "" and usage["agent_label"] is None


def test_denied_acquisition_cannot_replace_another_users_task(client, api, db_session):
    command(client, api, "lock n1 1h", task_category="debugging")
    assert (
        command(client, api, "lock n1 1h", headers=api["other_headers"], task_category="code_review").status_code == 200
    )
    assert resources(client, api)["n1"]["holders"][0]["usage"]["task_category"] == "debugging"
    assert db_session.query(UsageDeclaration).count() == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"agent_label": "named-agent"},
        {"purpose": "Task only"},
    ],
)
def test_optional_metadata_and_noop_free_do_not_create_false_registrations(client, api, metadata):
    assert command(client, api, "lock n1 1h", **metadata).status_code == 200
    usage = resources(client, api)["n1"]["holders"][0]["usage"]
    assert usage is None
    assert command(client, api, "free n1", agent_label="release-agent", purpose="Finished").status_code == 200
    assert not resources(client, api)["n1"]["holders"]
    assert command(client, api, "lock n1 1h").status_code == 200
    assert resources(client, api)["n1"]["holders"][0]["usage"] is None


def test_db_failure_reports_uncertain_outcome_instead_of_replaying_lock(client, api, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated task store failure")

    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.store_declaration", fail)
    response = command(client, api, "lock n1 1h", task_category="operator_development")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "may_have_executed"
    state = resources(client, api)["n1"]
    assert state["holders"][0]["external_user_id"] == "alice" and state["holders"][0]["usage"] is None


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_device_command_also_registers_node_scoped_task(client, api):
    assert command(client, api, "lock n1 dev0 1h", task_category="functional_testing").status_code == 200
    usage = resources(client, api)["n1"]["devices"][0]["holders"][0]["usage"]
    assert usage["task_category"] == "functional_testing"


def test_api_metadata_leaves_native_lock_grammar_unchanged(client, api):
    bot = api["bot"]
    assert bot.parse_command("alice", "lock", "lock n1 1h", True)[0]
    assert not bot.parse_command("alice", "lock", "lock n1 1h purpose task", True)[0]
    assert command(client, api, "lock n1 1h", agent_label="dev", purpose="Separate JSON note").status_code == 200
    assert resources(client, api)["n1"]["holders"][0]["usage"] is None
