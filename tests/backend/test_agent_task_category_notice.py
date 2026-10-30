# ruff: noqa: F811
"""Optional public categories guide new requests without replaying existing mutations."""

import copy

import pytest
from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES

from backend.test_resource_api_resources import api  # noqa: F401


def _target(api):
    return "n1 dev0" if api["bot"].config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"} else "n1"


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
@pytest.mark.parametrize("category", ["omitted", None, "operator_testing"])
def test_legacy_omission_is_guidance_but_explicit_category_or_null_is_respected(client, api, monkeypatch, category):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    body = {
        "command": f"lock {_target(api)} 1h",
        "purpose": "PRIVATE_NOTE_CANARY",
        "agent_label": "PRIVATE_AGENT_CANARY",
    }
    if category != "omitted":
        body["task_category"] = category
    response = client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reply"].strip()
    assert "skill_update" in payload
    if category == "omitted":
        notice = payload["task_category_notice"]
        assert notice["code"] == "optional_public_task_category"
        assert notice["optional"] and notice["next_request_only"] and notice["do_not_replay_command"]
        assert notice["allowed_categories"] == TASK_CATEGORIES
        assert notice["context_url"] == "/api/agent/context"
        assert "null" in notice["message"]
        assert "PRIVATE_" not in str(notice)
        assert notice["message"] not in payload["reply"]
    else:
        assert "task_category_notice" not in payload
    # The legacy omitted field is not a refusal: the original request actually acquired a resource.
    state = api["bot"].state.bot_state["n1"]
    row = state[0] if isinstance(state, list) else state
    assert [holder["user_id"] for holder in row["current_users"]] == ["alice"]


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_invalid_category_rejected_before_dispatch_for_all_bot_types(client, api, monkeypatch):
    before = copy.deepcopy(api["bot"].state.bot_state)
    monkeypatch.setattr(
        "lockbot.backend.app.resource_api.commands.execute_text_command",
        lambda *args, **kwargs: pytest.fail("invalid category must not dispatch"),
    )
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": f"lock {_target(api)} 1h", "task_category": "PRIVATE_INVALID_CANARY"},
    )
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["ctx"]["allowed_categories"] == TASK_CATEGORIES
    assert error["ctx"]["context_url"] == "/api/agent/context"
    assert "PRIVATE_INVALID_CANARY" not in response.text
    assert api["bot"].state.bot_state == before


@pytest.mark.parametrize("api", ["NODE", "DEVICE"], indirect=True)
def test_shared_lock_missing_category_has_notice(client, api):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": f"slock {_target(api)} 1h"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task_category_notice"]["next_request_only"] is True


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_booking_notice_preserves_claim_next_step(client, api):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": f"book {_target(api)} 1h"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_category_notice"]["do_not_replay_command"]
    assert "claim_command" in payload["next_step"] and "skill_update" in payload


@pytest.mark.parametrize("command", ["query", "n1", "free n1", "unlock n1", "alias n1 public-label"])
def test_non_acquisition_commands_do_not_repeat_category_notice(client, api, command):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command}
    )
    assert response.status_code == 200, response.text
    assert "task_category_notice" not in response.json()


def test_refused_command_is_not_a_category_notice(client, api):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": "help"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "command_not_allowed"
    assert "task_category_notice" not in response.json()


def test_public_openapi_documents_optional_notice_without_changing_required_request(client):
    document = client.get("/api/public-openapi.json").json()
    schemas = document["components"]["schemas"]
    assert "task_category_notice" not in schemas["CommandResponse"].get("required", [])
    assert "task_category" not in schemas["CommandRequest"].get("required", [])
    assert schemas["TaskCategoryNotice"]["properties"]["do_not_replay_command"]["default"] is True
