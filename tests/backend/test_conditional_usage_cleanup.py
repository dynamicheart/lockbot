"""Metadata cleanup must not delete a declaration replaced after client inspection."""

# ruff: noqa: F811
import copy

import pytest
from lockbot.backend.app.resource_api.usage import UsageDeclaration, declaration_revision

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture
def declaration(client, api, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])
    path = f"/api/bots/{api['bot_id']}"
    device = api["bot"].config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"}
    target = "n1 dev0" if device else "n1"
    resource = "n1:dev0" if api["bot"].config.get_val("BOT_TYPE") == "QUEUE_DEVICE" else "n1"
    command = client.post(path + "/commands", headers=api["headers"], json={"command": f"lock {target} 10m"})
    assert command.status_code == 200
    response = client.put(
        path + "/usage-declarations/" + resource, headers=api["headers"], json={"purpose": "fixture task"}
    )
    assert response.status_code == 200
    return path, response.json()


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_matching_revision_cleans_only_metadata_without_resource_mutation(client, api, declaration):
    path, item = declaration
    metadata = client.get("/api/agent/context", headers=api["headers"]).json()["usage_privacy"]
    assert metadata["conditional_cleanup"] == {
        "header": "If-Match",
        "revision_field": "revision",
        "mismatch_status": 412,
        "lease_required": True,
    }
    assert "revision" in metadata["private_fields"]
    before = copy.deepcopy(api["bot"].state.bot_state)
    response = client.delete(
        path + "/usage-declarations/" + item["resource_id"], headers={**api["headers"], "If-Match": item["revision"]}
    )
    assert response.status_code == 200 and response.json() == {"deleted": True}
    assert client.get(path + "/usage-declarations", headers=api["headers"]).json()["items"] == []
    assert api["bot"].state.bot_state == before
    # Replaying the conditional cleanup cannot become a new delete.
    assert (
        client.delete(
            path + "/usage-declarations/" + item["resource_id"],
            headers={**api["headers"], "If-Match": item["revision"]},
        ).status_code
        == 412
    )


def test_replaced_declaration_survives_old_revision(client, api, declaration):
    path, old = declaration
    updated = client.put(path + "/usage-declarations/n1", headers=api["headers"], json={"purpose": "replacement task"})
    assert updated.status_code == 200
    assert updated.json()["revision"] != old["revision"]
    response = client.delete(path + "/usage-declarations/n1", headers={**api["headers"], "If-Match": old["revision"]})
    assert response.status_code == 412
    own = client.get(path + "/usage-declarations", headers=api["headers"]).json()["items"]
    assert own[0]["purpose"] == "replacement task"


def test_reacquired_lease_cannot_be_cleaned_with_old_declaration_revision(client, api, declaration):
    path, old = declaration
    api["bot"].unlock("alice", "unlock n1")
    api["bot"].lock("alice", "lock n1 10m")
    before = copy.deepcopy(api["bot"].state.bot_state)
    response = client.delete(path + "/usage-declarations/n1", headers={**api["headers"], "If-Match": old["revision"]})
    assert response.status_code == 412
    assert api["bot"].state.bot_state == before


@pytest.mark.parametrize("value", ["*", 'W/"old"', '"not-a-version"', ""])
def test_unknown_or_wildcard_revision_is_not_an_unconditional_delete(client, api, declaration, value):
    path, _ = declaration
    response = client.delete(path + "/usage-declarations/n1", headers={**api["headers"], "If-Match": value})
    assert response.status_code == 412
    assert len(client.get(path + "/usage-declarations", headers=api["headers"]).json()["items"]) == 1


def test_revision_is_private_and_cannot_authorize_another_actor(client, api, declaration):
    path, item = declaration
    for suffix in ["/resources", "/usage-declarations"]:
        public = client.get(path + suffix, headers=api["other_headers"])
        assert '"revision"' not in public.text
        assert item["revision"] not in public.text
    response = client.delete(
        path + "/usage-declarations/n1", headers={**api["other_headers"], "If-Match": item["revision"]}
    )
    assert response.status_code == 412
    assert len(client.get(path + "/usage-declarations", headers=api["headers"]).json()["items"]) == 1
    assert client.delete(path + "/usage-declarations/n1", headers={"If-Match": item["revision"]}).status_code == 401


def test_legacy_delete_without_precondition_keeps_its_existing_contract(client, api, declaration):
    path, _ = declaration
    for _ in range(2):
        response = client.delete(path + "/usage-declarations/n1", headers=api["headers"])
        assert response.status_code == 200 and response.json() == {"deleted": True}


def test_equal_update_times_still_detect_replaced_private_text(db_session, declaration):
    row = db_session.query(UsageDeclaration).one()
    original = declaration_revision(row)
    row.purpose = "another task with identical timestamp"
    assert declaration_revision(row) != original


def test_expired_holding_does_not_allow_cleanup_of_a_still_unexpired_declaration(client, api, declaration):
    path, item = declaration
    holder = api["bot"].state.bot_state["n1"]["current_users"][0]
    holder["start_time"] -= 1000
    holder["duration"] = 1
    response = client.delete(path + "/usage-declarations/n1", headers={**api["headers"], "If-Match": item["revision"]})
    assert response.status_code == 412


def test_cleanup_rechecks_the_row_in_sql_after_a_concurrent_replacement(client, api, declaration, session_factory):
    from sqlalchemy import event, update

    path, item = declaration
    with session_factory() as session:
        row_id = session.query(UsageDeclaration.id).scalar()
        engine = session.get_bind()
    replaced = []

    def replace_before_delete(connection, cursor, statement, parameters, context, executemany):
        if "DELETE FROM resource_usage_declarations" not in statement or replaced:
            return
        replaced.append(True)
        # Another writer commits after the cleanup read and before its DELETE.
        with session_factory() as other:
            other.execute(update(UsageDeclaration).where(UsageDeclaration.id == row_id).values(purpose="replacement"))
            other.commit()

    event.listen(engine, "before_cursor_execute", replace_before_delete)
    try:
        response = client.delete(
            path + "/usage-declarations/n1", headers={**api["headers"], "If-Match": item["revision"]}
        )
    finally:
        event.remove(engine, "before_cursor_execute", replace_before_delete)
    assert replaced
    assert response.status_code == 412, response.text
    own = client.get(path + "/usage-declarations", headers=api["headers"]).json()["items"]
    assert own[0]["purpose"] == "replacement"
