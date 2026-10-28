"""AC-5 / AC-5.1 / AC-4: the command endpoint.

One mutation language, allowlisted. The tests below cover what an agent may reach, what it
may never reach, that refusals happen before any bot method runs, and that the response is
platform-neutral text rather than a raw platform message dict.
"""

from unittest.mock import patch

import pytest
from fake_adapter import FakeAdapter
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.core.platforms.registry import register_adapter, unregister_adapter

PLATFORM = "infoflow"
ACTOR = "alice"


@pytest.fixture
def api(client, auth_header, db_session, monkeypatch, session_factory, tmp_path):
    """A running bot plus a confirmed credential, with the endpoint's manager pointed at it."""
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)

    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "cmdbot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1", "n2": "m2"},
        },
    )
    assert created.status_code == 201, created.text
    bot_id = created.json()["id"]
    client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})

    issued = client.post("/api/resource-auth/bindings", json={"label": "agent"}).json()
    service.handle_chat_binding(
        platform=PLATFORM,
        external_user_id=ACTOR,
        bot_id=bot_id,
        action="bind",
        argument=issued["bind_code"],
    )
    db_session.expire_all()

    manager = BotManager(max_inflight=3, drain_timeout=2.0)
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": "NODE",
            "BOT_NAME": "cmdbot",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"},
            "ALLOW_USER_ALIAS": True,
        },
    )

    with patch("lockbot.backend.app.resource_api.commands.bot_manager", manager):
        yield {
            "bot_id": bot_id,
            "credential": issued["credential"],
            "manager": manager,
            "bot": manager.get_instance(bot_id).bot,
            "headers": {"Authorization": f"Bearer {issued['credential']}"},
            "auth_header": auth_header,
        }
    manager.shutdown_all()


def _post(client, api, command):
    return client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command})


class TestAllowedOperations:
    def test_query_succeeds_and_returns_text(self, client, api):
        resp = _post(client, api, "query")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body["reply"], str)
        assert body["reply"].strip()

    def test_lock_mutates_state(self, client, api):
        resp = _post(client, api, "lock n1 1h")
        assert resp.status_code == 200, resp.text
        holders = [u["user_id"] for u in api["bot"].state.bot_state["n1"]["current_users"]]
        assert holders == [ACTOR], "the command must act as the verified IM identity"

    def test_slock_is_allowed(self, client, api):
        assert _post(client, api, "slock n1 1h").status_code == 200

    def test_unlock_is_allowed(self, client, api):
        _post(client, api, "lock n1 1h")
        assert _post(client, api, "unlock n1").status_code == 200
        assert api["bot"].state.bot_state["n1"]["current_users"] == []

    def test_free_is_allowed_as_an_alias_of_unlock(self, client, api):
        _post(client, api, "lock n1 1h")
        assert _post(client, api, "free n1").status_code == 200
        assert api["bot"].state.bot_state["n1"]["current_users"] == []

    def test_a_bare_node_key_is_treated_as_query(self, client, api):
        """The dispatcher resolves it to `query`; the allowlist must agree."""
        resp = _post(client, api, "n1")
        assert resp.status_code == 200, resp.text

    def test_response_is_platform_neutral(self, client, api):
        body = _post(client, api, "query").json()
        assert set(body) == {"bot_id", "bot_type", "platform", "actor", "command", "reply"}
        assert body["bot_type"] == "NODE"
        assert body["platform"] == "infoflow"
        assert body["actor"] == {"platform": PLATFORM, "external_user_id": ACTOR}
        assert body["command"] == "query"

    def test_no_raw_platform_message_dict_leaks(self, client, api):
        raw = _post(client, api, "lock n1 1h").text
        for field in ("fromuserid", "atuserids", '"body"', '"header"', "toid"):
            assert field not in raw


