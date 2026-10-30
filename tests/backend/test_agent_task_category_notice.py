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
        assert notice["reason"] == "missing"
        assert notice["optional"] and notice["next_request_only"] and notice["do_not_replay_command"]
        assert notice["allowed_categories"] == TASK_CATEGORIES
        assert notice["context_url"] == "/api/agent/context"
        assert "null" in notice["guidance"]
        assert "PRIVATE_" not in str(notice)
        assert notice["guidance"] not in payload["reply"]
    else:
        assert "task_category_notice" not in payload
    # The legacy omitted field is not a refusal: the original request actually acquired a resource.
    state = api["bot"].state.bot_state["n1"]
    row = state[0] if isinstance(state, list) else state
    assert [holder["user_id"] for holder in row["current_users"]] == ["alice"]


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
@pytest.mark.parametrize("category", ["UNKNOWN_CATEGORY_CANARY", "", "  "])
def test_unsupported_string_is_ignored_and_dispatches_once(client, api, monkeypatch, db_session, category):
    from lockbot.backend.app.resource_api import commands
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    original = commands.execute_text_command
    calls = []

    def dispatch(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(commands, "execute_text_command", dispatch)
    sent = []
    monkeypatch.setattr(commands, "notify_group", lambda *args, **kwargs: sent.append(kwargs))
    command = f"lock {_target(api)} 1h"
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": command, "task_category": category},
    )
    assert response.status_code == 200, response.text
    notice = response.json()["task_category_notice"]
    assert notice["reason"] == "unsupported_category"
    assert notice["allowed_categories"] == TASK_CATEGORIES
    assert notice["context_url"] == "/api/agent/context"
    assert notice["do_not_replay_command"]
    assert "UNKNOWN_CATEGORY_CANARY" not in response.text
    assert calls == [command]
    assert all(item["task_category"] is None for item in sent)
    assert not db_session.query(UsageDeclaration).filter(UsageDeclaration.task_category.is_not(None)).count()
    state = api["bot"].state.bot_state["n1"]
    row = state[0] if isinstance(state, list) else state
    assert [holder["user_id"] for holder in row["current_users"]] == ["alice"]


@pytest.mark.parametrize("endpoint", ["commands", "usage-declarations/n1"])
@pytest.mark.parametrize(
    "category", [["operator_testing"], 1, {"id": "operator_testing"}, "x" * 49, " " * 49, "bad\ncategory", "bad\n"]
)
def test_malformed_categories_still_reject_without_dispatch(client, api, monkeypatch, endpoint, category):
    monkeypatch.setattr(
        "lockbot.backend.app.resource_api.commands.execute_text_command",
        lambda *args, **kwargs: pytest.fail("malformed category must not dispatch"),
    )
    before = copy.deepcopy(api["bot"].state.bot_state)
    body = {"task_category": category}
    if endpoint == "commands":
        body["command"] = "lock n1 1h"
    response = getattr(client, "post" if endpoint == "commands" else "put")(
        f"/api/bots/{api['bot_id']}/{endpoint}", headers=api["headers"], json=body
    )
    assert response.status_code == 422
    assert api["bot"].state.bot_state == before


@pytest.mark.parametrize("category", ["omitted", None, "operator_testing", "UNKNOWN_CATEGORY_CANARY", ""])
def test_declaration_notice_sanitizes_metadata_without_changing_lease(client, api, monkeypatch, db_session, category):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}"
    assert client.post(path + "/commands", headers=api["headers"], json={"command": "lock n1 1h"}).status_code == 200
    before = copy.deepcopy(api["bot"].state.bot_state)
    body = {"purpose": "DETAILED_NOTE_CANARY"}
    if category != "omitted":
        body["task_category"] = category
    response = client.put(path + "/usage-declarations/n1", headers=api["headers"], json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    if category in {"omitted", "UNKNOWN_CATEGORY_CANARY", ""}:
        notice = payload["task_category_notice"]
        assert notice["reason"] == ("missing" if category == "omitted" else "unsupported_category")
        assert notice["allowed_categories"] == TASK_CATEGORIES
        assert "DETAILED_NOTE_CANARY" not in str(notice)
    else:
        assert "task_category_notice" not in payload
    assert "UNKNOWN_CATEGORY_CANARY" not in response.text
    assert api["bot"].state.bot_state == before
    assert db_session.query(UsageDeclaration).one().task_category == (
        "operator_testing" if category == "operator_testing" else None
    )
    other = client.get(path + "/usage-declarations", headers=api["other_headers"])
    assert "DETAILED_NOTE_CANARY" not in other.text and "UNKNOWN_CATEGORY_CANARY" not in other.text


@pytest.mark.parametrize("category", [None, "UNKNOWN_CATEGORY_CANARY"])
def test_explicit_optout_or_ignored_category_clears_inherited_public_category(
    client, api, monkeypatch, db_session, category
):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}/commands"
    first = {"command": "lock n1 1h", "task_category": "operator_testing", "purpose": "KEEP_DETAIL"}
    assert client.post(path, headers=api["headers"], json=first).status_code == 200
    response = client.post(path, headers=api["headers"], json={"command": "lock n1 2h", "task_category": category})
    assert response.status_code == 200, response.text
    row = db_session.query(UsageDeclaration).one()
    db_session.refresh(row)
    assert row.task_category is None and row.purpose == "KEEP_DETAIL"


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
    response = client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command})
    assert response.status_code == 200, response.text
    assert "task_category_notice" not in response.json()


