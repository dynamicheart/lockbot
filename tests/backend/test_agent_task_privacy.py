# ruff: noqa: F811
"""Privacy must hold at API/transport boundaries, including old free-form clients."""

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_other_user_cannot_read_private_notes_from_any_agent_read(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
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
    for endpoint in ("/resources", "/usage-declarations"):
        own = client.get(path + endpoint, headers=api["headers"])
        other = client.get(path + endpoint, headers=api["other_headers"])
        assert own.status_code == other.status_code == 200
        assert "private-experiment-123" in own.text and "private-agent-name" in own.text
        assert "private-experiment-123" not in other.text and "private-agent-name" not in other.text
        assert "operator_testing" in other.text
        assert '"authorization_label"' not in other.text and '"binding_id"' not in other.text


def test_legacy_notes_stay_private_and_categories_are_validated_before_execution(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}"
    for value in ("operator_testing secret-project", "secret-project", "", ["operator_testing"]):
        result = client.post(
            path + "/commands",
            headers=api["headers"],
            json={
                "command": "lock n1 1h",
                "task_category": value,
            },
        )
        assert result.status_code == 422
    assert not api["bot"].state.bot_state["n1"]["current_users"]
    result = client.post(
        path + "/commands",
        headers=api["headers"],
        json={
            "command": "lock n1 1h",
            "purpose": "legacy-private-note",
            "agent_label": "legacy-private-agent",
        },
    )
    assert result.status_code == 200
    other = client.get(path + "/resources", headers=api["other_headers"])
    assert "legacy-private" not in other.text
    result = client.put(
        path + "/usage-declarations/n1",
        headers=api["headers"],
        json={
            "purpose": "replacement-secret",
            "task_category": "invented-category",
        },
    )
    assert result.status_code == 422
    own = client.get(path + "/resources", headers=api["headers"])
    assert "legacy-private-note" in own.text and "replacement-secret" not in own.text


@pytest.mark.parametrize("endpoint", ["commands", "usage-declarations/n1"])
def test_validation_errors_do_not_echo_private_submissions(client, api, endpoint):
    body = {
        "task_category": "private-category-marker",
        "purpose": "private-purpose-marker" * 300,
        "agent_label": "private-agent-marker" * 30,
    }
    method = client.put
    if endpoint == "commands":
        method = client.post
        body["command"] = "lock n1 1h"
    response = method(f"/api/bots/{api['bot_id']}/{endpoint}", headers=api["headers"], json=body)
    assert response.status_code == 422
    assert "private-" not in response.text
    assert response.headers["cache-control"] == "no-store"
    errors = response.json()["detail"]
    assert all("input" not in error for error in errors)
    category = next(error for error in errors if error["loc"] == ["body", "task_category"])
    assert category["ctx"]["allowed_categories"]["unit_testing"] == "单元测试"
    assert not api["bot"].state.bot_state["n1"]["current_users"]


@pytest.mark.parametrize("endpoint", ["commands", "usage-declarations/n1"])
def test_category_error_lists_context_choices_without_dispatch(client, api, monkeypatch, endpoint):
    sent = []
    monkeypatch.setattr(api["bot"].adapter, "send", sent.append)
    context = client.get("/api/agent/context", headers=api["headers"]).json()
    body = {"task_category": "invented-category", "purpose": "private-note"}
    method = client.put
    if endpoint == "commands":
        method = client.post
        body["command"] = "lock n1 1h"
    response = method(f"/api/bots/{api['bot_id']}/{endpoint}", headers=api["headers"], json=body)
    assert response.status_code == 422
    error = next(item for item in response.json()["detail"] if item["loc"] == ["body", "task_category"])
    assert error["type"] == "invalid_task_category"
    assert error["ctx"]["allowed_categories"] == context["usage_privacy"]["public_task_categories"]
    assert error["ctx"]["allowed_categories"]["operator_testing"] == "算子测试"
    assert error["ctx"]["context_url"] == "/api/agent/context"
    assert "private-note" not in str(error["ctx"])
    assert not api["bot"].state.bot_state["n1"]["current_users"]
    assert sent == []


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
    assert policy["legacy_purpose_visibility"] == "private"
    assert policy["public_task_categories"]["operator_testing"] == "算子测试"
    assert "purpose" in policy["private_fields"]