class TestRefusedOperations:
    @pytest.mark.parametrize("command", ["kickout alice", "take n1", "kicklock n1"])
    def test_preemptive_and_administrative_commands_are_refused(self, client, api, command):
        resp = _post(client, api, command)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "command_not_allowed"

    @pytest.mark.parametrize("command", ["bind ABCD2345", "unbind all", "bindings"])
    def test_enrollment_commands_are_refused(self, client, api, command):
        resp = _post(client, api, command)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "command_not_allowed"

    def test_a_refused_command_never_reaches_the_bot(self, client, api):
        with patch.object(type(api["bot"]), "kickout", autospec=True) as spy:
            _post(client, api, "kickout alice")
        spy.assert_not_called()

    def test_a_refused_command_consumes_no_admission_slot(self, client, api):
        _post(client, api, "kickout alice")
        assert api["manager"].inflight(api["bot_id"]) == 0

    @pytest.mark.parametrize("command", ["KICKOUT alice", "KickOut alice", "TAKE n1"])
    def test_casing_does_not_bypass_the_allowlist(self, client, api, command):
        assert _post(client, api, command).status_code == 403

    def test_an_unknown_command_is_refused(self, client, api):
        resp = _post(client, api, "selfdestruct now")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "command_not_allowed"

    def test_book_on_a_node_bot_is_unsupported_not_a_help_dump(self, client, api):
        """AC-5: a different mistake gets a different code."""
        resp = _post(client, api, "book n1")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "command_not_supported"
        assert "NODE" in resp.json()["detail"]["message"]

    @pytest.mark.parametrize("field", ["actor", "external_user_id", "platform", "user_id"])
    def test_identity_fields_in_the_body_are_rejected(self, client, api, field):
        resp = client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers=api["headers"],
            json={"command": "query", field: "attacker"},
        )
        assert resp.status_code == 422

    def test_an_empty_command_is_rejected_by_validation(self, client, api):
        resp = client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": ""})
        assert resp.status_code == 422


class TestAlias:
    def test_alias_can_be_set_and_cleared(self, client, api):
        assert _post(client, api, "alias n1 10.0.0.5").status_code == 200
        assert _post(client, api, "alias n1").status_code == 200

    def test_aliases_disabled_is_distinguishable_from_not_allowed(self, client, api):
        """AC-5.1: the agent is permitted; the bot is not configured."""
        api["bot"].config.set_val("ALLOW_USER_ALIAS", False)
        resp = _post(client, api, "alias n1 10.0.0.5")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "aliases_disabled"
        assert resp.json()["detail"]["code"] != "command_not_allowed"

    def test_aliases_disabled_is_not_a_help_dump(self, client, api):
        api["bot"].config.set_val("ALLOW_USER_ALIAS", False)
        message = _post(client, api, "alias n1 x").json()["detail"]["message"]
        assert "ALLOW_USER_ALIAS" in message
        assert "lock" not in message.lower(), "a help dump would list every command"


class TestPrincipalSeparationOnThisRoute:
    """The half of task11 that needed this route to exist."""

    def test_a_management_jwt_is_rejected(self, client, api):
        resp = client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers=api["auth_header"],
            json={"command": "query"},
        )
        assert resp.status_code == 401

    def test_a_legacy_service_key_is_rejected(self, client, api, auth_header):
        key = client.post(f"/api/bots/{api['bot_id']}/api-key", headers=auth_header).json()["api_key"]
        resp = client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers={"Authorization": f"Bearer {key}"},
            json={"command": "query"},
        )
        assert resp.status_code == 401

    def test_an_unauthenticated_call_is_rejected(self, client, api):
        assert client.post(f"/api/bots/{api['bot_id']}/commands", json={"command": "query"}).status_code == 401

    def test_a_credential_for_another_bot_is_rejected(self, client, api, auth_header):
        other = client.post(
            "/api/bots",
            headers=auth_header,
            json={
                "name": "otherbot",
                "bot_type": "NODE",
                "webhook_url": "https://example.invalid/hook2",
                "cluster_configs": {"x1": "m1"},
            },
        ).json()["id"]
        resp = client.post(f"/api/bots/{other}/commands", headers=api["headers"], json={"command": "query"})
        assert resp.status_code == 401

    def test_a_revoked_credential_stops_working_immediately(self, client, api, auth_header):
        assert _post(client, api, "query").status_code == 200
        client.delete(f"/api/bots/{api['bot_id']}/resource-auth/bindings", headers=auth_header)
        assert _post(client, api, "query").status_code == 401


