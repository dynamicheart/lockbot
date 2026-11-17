"""Optional metadata never converts a reporter/browser session into key authority."""

# ruff: noqa: F811

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.personal_panel.native import NativeProvider
from lockbot.backend.app.personal_panel.router import COOKIE, create_panel
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_personal_panel_http import Directory, issue, post
from backend.test_resource_api_resources import api  # noqa: F401

ORIGIN = "https://bot.example.test"
SOURCE = "https://resource.example.test"
OTHER = "https://unused.example.test"


class Adapter:
    def __init__(self):
        self.calls = []
        self.raw = {
            "service_id": "lockbot.resource-api",
            "api_version": "1",
            "service_origin": SOURCE,
            "instance_id": "native-instance",
            "bot": {"id": 7, "uuid": "22222222-2222-4222-8222-222222222222", "name": "Synthetic group", "type": "NODE"},
            "actor": {"platform": "feishu", "external_user_id": "same"},
            "identity_revision": 1,
            "expires_at": None,
            "credential_recovery_health": "recoverable",
        }

    def describe_connection(self, grant):
        self.calls.append(deepcopy(grant))
        return deepcopy(self.raw) if grant["ref"] == "a" else None


@pytest.fixture
def env(tmp_path):
    provider, adapter = Directory(), Adapter()
    app = FastAPI()
    app.mount(
        "/me",
        create_panel(
            ORIGIN,
            tmp_path / "sessions.db",
            provider,
            metadata_adapter=adapter,
            metadata_pins={SOURCE: "native-instance", OTHER: "unused-instance"},
        ),
    )
    agent, browser = TestClient(app, base_url=ORIGIN), TestClient(app, base_url=ORIGIN)
    return provider, adapter, agent, browser


def login(agent, browser, key="a-secret", mode="replace"):
    assert post(browser, "login", {"ticket": issue(agent, key), "mode": mode}).status_code == 200


def test_anonymous_caps_do_not_disclose_pins_or_upstream_and_cookie_reads_only_proven_metadata(env):
    _, adapter, agent, browser = env
    caps = browser.get("/me/capabilities")
    assert SOURCE not in caps.text and OTHER not in caps.text
    assert caps.json()["connection_management"] == {
        "version": 2,
        "metadata_available": True,
        "credential_disclosure_supported": False,
        "refresh_semantics": "revalidate_only",
    }
    assert browser.get("/me/connections").status_code == 401
    login(agent, browser)
    response = browser.get("/me/connections")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 2
    assert [pin["origin"] for pin in body["service_pins"]] == [SOURCE]
    row = body["connections"][0]
    assert row["skill"]["bundle_url"] == SOURCE + "/api/agent-skill/lockbot-resource-agent.zip"
    assert SOURCE in row["instructions"] and "unused-instance" not in response.text
    assert all(secret not in response.text for secret in ("a-secret", '"ref"', '"credential"', '"digest"'))
    assert adapter.calls[0]["ref"] == "a"  # Trusted local adapter input, never HTTP output.
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", ["origin", "instance", "actor", "protocol", "key", "url", "label"])
def test_adapter_cannot_forge_origin_identity_or_native_capability(env, change):
    _, adapter, agent, browser = env
    login(agent, browser)
    if change == "origin":
        adapter.raw["service_origin"] = OTHER
    elif change == "instance":
        adapter.raw["instance_id"] = "wrong"
    elif change == "actor":
        adapter.raw["actor"]["external_user_id"] = "other"
    elif change == "protocol":
        adapter.raw["service_id"] = "private.report-only"
    elif change == "key":
        adapter.raw["credential"] = "secret"
    elif change == "url":
        adapter.raw["instructions"] = "forged"
    else:
        adapter.raw["bot"]["name"] = "lbr_SYNTHETIC_KEY"
    response = browser.get("/me/connections")
    assert response.status_code == 503
    assert "connections" not in response.json() and "secret" not in response.text


def test_none_and_partial_authorizations_do_not_imply_other_scope_access(env):
    _, _, agent, browser = env
    login(agent, browser, "b-secret")
    assert browser.get("/me/connections").status_code == 403
    login(agent, browser, "a-secret", "append")
    response = browser.get("/me/connections").json()
    assert response["some_authorizations_unavailable"] is True
    assert len(response["connections"]) == 1


@pytest.mark.parametrize("change", ["revoke", "logout", "migrate"])
def test_final_authority_check_wins_over_inflight_metadata(env, change):
    provider, adapter, agent, browser = env
    login(agent, browser)
    original = adapter.describe_connection

    def inspect(grant):
        raw = original(grant)
        if change == "logout":
            post(browser, "logout", {})
        elif change == "revoke":
            provider.bindings.pop("a-secret")
        else:
            provider.bindings["a-secret"] = {**provider.bindings["a-secret"], "scope": "changed"}
        return raw

    adapter.describe_connection = inspect
    assert browser.get("/me/connections").status_code in {401, 403}


def test_revocation_of_earlier_scope_while_later_adapter_returns_none_denies_batch(env):
    provider, adapter, agent, browser = env
    login(agent, browser)
    login(agent, browser, "b-secret", "append")
    original = adapter.describe_connection

    def inspect(grant):
        if grant["ref"] == "b":
            provider.bindings.pop("a-secret")
        return original(grant)

    adapter.describe_connection = inspect
    assert browser.get("/me/connections").status_code == 403


