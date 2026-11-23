# ruff: noqa: F811
"""Privacy must hold at API/transport boundaries, including old free-form clients."""

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_legacy_notes_are_discarded_for_all_agent_readers(client, api, db_session):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    target = "n1 dev0" if api["bot"].config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"} else "n1"
    path = f"/api/bots/{api['bot_id']}"
    result = client.post(
        path + "/commands",
        headers=api["headers"],
        json={
            "command": f"lock {target} 1h",
            "purpose": "private-experiment-123",
            "agent_label": "private-agent-name",
            "task_category": "operator_testing",
        },
    )
    assert result.status_code == 200, result.text
    for headers in (api["headers"], api["other_headers"]):
        snapshot = client.get(path + "/resources", headers=headers)
        assert snapshot.status_code == 200
        assert "private-experiment-123" not in snapshot.text and "private-agent-name" not in snapshot.text
        assert "operator_testing" in snapshot.text
    row = db_session.query(UsageDeclaration).one()
    assert row.task_category == "operator_testing" and row.purpose == "" and row.agent_label is None


def test_validation_errors_do_not_echo_private_submissions(client, api):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={
            "command": "lock n1 1h",
            "task_category": "private-category-marker" * 10,
            "purpose": "private-purpose-marker" * 300,
            "agent_label": "private-agent-marker" * 30,
        },
    )
    assert response.status_code == 422
    assert "private-" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert all("input" not in error for error in response.json()["detail"])
    assert not api["bot"].state.bot_state["n1"]["current_users"]


def test_non_agent_validation_retains_its_existing_contract(client):
    response = client.post("/api/auth/login", json={})
    assert response.status_code == 422
    assert any("input" in error for error in response.json()["detail"])


def test_group_notifications_never_publish_private_fields_even_on_release(client, api, monkeypatch):
    from lockbot.backend.app.resource_api.notifications import notification_budget

    sent = []
    monkeypatch.setattr(
        api["bot"].adapter, "send_notification", lambda message: sent.append(message) or ("accepted", "ok")
    )
    path = f"/api/bots/{api['bot_id']}/commands"
    for command in ("lock n1 1h", "free n1"):
        notification_budget.reset()
        response = client.post(
            path,
            headers=api["headers"],
            json={
                "command": command,
                "purpose": "private-experiment-123",
                "agent_label": "private-agent-name",
                "task_category": "performance_testing",
            },
        )
        assert response.status_code == 200
    assert len(sent) == 2
    for message in sent:
        rendered = api["bot"].adapter.extract_reply_text(message)
        assert "性能测试" in rendered
        assert "private-experiment-123" not in rendered and "private-agent-name" not in rendered


def test_category_migration_preserves_legacy_values_and_is_repeatable(monkeypatch):
    from lockbot.backend.app import main

    old = create_engine("sqlite://")
    with old.begin() as conn:
        conn.execute(
            text("CREATE TABLE resource_usage_declarations (id INTEGER PRIMARY KEY, purpose TEXT, agent_label TEXT)")
        )
        conn.execute(text("INSERT INTO resource_usage_declarations VALUES (1, 'legacy note', 'legacy agent')"))
    monkeypatch.setattr(main, "engine", old)
    main._migrate_usage_task_category()
    main._migrate_usage_task_category()
    assert "task_category" in {col["name"] for col in inspect(old).get_columns("resource_usage_declarations")}
    with old.connect() as conn:
        assert conn.execute(
            text("SELECT purpose, agent_label, task_category FROM resource_usage_declarations")
        ).one() == (
            "legacy note",
            "legacy agent",
            None,
        )
        # An old-version projection still works with the added nullable column.
        assert conn.execute(text("SELECT purpose, agent_label FROM resource_usage_declarations")).one() == (
            "legacy note",
            "legacy agent",
        )
    old.dispose()


def test_context_advertises_machine_readable_privacy_and_categories(client, api):
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    policy = response.json()["usage_privacy"]
    assert "legacy_purpose_visibility" not in policy
    assert policy["public_task_categories"]["operator_testing"] == "算子测试"
    assert "purpose" in policy["private_fields"]
