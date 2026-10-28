"""Discovery reads current persisted policy even without a running resource engine."""

# ruff: noqa: F811
import json

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_api import commands
from lockbot.backend.app.resource_api.snapshot import QUEUE_CLAIM_WINDOW_SECONDS
from lockbot.backend.app.resource_api.traffic import credential_budget, resource_tier_gate
from lockbot.backend.app.resource_auth import service
from lockbot.core.config import Config

from backend.test_resource_api_resources import api  # noqa: F401


def context(client, api, headers=None):
    response = client.get("/api/agent/context", headers=headers or api["headers"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "Authorization" in response.headers["vary"]
    return response.json()


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_rules_use_core_defaults_and_type_operations(client, api):
    result = context(client, api)
    rules = result["bot_rules"]
    defaults = Config()
    kind = result["bot"]["type"]
    assert result["server"]["api_version"] == "1"
    assert rules["forbid_relock"] is False
    assert rules["max_resources_per_user"] == -1
    assert rules["default_duration_seconds"] == defaults.get_val("DEFAULT_DURATION")
    assert rules["max_lock_duration_seconds"] == defaults.get_val("MAX_LOCK_DURATION")
    assert rules["effective_max_lock_duration_seconds"] == -1
    assert rules["duration_limit_exempt"] is False
    assert rules["resource_unit"] == ("card" if "DEVICE" in kind else "node")
    assert rules["queue_claim_window_seconds"] == (QUEUE_CLAIM_WINDOW_SECONDS if "QUEUE" in kind else None)
    assert set(rules["supported_operations"]) == (
        set(api["bot"].supported_commands()) & commands.AGENT_ALLOWED_COMMANDS
    )
    assert not {"take", "kickout", "kicklock"} & set(rules["supported_operations"])
    assert rules["allow_user_alias"] is False
    assert result["links"]["resources"] == f"/api/bots/{api['bot_id']}/resources"


def test_next_context_refreshes_retained_orm_identity_from_db(client, api, db_session, session_factory, monkeypatch):
    # Hold a strong ORM reference and reuse the request Session to avoid relying on GC.
    with session_factory() as reused:
        retained = reused.get(Bot, api["bot_id"])

        def same_session():
            yield reused

        monkeypatch.setitem(client.app.dependency_overrides, get_db, same_session)
        assert context(client, api)["bot_rules"]["forbid_relock"] is False
        db_bot = db_session.get(Bot, api["bot_id"])
        db_bot.config_overrides = json.dumps(
            {
                "FORBID_RELOCK": True,
                "MAX_RESOURCES_PER_USER": 2,
                "DEFAULT_DURATION": 600,
                "MAX_LOCK_DURATION": 1800,
                "ALLOW_USER_ALIAS": True,
            }
        )
        db_session.commit()
        rules = context(client, api)["bot_rules"]
        assert retained is reused.get(Bot, api["bot_id"])
        assert rules["forbid_relock"] is True and rules["max_resources_per_user"] == 2
        assert rules["default_duration_seconds"] == 600 and rules["max_lock_duration_seconds"] == 1800
        assert rules["allow_user_alias"] is True
        # The fixture's running instance was deliberately not restarted; context must not use it.
        assert api["bot"].config.get_val("FORBID_RELOCK") is False
        assert api["bot"].config.get_val("MAX_RESOURCES_PER_USER") == -1


def test_stopped_bot_returns_persisted_rules_without_admission(client, api, db_session, monkeypatch):
    api["manager"].stop_bot(api["bot_id"])

    def refuse(*args, **kwargs):
        raise AssertionError("Context must not admit a runtime")

    monkeypatch.setattr(api["manager"], "admit", refuse)
    bot = db_session.get(Bot, api["bot_id"])
    bot.status = "stopped"
    bot.config_overrides = json.dumps({"FORBID_RELOCK": True, "MAX_RESOURCES_PER_USER": 3})
    db_session.commit()
    result = context(client, api)
    assert result["bot_rules"]["forbid_relock"] is True
    assert result["bot_rules"]["max_resources_per_user"] == 3


def test_whitelist_exemption_is_caller_specific_without_disclosing_list(client, api, db_session):
    bot = db_session.get(Bot, api["bot_id"])
    bot.config_overrides = json.dumps(
        {"MAX_LOCK_DURATION": 1800, "DURATION_WHITELIST": ["alice", "private-hidden-user"]}
    )
    db_session.commit()
    alice = context(client, api)
    bob = context(client, api, api["other_headers"])
    assert alice["bot_rules"]["duration_limit_exempt"] is True
    assert alice["bot_rules"]["effective_max_lock_duration_seconds"] == -1
    assert alice["bot_rules"]["max_lock_duration_seconds"] == 1800
    assert bob["bot_rules"]["duration_limit_exempt"] is False
    assert bob["bot_rules"]["effective_max_lock_duration_seconds"] == 1800
    assert "private-hidden-user" not in json.dumps([alice, bob])
    assert "DURATION_WHITELIST" not in json.dumps([alice, bob])


def test_rules_report_actual_deployment_budgets(client, api, monkeypatch):
    monkeypatch.setattr(credential_budget, "read_limit", 43)
    monkeypatch.setattr(credential_budget, "write_limit", 4)
    monkeypatch.setattr(resource_tier_gate, "active", 13)
    monkeypatch.setattr(resource_tier_gate, "booking", 27)
    monkeypatch.setattr(resource_tier_gate, "discovery", 59)
    monkeypatch.setattr(commands, "RESOURCE_API_MAX_HELD_PER_IDENTITY", 5)
    rules = context(client, api)["bot_rules"]
    assert rules["request_budget"] == {"read_per_minute": 43, "write_per_minute": 4}
    assert rules["poll_minimum_seconds"] == {"active": 13, "booking": 27, "discovery": 59}
    assert rules["api_max_machines_per_user"] == 5


def test_keys_resolve_only_their_own_bot_policy(client, api, db_session):
    other = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "separate-rule-bot",
            "bot_type": "QUEUE_DEVICE",
            "webhook_url": "",
            "cluster_configs": {"n1": ["gpu"]},
            "config_overrides": {"MAX_RESOURCES_PER_USER": 7, "FORBID_RELOCK": True},
        },
    ).json()
    issued = client.post("/api/resource-auth/bindings", json={"label": "separate scope"}).json()
    service.handle_chat_binding(
        platform="infoflow", external_user_id="alice", bot_id=other["id"], action="bind", argument=issued["bind_code"]
    )
    first = context(client, api)
    second = context(client, api, {"Authorization": "Bearer " + issued["credential"]})
    assert first["bot"]["id"] == api["bot_id"] and second["bot"]["id"] == other["id"]
    assert first["bot_rules"]["max_resources_per_user"] == -1
    assert second["bot_rules"]["max_resources_per_user"] == 7
    assert second["bot_rules"]["resource_unit"] == "card"
    assert second["bot_rules"]["forbid_relock"] is True
    assert context(client, api)["bot_rules"]["max_resources_per_user"] == -1


def test_public_schema_documents_rules_as_additive_context_field(client):
    schema = client.get("/api/public-openapi.json").json()
    definitions = schema["components"]["schemas"]
    rule_schema = definitions["AgentContextResponse"]["properties"]["bot_rules"]
    assert any(choice.get("$ref", "").endswith("/BotRules") for choice in rule_schema["anyOf"])
    assert "bot_rules" not in definitions["AgentContextResponse"].get("required", [])
    for field in [
        "forbid_relock",
        "max_resources_per_user",
        "resource_unit",
        "default_duration_seconds",
        "effective_max_lock_duration_seconds",
        "poll_minimum_seconds",
        "request_budget",
        "api_max_machines_per_user",
    ]:
        assert field in definitions["BotRules"]["properties"]