def test_refused_command_is_not_a_category_notice(client, api):
    response = client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": "help"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "command_not_allowed"
    assert "task_category_notice" not in response.json()


def test_public_openapi_documents_optional_notice_without_changing_required_request(client):
    document = client.get("/api/public-openapi.json").json()
    schemas = document["components"]["schemas"]
    assert "task_category_notice" not in schemas["CommandResponse"].get("required", [])
    assert "task_category" not in schemas["CommandRequest"].get("required", [])
    assert schemas["TaskCategoryNotice"]["properties"]["do_not_replay_command"]["default"] is True


def test_missing_category_can_inherit_existing_valid_category_without_false_state_claim(
    client, api, monkeypatch, db_session
):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}/commands"
    assert (
        client.post(
            path, headers=api["headers"], json={"command": "lock n1 1h", "task_category": "operator_testing"}
        ).status_code
        == 200
    )
    response = client.post(path, headers=api["headers"], json={"command": "lock n1 2h"})
    assert response.status_code == 200, response.text
    assert response.json()["task_category_notice"]["reason"] == "missing"
    assert "retain its existing valid category" in response.json()["task_category_notice"]["guidance"]
    row = db_session.query(UsageDeclaration).one()
    db_session.refresh(row)
    assert row.task_category == "operator_testing"


@pytest.mark.parametrize("endpoint", ["commands", "usage-declarations/n1"])
@pytest.mark.parametrize("identity_field", ["external_user_id", "_task_category_notice_reason"])
def test_unsupported_category_does_not_relax_identity_or_extra_field_validation(client, api, endpoint, identity_field):
    before = copy.deepcopy(api["bot"].state.bot_state)
    body = {"task_category": "unknown", identity_field: "forged"}
    if endpoint == "commands":
        body["command"] = "lock n1 1h"
    response = getattr(client, "post" if endpoint == "commands" else "put")(
        f"/api/bots/{api['bot_id']}/{endpoint}", headers=api["headers"], json=body
    )
    assert response.status_code == 422
    assert api["bot"].state.bot_state == before


@pytest.mark.parametrize("category", [None, "UNKNOWN_CATEGORY_CANARY"])
def test_declaration_optout_keeps_own_current_details_unless_explicitly_cleared(client, api, monkeypatch, category):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}"
    assert (
        client.post(
            path + "/commands",
            headers=api["headers"],
            json={
                "command": "lock n1 1h",
                "purpose": "KEEP_DETAIL",
                "agent_label": "KEEP_AGENT",
                "task_category": "operator_testing",
            },
        ).status_code
        == 200
    )
    before = copy.deepcopy(api["bot"].state.bot_state)
    result = client.put(path + "/usage-declarations/n1", headers=api["headers"], json={"task_category": category})
    assert result.status_code == 200, result.text
    assert result.json()["task_category"] is None
    assert result.json()["purpose"] == "KEEP_DETAIL" and result.json()["agent_label"] == "KEEP_AGENT"
    result = client.put(
        path + "/usage-declarations/n1",
        headers=api["headers"],
        json={"task_category": None, "purpose": "", "agent_label": None},
    )
    assert result.status_code == 200, result.text
    assert result.json()["purpose"] == "" and result.json()["agent_label"] is None
    assert api["bot"].state.bot_state == before


@pytest.mark.parametrize("command", ["query", "free n1"])
def test_explicitly_ignored_category_is_reported_even_outside_acquisition(client, api, command):
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands",
        headers=api["headers"],
        json={"command": command, "task_category": "UNKNOWN_CATEGORY_CANARY"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task_category_notice"]["reason"] == "unsupported_category"
    assert "UNKNOWN_CATEGORY_CANARY" not in response.text


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
@pytest.mark.parametrize("category", ["omitted", None, "UNKNOWN_CATEGORY_CANARY"])
def test_queue_claim_category_choice_does_not_erase_own_booked_details(client, api, monkeypatch, db_session, category):
    from lockbot.backend.app.resource_api.usage import UsageDeclaration

    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}/commands"
    assert (
        client.post(
            path,
            headers=api["headers"],
            json={
                "command": f"book {_target(api)} 30m",
                "task_category": "operator_testing",
                "purpose": "KEEP_BOOKED_DETAIL",
            },
        ).status_code
        == 200
    )
    body = {"command": f"lock {_target(api)}"}
    if category != "omitted":
        body["task_category"] = category
    result = client.post(path, headers=api["headers"], json=body)
    assert result.status_code == 200, result.text
    row = db_session.query(UsageDeclaration).one()
    db_session.refresh(row)
    assert row.purpose == "KEEP_BOOKED_DETAIL"
    assert row.task_category == ("operator_testing" if category == "omitted" else None)
    state = api["bot"].state.bot_state["n1"]
    resource = state[0] if isinstance(state, list) else state
    assert [holder["user_id"] for holder in resource["current_users"]] == ["alice"]
