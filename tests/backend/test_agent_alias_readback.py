"""Public node aliases are observed through the actual authenticated resource API."""

# ruff: noqa: F811
import copy
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_api.usage import UsageDeclaration

from backend.test_resource_api_resources import _get, api  # noqa: F401

pytestmark = pytest.mark.parametrize("api", ["NODE", "QUEUE", "DEVICE", "QUEUE_DEVICE"], indirect=True)


def command(client, api, text, **metadata):
    return client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": text, **metadata}
    )


def node(client, api, headers=None):
    response = _get(client, api, headers)
    assert response.status_code == 200, response.text
    return next(resource for resource in response.json()["resources"] if resource["id"] == "n1")


def test_configured_and_unset_aliases_are_public_parent_node_fields(client, api):
    initial = _get(client, api).json()
    assert all(resource["alias"] == "" for resource in initial["resources"])
    api["bot"].config.set_val("NODE_ALIASES", {"n1": "rack-a", "unconfigured-node": "not-in-snapshot"})
    response = _get(client, api)
    current = node(client, api)
    assert current["alias"] == "rack-a"
    assert all("alias" not in device for device in current.get("devices", []))
    assert "not-in-snapshot" not in response.text
    assert node(client, api, api["other_headers"])["alias"] == "rack-a"
    assert _get(client, api, api["auth_header"]).status_code == 401
    assert client.get(f"/api/bots/{api['bot_id']}/resources").status_code == 401


def test_agent_sets_changes_and_clears_alias_without_changing_holdings_or_task_notes(client, api, db_session):
    target = "n1 dev0" if "DEVICE" in api["bot"].config.get_val("BOT_TYPE") else "n1"
    locked = command(client, api, f"lock {target} 1h", purpose="Private task", agent_label="Worker")
    assert locked.status_code == 200, locked.text
    before = copy.deepcopy(api["bot"].state.bot_state)
    note = db_session.query(UsageDeclaration).one()
    before_note = (note.purpose, note.agent_label, note.holding_proof)
    for value in ("rack-a", "rack-b", ""):
        response = command(client, api, f"alias n1 {value}".rstrip())
        assert response.status_code == 200, response.text
        assert response.json()["reply"]
        assert node(client, api)["alias"] == value
        assert node(client, api, api["other_headers"])["alias"] == value
        db_session.expire_all()
        configured = json.loads(db_session.get(Bot, api["bot_id"]).config_overrides)["NODE_ALIASES"]
        assert configured.get("n1", "") == value
        assert api["bot"].state.bot_state == before
        note = db_session.query(UsageDeclaration).one()
        assert (note.purpose, note.agent_label, note.holding_proof) == before_note


def test_current_alias_is_observed_even_when_lock_state_is_cached(client, api):
    api["bot"].config.set_val("NODE_ALIASES", {"n1": "before"})
    first = _get(client, api).json()
    api["bot"].config.set_val("NODE_ALIASES", {"n1": "after"})
    with patch.object(type(api["bot"]), "state_snapshot", side_effect=AssertionError("lock state must stay cached")):
        second = _get(client, api).json()
    assert second["captured_at"] == first["captured_at"]
    assert second["resources"][0]["alias"] == "after"
    assert first["resources"][0]["alias"] == "before"
    assert datetime.fromisoformat(second["aliases_observed_at"]) >= datetime.fromisoformat(first["aliases_observed_at"])
    assert datetime.fromisoformat(second["aliases_observed_at"]) >= datetime.fromisoformat(second["captured_at"])
    assert "does not establish an atomic observation" in second["notes"]["alias"]


def test_disabled_editing_preserves_current_alias_and_im_display_setting(client, api):
    api["bot"].config.set_val("NODE_ALIASES", {"n1": "keep-label"})
    api["bot"].config.set_val("ALLOW_USER_ALIAS", False)
    api["bot"].config.set_val("SHOW_NODE_ALIAS", False)
    for text in ("alias n1 replacement", "alias n1"):
        response = command(client, api, text)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "aliases_disabled"
        assert node(client, api)["alias"] == "keep-label"
    assert api["bot"]._display_node("n1") == "n1"


def test_dispatched_but_rejected_alias_does_not_claim_a_changed_value(client, api):
    api["bot"].config.set_val("NODE_ALIASES", {"n1": "unchanged"})
    for text in ("alias n1 " + "x" * 16, "alias unknown label"):
        response = command(client, api, text)
        assert response.status_code == 200, response.text
        assert node(client, api)["alias"] == "unchanged"


def test_readback_observes_memory_without_claiming_failed_persistence(client, api, db_session):
    before = db_session.get(Bot, api["bot_id"]).config_overrides
    with patch.object(type(api["bot"]), "_persist_config_override", return_value=False):
        response = command(client, api, "alias n1 volatile")
    assert response.status_code == 200, response.text
    assert node(client, api)["alias"] == "volatile"
    db_session.expire_all()
    assert db_session.get(Bot, api["bot_id"]).config_overrides == before
    assert "Neither HTTP 200" in _get(client, api).json()["notes"]["alias"]