class TestLifecycleErrors:
    def test_a_stopped_bot_reports_bot_not_running(self, client, api):
        api["manager"].stop_bot(api["bot_id"])
        resp = _post(client, api, "query")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "bot_not_running"
        assert resp.headers.get("Retry-After")

    def test_a_retiring_instance_refuses_new_work(self, client, api):
        api["manager"]._begin_retiring(api["bot_id"])
        resp = _post(client, api, "query")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "instance_retiring"

    def test_a_full_admission_queue_sheds_with_retry_after(self, client, api, monkeypatch):
        from lockbot.backend.app.bots.manager import AdmissionFull

        def refuse(_bot_id):
            raise AdmissionFull("full")

        monkeypatch.setattr(api["manager"], "admit", refuse)
        resp = _post(client, api, "query")
        assert resp.status_code == 429
        assert resp.json()["detail"]["code"] == "admission_full"
        assert resp.headers["Retry-After"]

    def test_replacement_under_an_admitted_command_reports_may_have_executed(self, client, api, monkeypatch):
        monkeypatch.setattr(api["manager"], "lease_is_current", lambda _lease: False)
        resp = _post(client, api, "lock n1 1h")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "may_have_executed"
        detail = resp.json()["detail"]["message"]
        assert "/resources" in detail
        assert "extends duration" in detail


class TestPlatformNeutrality:
    def test_a_non_infoflow_bot_is_served_without_infoflow_payloads(self, client, api, db_session):
        """AC-8: the command path must work for a registered non-Infoflow adapter."""
        from lockbot.backend.app.bots.models import Bot

        register_adapter(FakeAdapter)
        try:
            bot_row = db_session.get(Bot, api["bot_id"])
            bot_row.platform = FakeAdapter.platform_name
            db_session.commit()
            api["bot"].adapter = FakeAdapter()

            resp = _post(client, api, "lock n1 1h")
            assert resp.status_code == 200, resp.text
            assert resp.json()["platform"] == FakeAdapter.platform_name
            assert isinstance(resp.json()["reply"], str)
        finally:
            unregister_adapter(FakeAdapter.platform_name)


def test_credential_last_used_is_recorded(client, api, db_session):
    """AC-11: enough detail to identify which credential is driving load."""
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    _post(client, api, "query")
    db_session.expire_all()
    row = db_session.query(ResourceBinding).one()
    assert row.last_used_at is not None
    assert credentials.short_id(row.public_id)


class TestAgentAccessSwitch:
    def test_disable_suspends_existing_key_and_all_agent_routes_then_reenable_restores_it(
        self, client, api, auth_header, db_session
    ):
        assert _post(client, api, "lock n1 10m").status_code == 200
        off = client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=auth_header, json={"enabled": False})
        assert off.status_code == 200
        assert off.json()["resource_auth_enabled"] is False
        for method, suffix, payload in [
            ("GET", "resources", None),
            ("GET", "usage-declarations", None),
            ("POST", "commands", {"command": "unlock n1"}),
        ]:
            response = client.request(
                method, f"/api/bots/{api['bot_id']}/{suffix}", headers=api["headers"], json=payload
            )
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "agent_access_disabled"
        assert api["bot"].state.bot_state["n1"]["current_users"][0]["user_id"] == ACTOR
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=api["bot_id"], bot_uuid=None, token=api["credential"])
        assert (
            client.put(
                f"/api/bots/{api['bot_id']}/resource-auth", headers=auth_header, json={"enabled": True}
            ).status_code
            == 200
        )
        assert _post(client, api, "unlock n1").status_code == 200

    def test_access_is_rechecked_after_initial_authentication(self, client, api, auth_header, db_session):
        from fastapi import HTTPException
        from lockbot.backend.app.resource_auth.dependencies import require_resource_actor, reverify_actor
        from starlette.requests import Request

        request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + api["credential"]).encode())]})
        actor = require_resource_actor(api["bot_id"], request, db_session)
        client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=auth_header, json={"enabled": False})
        with pytest.raises(HTTPException) as error:
            reverify_actor(db_session, request, actor)
        assert error.value.status_code == 403
        assert error.value.detail["code"] == "agent_access_disabled"

    def test_chat_revocation_still_works_while_access_is_disabled(self, client, api, auth_header, db_session):
        client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=auth_header, json={"enabled": False})
        reply = service.handle_chat_binding(
            platform=PLATFORM, external_user_id=ACTOR, bot_id=api["bot_id"], action="unbind", argument=""
        )
        assert "已撤销" in reply
        client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=auth_header, json={"enabled": True})
        assert _post(client, api, "query").status_code == 401