def test_expired_metadata_and_missing_native_protocol_are_rejected(env):
    _, adapter, agent, browser = env
    login(agent, browser)
    adapter.raw["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert browser.get("/me/connections").status_code == 503
    adapter.raw["expires_at"] = None
    adapter.raw.pop("service_id")
    assert browser.get("/me/connections").status_code == 503


def test_pins_are_explicit_bounded_and_no_credential_urls():
    from lockbot.backend.app.personal_panel.metadata import pinned_services

    for origins, pins in (
        ([], {}),
        ([SOURCE], {}),
        ([SOURCE], {SOURCE: "pin", OTHER: "other"}),
        (["https://user:pass@example.test"], {"https://user:pass@example.test": "pin"}),
    ):
        with pytest.raises(ValueError):
            pinned_services(origins, pins)


def test_upstream_recovery_health_can_be_unknown_without_guessing(env):
    _, adapter, agent, browser = env
    login(agent, browser)
    adapter.raw["credential_recovery_health"] = "unknown"
    adapter.raw["identity_revision"] = None
    response = browser.get("/me/connections")
    assert response.status_code == 200
    assert response.json()["connections"][0]["credential_recovery_health"] == "unknown"
    assert response.json()["connections"][0]["identity_revision"] is None


def test_native_login_preserves_existing_external_browser_authority(api, db_session, session_factory, tmp_path):
    ensure_instance_id(db_session)
    external, adapter = Directory(), Adapter()
    native = NativeProvider({"issuer": ORIGIN, "session_factory": session_factory, "local_key_handoff": True})
    state_file = tmp_path / "sessions.db"
    reporter_calls = []

    class Reporter:
        def report(self, grant):
            reporter_calls.append(grant.copy())
            return [{"label": "Private report", "status": "ok", "allocations": []}]

    def mount(**kwargs):
        app = FastAPI()
        app.mount(
            "/me",
            create_panel(
                ORIGIN,
                state_file,
                external,
                reporters=(Reporter(),),
                metadata_adapter=adapter,
                metadata_pins={SOURCE: "native-instance"},
                **kwargs,
            ),
        )
        return app

    before = mount()
    previous_agent, previous_browser = TestClient(before, base_url=ORIGIN), TestClient(before, base_url=ORIGIN)
    login(previous_agent, previous_browser)
    previous_groups = previous_browser.get("/me/resources").json()["groups"]
    old_cookie = previous_browser.cookies.get(COOKIE)
    assert [group["label"] for group in previous_groups] == ["GPU A", "Private report"]

    upgraded = mount(native_handoff=native)
    agent, browser = TestClient(upgraded, base_url=ORIGIN), TestClient(upgraded, base_url=ORIGIN)
    browser.cookies.set(COOKIE, old_cookie, domain="bot.example.test", path="/")
    assert browser.get("/me/resources").json()["groups"] == previous_groups
    management = browser.get("/me/capabilities").json()["connection_management"]
    assert management["version"] == 2
    assert management["credential_disclosure_supported"] is False
    assert management["native_key_handoff"] == {"version": 1, "account_issue_path": "/me/account-links"}
    local_key = api["headers"]["Authorization"][7:]
    grant = native.authenticate(local_key)
    external.bindings["spoof"] = grant
    assert agent.post("/me/links", headers={"Authorization": "Bearer spoof"}).status_code == 401
    assert agent.post("/me/account-links", headers={"Authorization": "Bearer a-secret"}).status_code == 401
    assert agent.post("/me/credential-view-links", headers=api["headers"]).status_code == 404
    result = agent.post("/me/account-links", headers=api["headers"])
    assert result.status_code == 200 and local_key not in result.text
    ticket = result.json()["login_url"].split("?ticket=", 1)[1]
    assert post(browser, "preview", {"ticket": ticket}).json()["identity"]["issuer"] == ORIGIN
    assert post(browser, "login", {"ticket": ticket, "mode": "append_account"}).status_code == 200
    assert len(browser.get("/me/session").json()["identities"]) == 2

    connections = browser.get("/me/connections").json()
    assert connections["schema_version"] == 2
    assert {row["service_origin"] for row in connections["connections"]} == {SOURCE, ORIGIN}
    assert {pin["origin"] for pin in connections["service_pins"]} == {SOURCE, ORIGIN}
    groups = browser.get("/me/resources").json()["groups"]
    assert groups[:2] == previous_groups
    assert [group["identity"]["issuer"] for group in groups[2:]] == [ORIGIN]
    batches = [json.loads(line) for line in browser.get("/me/resources-stream").iter_lines()]
    assert batches[-1]["event"] == "complete"
    assert {g["detail_id"] for batch in batches if batch["event"] == "batch" for g in batch["groups"]} == {
        g["detail_id"] for g in groups
    }
    assert adapter.calls and reporter_calls
    assert all(grant["ref"] == "a" for grant in adapter.calls + reporter_calls)

    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.revoked_at = datetime.now(timezone.utc)
    db_session.commit()
    assert browser.get("/me/resources").json()["groups"] == previous_groups
