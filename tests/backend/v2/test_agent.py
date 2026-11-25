from io import BytesIO
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_auth import service

API = "/api/agent/v2"


def test_openapi_versions_and_query_protection(client):
    from lockbot.backend.app import config

    v1 = client.get("/api/public-openapi.json?scope=agent").raise_for_status().json()
    assert "/api/agent/context" in v1["paths"]
    assert v1["paths"]["/api/agent/context"]["get"]["x-lockbot-rate-limit"]["route_per_ip"] == "60/minute"
    assert client.get("/api/agent/context").headers["x-lockbot-api-version"] == "1"
    assert client.get(API + "/resources").headers["x-lockbot-api-version"] == "2"

    v2 = client.get(API + "/openapi.json").raise_for_status().json()
    assert v2["info"]["title"] == "LockBot Agent API v2"
    paths = v2["paths"]
    assert all(path.startswith(API + "/") for path in paths)
    binding_rate = paths[API + "/bindings"]["post"]["x-lockbot-rate-limit"]
    assert binding_rate["route_per_ip"] == "60/hour"
    assert binding_rate["outstanding_confirmations_per_ip"] == config.RESOURCE_AUTH_V2_MAX_PENDING_PER_SOURCE
    command_rate = paths[API + "/commands"]["post"]["x-lockbot-rate-limit"]
    assert command_rate["shared_credential_budget"]["per_minute"] == config.RESOURCE_API_WRITE_BUDGET_PER_MINUTE
    read_rate = paths[API + "/resources"]["get"]["x-lockbot-rate-limit"]
    assert read_rate["ingress_source_per_minute"] == 2400
    assert read_rate["read_admission"]["per_credential_in_flight"] == 2
    assert read_rate["read_admission"]["queue_wait_seconds"] == 0.2
    watch_rate = paths[API + "/resources/events"]["get"]["x-lockbot-rate-limit"]
    assert watch_rate["concurrent_streams"] == {"per_identity": 2, "total": 512}
    assert "/api/agent-skill" not in v1["paths"]
    assert client.get("/api/agent-skill/lockbot-resource-agent.zip").status_code == 404


@pytest.mark.parametrize("lang, heading", [("zh", "HTTP 快速开始"), ("en", "HTTP quickstart")])
def test_agent_v2_documentation_links_to_quickstart(client, lang, heading):
    headers = {"Accept-Language": lang}
    directory = client.get("/api/public-docs", headers=headers).raise_for_status().text
    assert '<a href="/api/public-docs/agent"><s>Agent API v1</s></a>' in directory
    assert '<a href="/api/public-docs/agent-v2">Agent API v2</a>' in directory
    legacy = client.get("/api/public-docs/agent", headers=headers).raise_for_status().text
    assert "/api/public-openapi.json?scope=agent" in legacy
    assert "text-decoration: line-through" in legacy
    response = client.get("/api/public-docs/agent-v2", headers=headers).raise_for_status()
    assert response.headers["content-language"] == lang
    document = response.text
    quickstart_path = "/api/public-docs/agent-v2/quickstart"
    assert f'href="{quickstart_path}"' in document
    assert heading in document and 'id="swagger-ui"' in document
    assert "<details" not in document
    assert "text-decoration: line-through" not in document
    guide = client.get(quickstart_path, headers=headers).raise_for_status()
    assert guide.headers["content-language"] == lang
    assert heading in guide.text and 'id="swagger-ui"' not in guide.text
    assert '<code class="language-bash">' in guide.text
    assert 'class="nv"' in guide.text and 'class="s1"' in guide.text
    assert "cdn.jsdelivr" not in guide.text
    assert all(key in guide.text for key in ("resource_refs", "booking_ref", "Idempotency-Key"))


def test_openapi_v2_documents_binding_rules_and_example(client):
    from lockbot.backend.app.resource_api.v2.binding import BindingRuleDirectory

    document = client.get(API + "/openapi.json").raise_for_status().json()
    endpoint = document["paths"][API + "/bindings"]["get"]
    schemas = document["components"]["schemas"]
    for model in ("BindingLockRules", "BindingLowUtilizationRule", "IdleThresholds", "OvernightRule"):
        for name, field in schemas[model]["properties"].items():
            assert field.get("description"), f"{model}.{name} needs a field description"
            assert f"`{name}`" in endpoint["description"] or f".{name}`" in endpoint["description"]
    example = endpoint["responses"]["200"]["content"]["application/json"]["examples"]["bound"]["value"]
    details = BindingRuleDirectory.model_validate(example).grants[0]
    assert details.lock_rules.claim_window_seconds == 300
    assert details.low_utilization.thresholds.accelerator_utilization_percent == [5.0]


def test_openapi_v2_shows_command_examples_and_outcomes(client):
    from lockbot.backend.app.resource_api.v2.commands import Command
    from lockbot.metrics.gateway import validate_query

    paths = client.get(API + "/openapi.json").raise_for_status().json()["paths"]
    endpoint = paths[API + "/commands"]["post"]
    request_json = endpoint["requestBody"]["content"]["application/json"]
    examples = request_json["examples"]
    assert set(examples) == {"lock", "lock_multiple", "slock", "book", "lock_booking", "free", "label", "keep"}
    commands = {name: Command.model_validate(example["value"]) for name, example in examples.items()}
    assert len(commands["lock_multiple"].resource_refs) == 2
    assert commands["lock_booking"].operation == "lock"
    assert commands["lock_booking"].booking_ref and not commands["lock_booking"].resource_refs
    assert endpoint["parameters"][0]["description"]
    assert {"200", "401", "403", "404", "409", "422", "429", "503"} <= endpoint["responses"].keys()
    success = endpoint["responses"]["200"]["content"]["application/json"]["examples"]
    assert all(example["value"]["status"] == "completed" for example in success.values())
    assert all(op["description"] for methods in paths.values() for op in methods.values())
    metrics = paths[API + "/resources/{resource_ref}/metrics"]["get"]["description"]
    query = metrics.split("'query=", 1)[1].split("'", 1)[0]
    assert validate_query(query, {"node-23"}) == ({"lockbot_accelerator_utilization_percent"}, {"node-23"})


def test_openapi_v2_documents_each_http_operation(client):
    import json
    import re
    import subprocess

    paths = client.get(API + "/openapi.json").raise_for_status().json()["paths"]
    for path, methods in paths.items():
        for method, operation in methods.items():
            snippets = re.findall(r"```sh\n(.*?)\n```", operation["description"], re.S)
            assert snippets, (method, path)
            for snippet in snippets:
                subprocess.run(["bash", "-n"], input=snippet, text=True, check=True)
            assert "x-lockbot-rate-limit" in operation
            status, response = next(
                (status, reply) for status, reply in operation["responses"].items() if status.startswith("2")
            )
            if status == "204":
                assert "content" not in response
            elif path.endswith("/events"):
                frame = response["content"]["text/event-stream"]["example"]
                event = json.loads(frame.split("data: ", 1)[1])
                assert frame.startswith("id: ") and "\nevent: snapshot\n" in frame
                assert event["rows"][0]["booking"]["claimable"]
            elif path.endswith("/skill/lockbot.zip"):
                assert response["content"]["application/zip"]["schema"] == {"type": "string", "format": "binary"}
                assert "security" not in operation
            else:
                assert response["content"]["application/json"]["examples"]
    binding = issue(client)
    assert finish(client, binding).json() == {"status": "pending"}
    responses = paths[API + "/bindings"]["post"]["responses"]
    assert binding.keys() == responses["201"]["content"]["application/json"]["examples"]["started"]["value"].keys()


def test_agent_v2_publishes_downloadable_skill(client, monkeypatch):
    from hashlib import sha256

    monkeypatch.setenv("PLATFORM_URL", "https://lockbot.example.test:8155")
    manifest = client.get(API + "/manifest").raise_for_status().json()
    release = manifest["skill"]
    assert release["name"] == "lockbot"
    assert release["download_url"] == API + "/skill/lockbot.zip"
    response = client.get(release["download_url"], headers={"Host": "unexpected.example.test"}).raise_for_status()
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as bundle:
        markdown = bundle.read("lockbot/SKILL.md").decode()
    assert markdown.startswith("---\nname: lockbot\n")
    assert sha256(response.content).hexdigest() == release["sha256"]
    assert "`lb login https://lockbot.example.test:8155`" in markdown
    assert "Use `lb resource list` to search all saved, authorized connections" in markdown
    assert "only after the user explicitly authorizes binding" in markdown
    assert "If the user gives only a duration" in markdown
    assert "first run `lb resource view <user-supplied target> --json`" in markdown
    assert "Consider busy matches as well as available ones" in markdown
    assert "A label, node name, or bot name is not hardware evidence" in markdown
    assert "Proceed only after a fresh, complete result confirms the target." in markdown
    assert "If it cannot be confirmed, report the result as unknown and stop." in markdown
    assert "unexpected.example.test" not in markdown
    assert "`lb lock" in markdown and "`lb book" in markdown
    assert "/api/agent-skill" not in markdown

    monkeypatch.setenv("PLATFORM_URL", "https://another.example.test")
    updated = client.get(API + "/manifest").raise_for_status().json()["skill"]
    assert updated["sha256"] != release["sha256"]
    monkeypatch.delenv("PLATFORM_URL")
    unconfigured = client.get(release["download_url"]).raise_for_status()
    with ZipFile(BytesIO(unconfigured.content)) as bundle:
        markdown = bundle.read("lockbot/SKILL.md").decode()
    assert "Ask the user for the LockBot service address" in markdown
    assert "unexpected.example.test" not in markdown


def test_agent_v2_notification_preserves_v1_command_display(monkeypatch):
    from types import SimpleNamespace

    from lockbot.backend.app.resource_api import notifications
    from lockbot.backend.app.resource_api.v2.commands import _notification_command
    from lockbot.core.config import Config

    body = SimpleNamespace(operation="lock", duration="8h")
    rows = [{"node": str(node), "device": None} for node in (15, 16, 17, 18)]
    word, command = _notification_command(body, rows)
    assert (word, command) == ("lock", "lock 15,16,17,18 8h")
    events = []
    monkeypatch.setattr(notifications.notification_budget, "offer", lambda target, event, **_: events.append(event))
    bot = SimpleNamespace(config=Config({"LANGUAGE": "zh"}))
    notifications.notify_group(bot, bot_id=1, command="lock 15,16,17,18 8h", actor_id="test-user", command_word="lock")
    notifications.notify_group(bot, bot_id=1, command=command, actor_id="test-user", command_word=word, defer=True)
    assert events[0] == events[1]
    assert events[1].text == "🤖【Agent API操作】\n\n用户：test-user\n命令：lock 15,16,17,18 8h"
    rows = [{"node": "node-23", "device": card} for card in (0, 2)]
    assert _notification_command(body, rows) == ("lock", "lock node-23 dev0,2 8h")


def make_bot(client, auth_header, name):
    response = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": name,
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"node-23": "demo"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def bearer(key):
    return {"Authorization": "Bearer " + key}


def issue(client, key=None, purpose="bind"):
    response = client.post(API + "/bindings", headers=bearer(key) if key else {}, json={"purpose": purpose})
    assert response.status_code == 201, response.text
    return response.json()


def chat(bot_id, challenge, *, sender="alice"):
    return service.handle_chat_binding(
        bot_id=bot_id, platform="infoflow", external_user_id=sender, action="bind", argument=challenge["bind_code"]
    )


def finish(client, challenge, *, proof=None):
    return client.post(
        API + f"/bindings/{challenge['binding_id']}/complete", json={"proof": proof or challenge["proof"]}
    )


def test_host_inventory_reaches_agent_reads_and_shared_view(
    client, auth_header, session_factory, monkeypatch, tmp_path
):
    from datetime import datetime, timedelta, timezone

    from lockbot.backend.app.bots.manager import BotManager

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "views"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    bot_id = make_bot(client, auth_header, "Hardware inventory")
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": "NODE",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": {"node-23": "demo"},
        },
    )

    try:
        old_hardware = [{"model": "Old device", "count": 2}]
        client.patch(
            f"/api/bots/{bot_id}/node-hardware",
            headers=auth_header,
            json={"version": 1, "nodes": {"node-23": old_hardware}},
        ).raise_for_status()
        config_response = client.post(
            "/api/configuration-credentials",
            headers=auth_header,
            json={"label": "inventory", "bot_ids": [bot_id], "scopes": ["node_inventory:write"]},
        ).raise_for_status()
        config_headers = bearer(config_response.json()["credential"])
        config_path = f"/api/bots/{bot_id}/configuration-access"
        challenge = issue(client)
        chat(bot_id, challenge)
        agent_headers = bearer(finish(client, challenge).raise_for_status().json()["credential"])
        resource_path = API + "/resources"
        bot = client.get(f"/api/bots/{bot_id}", headers=auth_header).raise_for_status().json()
        shared_headers = bearer(bot["view_url"].rsplit("/", 1)[-1])
        shared_path = f"/api/bot-views/{bot_id}/state"
        stamp = datetime.now(timezone.utc)
        accelerators = [
            {"model": "Example Device", "count": 2, "memory_bytes_per_card": 64 * 2**30},
            {"model": "Example Device", "count": 2, "memory_bytes_per_card": 96 * 2**30},
        ]

        attributes = [{"label": "Example setting", "value": "Enabled"}]
        devices = [{"index": 0, "attributes": [{"label": "Example capacity", "value": "20", "unit": "units"}]}]

        def upload(scope, observed_at, **extra):
            sample = {"observed_at": observed_at.isoformat(), "scope": scope, "accelerators": accelerators, **extra}
            payload = {
                "scope": "node_inventory:write",
                "configuration": {"version": 1, "nodes": {"node-23": sample}},
            }
            plan = client.post(config_path + "/preview", headers=config_headers, json=payload).raise_for_status()
            client.put(
                config_path, headers=config_headers, json={**payload, "receipt": plan.json()["receipt"]}
            ).raise_for_status()

        upload("container", stamp - timedelta(seconds=1))
        (fallback,) = client.get(resource_path, headers=agent_headers).raise_for_status().json()["rows"]
        assert fallback["hardware"] == old_hardware

        upload("host", stamp, attributes=attributes, devices=devices)
        expected = [{"model": "Example Device", "count": 4}]
        (found,) = client.get(resource_path, headers=agent_headers).raise_for_status().json()["rows"]
        assert found["hardware"] == expected
        for fields in (None, "hardware"):
            params = {"resource_ref": found["resource_ref"], "model": "eXample device"}
            if fields:
                params["fields"] = fields
            (row,) = client.get(resource_path, headers=agent_headers, params=params).raise_for_status().json()["rows"]
            assert row["hardware"] == expected

        assert "node_attributes" not in found
        details = (
            client.get(resource_path, headers=agent_headers, params={"node": "node-23", "fields": "node_attributes"})
            .raise_for_status()
            .json()["rows"]
        )
        assert details[0]["node_attributes"] == {
            "observed_at": stamp.isoformat().replace("+00:00", "Z"),
            "attributes": attributes,
            "devices": devices,
        }
        assert (
            client.get(resource_path, headers=agent_headers, params={"include_node_attributes": "true"}).status_code
            == 422
        )

        (shared,) = client.get(shared_path, headers=shared_headers).raise_for_status().json()["resources"]
        assert shared["hardware"] == expected
        assert "node_attributes" not in shared
    finally:
        manager.stop_bot(bot_id)


def test_management_activity_identifies_v2_tokens_and_counts_recent_verified_access(
    client, auth_header, admin_header, db_session, session_factory, monkeypatch
):
    from lockbot.backend.app.auth.models import User

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    client.app.state.v2_agent_activity.engine = session_factory.kw["bind"]
    bot_id = make_bot(client, auth_header, "Versioned statistics")
    before = client.get("/api/public/stats", headers=auth_header).raise_for_status().json()
    challenge = issue(client)
    chat(bot_id, challenge)
    key = finish(client, challenge).raise_for_status().json()["credential"]
    assert client.get(API + "/bindings", headers=bearer(key)).status_code == 200
    assert client.delete(API + "/bindings/missing-grant", headers=bearer(key)).status_code == 404
    assert client.get(API + "/resources", headers=bearer("lbv2_unknown")).status_code == 401
    client.app.state.v2_agent_activity.flush()
    path = "/api/admin/agent-activity"
    assert client.get(path, headers=auth_header).status_code == 403
    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    activity = client.get(path, headers=admin_header).raise_for_status().json()
    assert activity["total"] == 1
    assert activity["items"][0]["version"] == "v2"
    assert activity["items"][0]["read_requests"] == 1
    assert activity["items"][0]["write_requests"] == activity["items"][0]["refused_requests"] == 1
    assert client.get(f"/api/bots/{bot_id}/agent-activity", headers=auth_header).json()["total"] == 0
    stats = client.get("/api/public/stats", headers=auth_header).raise_for_status().json()
    assert stats["agent_tokens"] == before["agent_tokens"] + 1
    assert stats["agent_requests_hour"] == before["agent_requests_hour"] + 2

    from datetime import datetime, timedelta

    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.resource_api.activity import AgentActivity
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    bot = db_session.get(Bot, bot_id)
    legacy = ResourceBinding(
        public_id="legacy-fixture",
        platform="infoflow",
        external_user_id="legacy-user",
        bound_bot_id=bot.id,
        bound_bot_uuid=bot.uuid,
        bound_identity_revision=bot.identity_revision,
        credential_digest="legacy-private-digest",
    )
    db_session.add(legacy)
    db_session.flush()
    db_session.add(
        AgentActivity(
            binding_id=legacy.id,
            read_requests=2,
            last_status=200,
            last_seen_at=datetime.utcnow() - timedelta(minutes=1),
        )
    )
    db_session.commit()
    for page, expected in ((1, "v2"), (2, "v1")):
        result = (
            client.get(path, headers=admin_header, params={"include_legacy": True, "limit": 1, "page": page})
            .raise_for_status()
            .json()
        )
        assert result["total"] == 2
        (item,) = result["items"]
        assert item["version"] == expected
        assert "sort_key" not in item and "credential_digest" not in item
        assert item["bot_name"] == ("Versioned statistics" if expected == "v1" else None)
    assert client.get(path, headers=admin_header).json()["total"] == 1

    legacy.credential_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    history = (
        client.get(path, headers=admin_header, params={"include_legacy": True, "status": "history"})
        .raise_for_status()
        .json()
    )
    (item,) = history["items"]
    assert item["version"] == "v1" and item["expired"] and not item["active"]
    assert item["read_requests"] == 2


def test_v2_binding_source_capacity_releases_confirmed_slot(client, auth_header, session_factory, monkeypatch):
    from lockbot.backend.app import config

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    bot_id = make_bot(client, auth_header, "shared-source")
    outstanding = [issue(client) for _ in range(config.RESOURCE_AUTH_V2_MAX_PENDING_PER_SOURCE)]
    over_capacity = client.post(API + "/bindings", json={})
    assert over_capacity.status_code == 429
    assert over_capacity.json()["detail"]["code"] == "too_many_pending_bindings"
    assert over_capacity.headers["Retry-After"] == "600"

    first = outstanding[0]
    chat(bot_id, first)
    issue(client)
    completion = finish(client, first).raise_for_status().json()
    assert completion["status"] == "confirmed"
    assert finish(client, first).json() == completion


@pytest.mark.parametrize("external_enabled", [False, True])
def test_browser_uses_v2_grants_without_storing_the_key(
    client, auth_header, session_factory, monkeypatch, tmp_path, external_enabled
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.personal_panel.native_v2 import NativeV2Provider
    from lockbot.backend.app.personal_panel.router import create_panel
    from lockbot.backend.app.resource_api.v2.binding import PersonalCredential

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "v2.commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    bot_id = make_bot(client, auth_header, "Browser lab")
    unrelated = make_bot(client, auth_header, "Other lab")
    for current in (bot_id, unrelated):
        manager.start_bot(
            current,
            {
                "BOT_TYPE": "NODE",
                "BOT_ID": current,
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"node-23": "demo"},
            },
        )

    def bind(target, sender):
        challenge = issue(client)
        chat(target, challenge, sender=sender)
        return finish(client, challenge).json()["credential"]

    origin = "https://lockbot.example"
    api_path = "/api/personal-panel"
    provider = NativeV2Provider(origin, session_factory=session_factory)
    remote_key = "synthetic-external-key"

    class ExternalProvider:
        workbench_credential_label = "Example reports"
        proof = {
            "issuer": "https://reports.example",
            "platform": "infoflow",
            "realm": "external-reports",
            "subject": "external-alice",
            "scope": "read-only",
            "ref": "opaque-external-reference",
        }

        def authenticate(self, key):
            return self.proof if key == remote_key else None

        def validate(self, proof):
            return proof == self.proof

        def report(self, proof):
            assert self.validate(proof)
            return [{"label": "External lab", "status": "ok", "allocations": []}]

    state = tmp_path / "panel.db"
    app = FastAPI()
    primary = ExternalProvider() if external_enabled else provider
    app.mount(api_path, create_panel(origin, state, primary, native_v2=provider))
    browser = TestClient(app, base_url=origin, headers={"Origin": origin})
    agent = TestClient(app, base_url=origin)

    try:
        key = bind(bot_id, "alice")
        other = bind(unrelated, "bob")
        (other_row,) = client.get(API + "/resources", headers=bearer(other)).json()["rows"]
        (row,) = client.get(API + "/resources", headers=bearer(key)).json()["rows"]
        assert (
            client.post(
                API + "/commands",
                headers={**bearer(key), "Idempotency-Key": "browser-integration-lock"},
                json={"operation": "lock", "resource_refs": [row["resource_ref"]], "duration": "30m"},
            ).status_code
            == 200
        )

        assert browser.post(api_path + "/catalog", json={}).status_code == 401
        assert browser.post(api_path + "/v2-key", json={"key": "lbr_legacy", "mode": "replace"}).status_code == 401
        assert agent.post(api_path + "/v2-key", json={"key": key, "mode": "replace"}).status_code == 403
        assert browser.post(api_path + "/v2-key", json={"key": key, "mode": "replace"}).status_code == 200

        session = browser.get(api_path + "/session").json()
        assert session["native_v2"]["accounts"][0]["subject"] == "alice"
        assert session["native_v2"]["external_provider"] == ("Example reports" if external_enabled else None)
        (group,) = browser.get(api_path + "/resources").json()["groups"]
        assert group["label"] == "Browser lab"
        assert group["capacity"] == {"nodes": 1, "cards": None}
        assert group["allocations"][0]["resource_ref"] == row["resource_ref"]
        assert group["allocations"][0]["kind"] == "hold"
        (result,) = browser.post(api_path + "/catalog", json={"mine": True}).json()["rows"]
        assert result["resource_ref"] == row["resource_ref"]
        assert result["status"] == "exclusive"
        assert browser.post(api_path + "/catalog", json={"resource_ref": other_row["resource_ref"]}).status_code == 404
        assert browser.post(api_path + "/catalog", json={"operation": "free"}).status_code == 422
        assert browser.post(api_path + "/account-links", headers=bearer(key)).status_code == 404

        link = agent.post(api_path + "/v2-links", headers=bearer(key)).raise_for_status().json()
        token = link["login_url"].split("ticket=")[1]
        assert key not in link["login_url"]
        assert browser.post(api_path + "/preview", json={"ticket": token}).json()["identity"]["subject"] == "alice"
        assert browser.post(api_path + "/login", json={"ticket": token, "mode": "replace"}).status_code == 200
        assert browser.post(api_path + "/login", json={"ticket": token, "mode": "replace"}).status_code == 410
        if external_enabled:
            assert (
                browser.post(api_path + "/external-key", json={"key": remote_key, "mode": "append"}).status_code == 200
            )
            groups = browser.get(api_path + "/resources").json()["groups"]
            assert {group["label"] for group in groups} == {"Browser lab", "External lab"}
            assert remote_key.encode() not in state.read_bytes()
        assert key.encode() not in state.read_bytes()

        with session_factory() as db:
            principal = db.query(PersonalCredential).filter_by(external_user_id="alice").one()
            principal.generation += 1
            db.commit()
        rotated = browser.get(api_path + "/session").json()
        assert rotated["authenticated"] is external_enabled
        assert rotated["native_v2"]["accounts"] == []
        assert browser.post(api_path + "/catalog", json={}).status_code == 401
    finally:
        manager.shutdown_all()


@pytest.mark.parametrize("bot_type", ["QUEUE", "QUEUE_DEVICE"])
def test_v2_watch_reports_timing_while_group_reminders_follow_recipient_source(
    client,
    auth_header,
    session_factory,
    monkeypatch,
    tmp_path,
    bot_type,
):
    import asyncio
    import json
    import time
    from contextlib import asynccontextmanager
    from datetime import datetime

    from fastapi import Request
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.resource_api import notifications
    from lockbot.backend.app.resource_api.v2 import resources, watch
    from lockbot.core.handler import execute_text_command

    real_time = int(time.time())
    clock = real_time

    class ResourceTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock, tz=tz)

    monkeypatch.setattr(time, "time", lambda: clock)
    monkeypatch.setattr(resources, "datetime", ResourceTime)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "v2.commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    cards = bot_type == "QUEUE_DEVICE"
    cluster = {"node-23": ["p800", "p800"]} if cards else {"node-23": "demo"}
    selector = "node-23 dev0" if cards else "node-23"
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "queue-lab",
            "bot_type": bot_type,
            "cluster_configs": cluster,
            "webhook_url": "https://example.invalid/hook",
        },
    ).raise_for_status()
    bot_id = created.json()["id"]
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": bot_type,
            "BOT_NAME": "queue-lab",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": cluster,
            "EARLY_NOTIFY": True,
            "TIME_ALERT": 30,
        },
    )
    with manager.admit(bot_id) as lease:
        bot = lease.bot
    notices = []

    def send(message):
        assert not bot._lock.locked()
        items = message["message"]["body"]
        mentions = [user for item in items if item["type"] == "AT" for user in item["atuserids"]]
        notices.append((bot.adapter.extract_reply_text(message), mentions))
        if len(notices) == 1:
            raise OSError("fixture webhook failure")

    monkeypatch.setattr(bot.adapter, "send", send)
    announcements = []
    monkeypatch.setattr(
        notifications.notification_budget, "offer", lambda target, event, **_: announcements.append(event)
    )

    def connect(actor):
        challenge = issue(client)
        chat(bot_id, challenge, sender=actor)
        return bearer(finish(client, challenge).json()["credential"])

    async def event(stream):
        frame = await asyncio.wait_for(anext(stream), timeout=3)
        fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
        return fields["id"], fields["event"], json.loads(fields["data"])

    @asynccontextmanager
    async def stream(headers, last_event_id=None):
        async def receive():
            await asyncio.Future()

        request = Request(
            {
                "type": "http",
                "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            },
            receive=receive,
        )
        response = await watch.watch_resources(request, mine=True, last_event_id=last_event_id)
        try:
            yield response.body_iterator
        finally:
            await response.body_iterator.aclose()
            await response.background()

    async def exercise():
        nonlocal clock
        holder = connect("holder")
        waiter = connect("api-waiter")
        filters = {"device": 0} if cards else {}
        (resource,) = client.get(API + "/resources", headers=holder, params=filters).raise_for_status().json()["rows"]
        ref = resource["resource_ref"]

        def command(headers, operation, request_id):
            return (
                client.post(
                    API + "/commands",
                    headers={**headers, "Idempotency-Key": request_id},
                    json={"operation": operation, "resource_refs": [ref], "duration": "1m"},
                )
                .raise_for_status()
                .json()
            )

        command(holder, "lock", "original-lock")
        command(waiter, "book", "booking")
        execute_text_command("chat-waiter", f"book {selector} 1m", bot, source="chat")
        async with stream(holder) as holds, stream(waiter) as waits:
            assert (await event(holds))[2]["rows"][0]["holding"]["renewal_due"] is False
            first = (await event(waits))[2]["rows"][0]["booking"]["estimated_ready_at"]
            assert datetime.fromisoformat(first).timestamp() == real_time + 60

            renewed = command(holder, "lock", "renew-once")
            assert command(holder, "lock", "renew-once") == renewed
            assert len(notices) == 1 and notices[0][1] == ["chat-waiter"]
            assert "wait" in notices[0][0] or "等待时间已增加" in notices[0][0]
            assert any(f"lock {selector} 1m" in notice.text for notice in announcements)
            after, kind, change = await event(waits)
            assert kind == "resource.changed"
            estimate = change["rows"][0]["booking"]["estimated_ready_at"]
            assert datetime.fromisoformat(estimate).timestamp() == real_time + 120
            async with stream(waiter, last_event_id=after) as resumed:
                assert (await event(resumed))[2]["rows"] == []
            assert (await event(holds))[2]["rows"][0]["holding"]["renewal_due"] is False

            clock = real_time + 95
            bot._check_and_notify()
            assert (await event(holds))[2]["rows"][0]["holding"]["renewal_due"] is True
            assert len(notices) == 1

            clock = real_time + 121
            bot._check_and_notify()
            release = (await event(holds))[2]
            assert release["removed"] == [ref]
            (expired,) = release["ended"]
            assert (expired["resource_ref"], expired["kind"]) == (ref, "holding_expired")
            assert datetime.fromisoformat(expired["deadline"]).timestamp() == real_time + 120
            waiting = (await event(waits))[2]
            assert "ended" not in waiting
            ready = waiting["rows"][0]["booking"]
            assert ready["claimable"] is True
            assert datetime.fromisoformat(ready["claim_expires_at"]).timestamp() == clock + 300
            assert len(notices) == 1

            clock += 301
            bot._check_and_notify()
            timeout = (await event(waits))[2]
            assert timeout["removed"] == [ref]
            (ended,) = timeout["ended"]
            assert (ended["resource_ref"], ended["kind"], ended["deadline"]) == (
                ref,
                "booking_timed_out",
                ready["claim_expires_at"],
            )
            assert len(notices) == 2 and notices[-1][1] == ["chat-waiter"]

    try:
        asyncio.run(exercise())
    finally:
        manager.shutdown_all()


def test_personal_key_adds_grants_only_for_confirmed_identity(client, auth_header, session_factory, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    assert client.get(API + "/manifest").json()["api_version"] == "2"
    schema = client.get(API + "/openapi.json").json()
    assert schema["components"]["securitySchemes"]["PersonalKey"]["bearerFormat"] == "lbv2_…"
    assert schema["paths"][API + "/commands"]["post"]["security"] == [{"PersonalKey": []}]
    assert "lock_rules" in schema["components"]["schemas"]["BindingRuleGrant"]["properties"]
    assert "thresholds" in schema["components"]["schemas"]["BindingLowUtilizationRule"]["properties"]
    assert all(path.startswith(API + "/") for path in schema["paths"])
    first_bot = make_bot(client, auth_header, "first")
    second_bot = make_bot(client, auth_header, "second")
    first = issue(client)
    assert finish(client, first).json() == {"status": "pending"}
    assert finish(client, first, proof=first["bind_code"]).status_code == 404

    chat(first_bot, first)
    enrollment = finish(client, first).json()
    assert enrollment["status"] == "confirmed"
    assert (
        client.get(
            f"/api/resource-auth/credential-challenges/{first['binding_id']}", headers=bearer(first["proof"])
        ).status_code
        == 404
    )
    key = enrollment["credential"]
    assert key.startswith("lbv2_")
    assert client.get("/api/agent/context", headers=bearer(key)).status_code == 401

    wrong_sender = issue(client, key)
    chat(second_bot, wrong_sender, sender="bob")
    assert finish(client, wrong_sender).json() == {"status": "pending"}
    grants = client.get(API + "/bindings", headers=bearer(key)).raise_for_status().json()["grants"]
    assert [x["name"] for x in grants] == ["first"]
    assert grants[0]["lock_rules"]["unit"] == "node"
    assert grants[0]["lock_rules"]["claim_window_seconds"] is None
    assert grants[0]["lock_rules"]["max_book_duration_seconds"] is None
    assert grants[0]["lock_rules"]["shared_lock_supported"] is True
    assert grants[0]["low_utilization"]["thresholds"] is None
    assert grants[0]["low_utilization"]["max_keeps"] == 1

    second = wrong_sender
    chat(second_bot, second)
    result = finish(client, second).json()
    assert result["status"] == "confirmed"
    assert "credential" not in result
    assert {x["name"] for x in client.get(API + "/bindings", headers=bearer(key)).json()["grants"]} == {
        "first",
        "second",
    }
    second_ref = result["grant"]["grant_ref"]
    assert client.delete(API + f"/bindings/{second_ref}", headers=bearer(key)).status_code == 204
    assert finish(client, second).status_code == 404
    assert [x["name"] for x in client.get(API + "/bindings", headers=bearer(key)).json()["grants"]] == ["first"]

    fresh_sign_in = issue(client)
    chat(first_bot, fresh_sign_in)
    assert finish(client, fresh_sign_in).json() == {"status": "pending"}
    recovery = issue(client, purpose="recover")
    chat(first_bot, recovery)
    assert finish(client, recovery).json()["credential"] == key


def test_v2_reset_is_explicit_and_preserves_confirmed_legacy_scope(client, auth_header, session_factory, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    first_bot = make_bot(client, auth_header, "first")
    second_bot = make_bot(client, auth_header, "second")
    legacy = client.post("/api/resource-auth/bindings").json()
    chat(second_bot, legacy)
    legacy_key = legacy["credential"]
    assert legacy_key.startswith("lbr_")
    assert client.get("/api/agent/context", headers=bearer(legacy_key)).status_code == 200
    assert client.get(API + "/bindings", headers=bearer(legacy_key)).status_code == 401
    assert client.post(API + "/bindings", headers=bearer(legacy_key), json={}).status_code == 401

    first = issue(client)
    chat(first_bot, first)
    previous = finish(client, first).json()["credential"]
    assert previous.startswith("lbv2_")
    assert client.get("/api/agent/context", headers=bearer(previous)).status_code == 401
    assert client.get(API + "/bindings", headers=bearer("lbv2_" + legacy_key.removeprefix("lbr_"))).status_code == 401
    assert client.get("/api/agent/context", headers=bearer(legacy["credential"])).status_code == 200

    reset = issue(client, previous, purpose="reset")
    chat(first_bot, reset)
    new_key = finish(client, reset).json()["credential"]
    assert previous != new_key
    assert client.get(API + "/bindings", headers=bearer(previous)).status_code == 401
    assert finish(client, first).status_code == 404
    assert client.get("/api/agent/context", headers=bearer(legacy["credential"])).status_code == 401
    assert {x["name"] for x in client.get(API + "/bindings", headers=bearer(new_key)).json()["grants"]} == {
        "first",
        "second",
    }


def test_legacy_booking_ref_survives_the_claim_window_opening():
    from lockbot.backend.app.resource_api.v2.resources import booking_ref

    resource = "res_" + "1" * 32
    pending = {"user_id": "alice", "start_time": 100, "duration": 1800}
    ready = {**pending, "booked_start_time": 100, "start_time": 200, "is_notified": True}
    replacement = {**pending, "start_time": 201, "holding_id": "new-booking-id"}

    assert booking_ref(resource, "principal", pending) == booking_ref(resource, "principal", ready)
    assert booking_ref(resource, "principal", pending) != booking_ref(resource, "principal", replacement)


def test_queue_timing_distinguishes_join_time_and_claim_window():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from lockbot.backend.app.resource_api.v2.resources import _resource_people

    principal = SimpleNamespace(platform="infoflow", external_user_id="bob")
    ready = {"user_id": "alice", "start_time": 220, "booked_start_time": 100, "is_notified": True, "duration": 1800}
    waiting = {"user_id": "bob", "start_time": 160, "duration": 1200}
    raw = {"status": "idle", "booking_list": [ready, waiting]}
    first, second = _resource_people(raw, principal, None, datetime.fromtimestamp(250, tz=timezone.utc))["queue"]
    assert first["waited_seconds"] == 150 and first["estimated_wait_seconds"] == 0
    assert first["claim_expires_at"] == datetime.fromtimestamp(520, tz=timezone.utc).isoformat()
    assert second["waited_seconds"] == 90 and second["estimated_wait_seconds"] == 2070
    expired = _resource_people(raw, principal, None, datetime.fromtimestamp(521, tz=timezone.utc))["queue"]
    assert all(row["estimated_wait_seconds"] is None and row["claim_expires_at"] is None for row in expired)


def test_agent_v2_resources_commands_and_queue(client, auth_header, session_factory, monkeypatch, tmp_path):
    import json
    from contextlib import closing

    import jwt
    from fastapi.testclient import TestClient
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.bots.models import Bot

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "v2.commands", "resources", "commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    nodes = {"node-23": ["p800", "p800"], "node-24": ["p800", "p900"]}
    response = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "card-lab",
            "bot_type": "QUEUE_DEVICE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": nodes,
        },
    )
    assert response.status_code == 201, response.text
    bot_id = response.json()["id"]
    from lockbot.backend.app.bots.node_access import serialize_node_access

    with session_factory() as database:
        database.get(Bot, bot_id).node_access_config = serialize_node_access(
            {
                "node-23": {
                    "host": "node-23.example.test",
                    "ip": "192.0.2.41",
                    "port": 22,
                    "method": "fixture-private-method",
                    "note": "fixture-private-note",
                }
            }
        )
        database.commit()
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": "QUEUE_DEVICE",
            "BOT_NAME": "card-lab",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": nodes,
        },
    )

    def connect(actor):
        challenge = issue(client)
        chat(bot_id, challenge, sender=actor)
        return bearer(finish(client, challenge).json()["credential"])

    def command(headers, operation, *refs, duration=None, booking=None, idem=None, connection=client):
        body = {"operation": operation}
        if refs:
            body["resource_refs"] = list(refs)
        if duration:
            body["duration"] = duration
        if booking:
            body["booking_ref"] = booking
        if idem:
            headers = {**headers, "Idempotency-Key": idem}
        return connection.post(API + "/commands", headers=headers, json=body)

    try:
        alice = connect("alice")
        bob = connect("bob")
        detail = (
            client.get(API + "/resources", headers=alice, params={"node": "node-23", "include_node_access": "true"})
            .raise_for_status()
            .json()["rows"]
        )
        assert len(detail) == 2
        assert all(
            row["node_access"] == {"host": "node-23.example.test", "ip": "192.0.2.41", "port": 22} for row in detail
        )
        assert all("holding" not in row for row in detail)
        assert client.get(API + "/resources", headers=alice, params={"include_node_access": "true"}).status_code == 422
        page = client.get(API + "/resources", headers=alice, params={"unit": "card", "model": "P800", "limit": 2})
        assert page.status_code == 200, page.text
        assert len(page.json()["rows"]) == 2
        assert all("node_access" not in row for row in page.json()["rows"])
        assert page.json()["next_cursor"]
        rest = client.get(
            API + "/resources",
            headers=alice,
            params={
                "unit": "card",
                "model": "p800",
                "cursor": page.json()["next_cursor"],
            },
        ).json()["rows"]
        assert len(rest) == 1
        refs = {(r["node"], r["device"]): r["resource_ref"] for r in page.json()["rows"] + rest}

        ref_a = refs["node-23", 1]
        ref_b = refs["node-24", 0]
        for retired_or_invalid in (
            {"agent_label": "private-v2-detail"},
            {"purpose": "private-v2-detail"},
            {"task_category": "private-v2-detail"},
        ):
            invalid = client.post(
                API + "/commands",
                headers=alice,
                json={
                    "operation": "lock",
                    "resource_refs": [ref_a],
                    **retired_or_invalid,
                },
            )
            assert invalid.status_code == 422 and "private-v2-detail" not in invalid.text
        label_request = {"operation": "label", "resource_refs": [ref_a], "label": "训练"}
        rejected = client.post(API + "/commands", headers=alice, json=label_request)
        assert rejected.json()["code"] == "labels_disabled"
        with manager.admit(bot_id) as lease:
            lease.bot.config.set_val("ALLOW_USER_LABEL", True)
        label_write = client.post(API + "/commands", headers=alice, json=label_request).raise_for_status()
        assert label_write.json()["resources"][0]["label"] == "训练"
        by_label = client.get(API + "/resources", headers=bob, params={"node": "训练"}).raise_for_status()
        assert {row["node"] for row in by_label.json()["rows"]} == {"node-23"}
        by_label_card = client.get(
            API + "/resources",
            headers=bob,
            params={
                "node": "训练",
                "device": 1,
                "fields": "node,device",
            },
        ).raise_for_status()
        assert by_label_card.json()["rows"] == [{"resource_ref": ref_a, "node": "node-23", "device": 1}]
        truncated_catalog = client.get(
            API + "/resources",
            headers=bob,
            params={"node_prefix": "node-24", "fields": "node"},
        ).raise_for_status()
        assert {row["node"] for row in truncated_catalog.json()["rows"]} == {"node-24"}
        client.post(API + "/commands", headers=alice, json={**label_request, "label": ""}).raise_for_status()
        with closing(TestClient(client.app, client=("192.0.2.42", 50000))) as agent_client:
            locked = command(alice, "lock", ref_a, ref_b, duration="30m", idem="once", connection=agent_client)
        assert locked.status_code == 200, locked.text
        assert {r["resource_ref"] for r in locked.json()["resources"]} == {ref_a, ref_b}

        from lockbot.backend.app.resource_api.v2 import resources as v2_resources

        original_project = v2_resources.project_rows
        observed_scopes = []

        def capture_scope(bot, grant, principal, commands, aliases, refs, observed, state, *, hardware):
            card_ids = {node: [card["dev_id"] for card in cards] for node, cards in state.items()}
            observed_scopes.append((set(refs), card_ids))
            return original_project(bot, grant, principal, commands, aliases, refs, observed, state, hardware=hardware)

        with monkeypatch.context() as scoped:
            scoped.setattr(v2_resources, "project_rows", capture_scope)
            exact = client.get(API + "/resources", headers=alice, params={"resource_ref": ref_a}).raise_for_status()
            (single,) = exact.json()["rows"]
            assert single["resource_ref"] == ref_a and single["holding"]["mode"] == "exclusive"
            devices = client.get(API + "/resources", headers=alice, params={"parent_ref": ref_a}).raise_for_status()
            assert sorted(row["device"] for row in devices.json()["rows"]) == [0, 1]
            filtered = client.get(
                API + "/resources", headers=alice, params={"unit": "card", "model": "P900", "available": True}
            ).raise_for_status()
            (row,) = filtered.json()["rows"]
            assert (row["node"], row["device"]) == ("node-24", 1)
            card = client.get(
                API + "/resources",
                headers=alice,
                params={
                    "node": "node-23",
                    "device": 1,
                    "include_people": True,
                },
            ).raise_for_status()
            (row,) = card.json()["rows"]
            assert row["resource_ref"] == ref_a and row["holders"][0]["name"] == "alice"
        assert observed_scopes == [
            ({("node-23", 1)}, {"node-23": [1]}),
            ({("node-23", 0), ("node-23", 1)}, {"node-23": [0, 1]}),
            ({("node-23", 0), ("node-23", 1), ("node-24", 0), ("node-24", 1)}, {"node-24": [1]}),
            ({("node-23", 1), ("node-24", 1)}, {"node-23": [1]}),
        ]
        mismatched_card = client.get(API + "/resources", headers=alice, params={"resource_ref": ref_a, "device": 0})
        assert mismatched_card.status_code == 404

        metrics_url = API + f"/resources/{ref_a}/metrics"
        assert client.get(metrics_url, headers=alice).json()["detail"]["code"] == "metrics_not_configured"
        endpoint = "https://metrics.example.test"
        with session_factory() as db:
            db.get(Bot, bot_id).utilization_config = json.dumps(
                {
                    "endpoint": endpoint,
                    "profile": "lockbot-v1",
                    "accessMode": "short_lived",
                    "nodeMapping": {"node-23": "source-23"},
                    "deviceMapping": {"node-23:dev1": "card-b"},
                }
            )
            db.commit()
        for headers in (alice, bob):
            (row,) = (
                client.get(
                    API + "/resources",
                    headers=headers,
                    params={"resource_ref": ref_a},
                )
                .raise_for_status()
                .json()["rows"]
            )
            assert row["metrics"] == "ready"
        assert client.get(metrics_url).status_code == 401
        signing_key = tmp_path / "signer.key"
        signing_key.write_bytes(b"test-metrics-signing-key-32-bytes")
        signing_key.chmod(0o600)
        monkeypatch.setenv("METRICS_GRANT_SECRET_FILE", str(signing_key))
        monkeypatch.delenv("METRICS_GRANT_SIGNERS_FILE", raising=False)
        metrics = client.get(metrics_url, headers=bob).raise_for_status()
        assert metrics.headers["Cache-Control"] == "no-store"
        assert metrics.json()["source"]["device"] == "card-b"
        claims = jwt.decode(
            metrics.json()["access"]["token"], signing_key.read_bytes(), algorithms=["HS256"], audience=endpoint
        )
        assert claims["resources"] == {"source-23": ["card-b"]}
        assert 0 < claims["exp"] - claims["iat"] <= 300
        renewed = client.get(metrics_url, headers=bob).raise_for_status().json()["access"]["token"]
        renewed_claims = jwt.decode(renewed, signing_key.read_bytes(), algorithms=["HS256"], audience=endpoint)
        assert renewed_claims["jti"] != claims["jti"]
        assert renewed_claims["resources"] == claims["resources"]
        replay = command(alice, "lock", ref_a, ref_b, duration="30m", idem="once")
        assert replay.json() == locked.json()
        assert command(alice, "lock", ref_a, duration="1h", idem="once").status_code == 409

        free_ref = refs["node-23", 0]
        conflict = command(bob, "lock", free_ref, ref_a, duration="30m")
        assert conflict.status_code == 409
        history_url = f"/api/bots/{bot_id}/agent-history"
        history = client.get(history_url, headers=auth_header, params={"limit": 4}).raise_for_status().json()["items"]
        assert [item["api_version"] for item in history] == [2, 2, 2, 2]
        assert [(item["actor"]["external_user_id"], item["outcome"]) for item in history] == [
            ("bob", "rejected"),
            ("alice", "rejected"),
            ("alice", "replayed"),
            ("alice", "applied"),
        ]
        assert all(item["command"] == "lock" and "credential_ref" not in item for item in history)
        assert history[0]["http_status"] == 409
        assert "source_ip" not in history[2]
        assert history[3]["source_ip"] == "192.0.2.42"
        assert history[3]["requested_resources"] == [
            {"resource_id": "node-23", "device_ids": [1]},
            {"resource_id": "node-24", "device_ids": [0]},
        ]
        assert client.get(API + "/resources", headers=bob, params={"mine": True}).json()["rows"] == []

        booked = client.post(
            API + "/commands",
            headers=bob,
            json={
                "operation": "book",
                "resource_refs": [ref_a],
                "duration": "20m",
                "task_category": "unit_testing",
            },
        )
        assert booked.status_code == 200, booked.text
        booking = booked.json()["resources"][0]["booking_ref"]
        people_response = client.get(
            API + "/resources",
            headers=bob,
            params={
                "resource_ref": ref_a,
                "fields": "holders,queue",
            },
        ).raise_for_status()
        (people,) = people_response.json()["rows"]
        assert people["resource_ref"] == ref_a
        (holder,) = people["holders"]
        (queued,) = people["queue"]
        assert (holder["name"], holder["is_you"], holder["duration_seconds"]) == ("alice", False, 1800)
        assert holder["locked_at"] < holder["expires_at"]
        assert (queued["name"], queued["is_you"], queued["position"], queued["duration_seconds"]) == (
            "bob",
            True,
            1,
            1200,
        )
        assert queued["queued_at"] and queued["estimated_wait_seconds"] == holder["remaining_seconds"]
        assert queued["claim_expires_at"] is None
        assert queued["task_category"] == "unit_testing"
        compact = client.get(API + "/resources", headers=bob, params={"resource_ref": ref_a}).raise_for_status()
        assert not {"holders", "queue"} & compact.json()["rows"][0].keys()
        assert command(bob, "claim", booking=booking).status_code == 422
        assert command(bob, "lock").status_code == 422
        assert command(bob, "lock", ref_a, booking=booking).status_code == 422
        assert command(bob, "lock", booking=booking, duration="10m").status_code == 422
        assert command(bob, "free", booking=booking).status_code == 422
        blocked = command(bob, "lock", booking=booking)
        assert blocked.status_code == 409 and blocked.json()["code"] == "booking_not_claimable"
        wait = client.get(API + "/resources", headers=bob, params={"view": "wait"}).json()["rows"]
        assert [r for r in wait if r["resource_ref"] == ref_a][0]["estimated_seconds"] > 0
        assert command(alice, "free", ref_a, ref_b).status_code == 200
        assert client.get(metrics_url, headers=alice).status_code == 200
        wrong_owner = command(alice, "lock", booking=booking)
        assert wrong_owner.status_code == 409 and wrong_owner.json()["code"] == "booking_not_claimable"
        locked_booking = command(bob, "lock", booking=booking, idem="lock-booking").raise_for_status().json()
        assert locked_booking["operation"] == "lock"
        assert locked_booking["resources"][0]["holding"]["mode"] == "exclusive"
        (row,) = (
            client.get(
                API + "/resources",
                headers=bob,
                params={
                    "resource_ref": ref_a,
                    "include_people": True,
                },
            )
            .raise_for_status()
            .json()["rows"]
        )
        assert row["holders"][0]["task_category"] == "unit_testing"
        assert command(bob, "lock", booking=booking, idem="lock-booking").raise_for_status().json() == locked_booking
        assert command(bob, "free", ref_a).status_code == 200
        assert command(bob, "lock", booking=booking).status_code == 409
        legacy = client.post("/api/resource-auth/bindings").json()
        chat(bot_id, legacy, sender="bob")
        v1_headers = bearer(legacy["credential"])
        monkeypatch.setattr("lockbot.backend.app.config.RESOURCE_AUTH_V1_ENROLLMENT_ENABLED", False)
        assert client.get(f"/api/bots/{bot_id}/resources", headers=v1_headers).status_code == 200
        assert (
            client.post(
                f"/api/bots/{bot_id}/commands",
                headers=v1_headers,
                json={
                    "command": "lock node-23 dev1 30m",
                },
            ).status_code
            == 200
        )
        own_rows = client.get(API + "/resources", headers=bob, params={"mine": True}).json()["rows"]
        assert [row["resource_ref"] for row in own_rows] == [ref_a]
        assert command(bob, "free", ref_a).status_code == 200

        other = make_bot(client, auth_header, "another-allocator")
        manager.start_bot(
            other,
            {
                "BOT_TYPE": "NODE",
                "BOT_NAME": "another-allocator",
                "BOT_ID": other,
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"node-23": "demo"},
            },
        )
        another = issue(client, alice["Authorization"].removeprefix("Bearer "))
        chat(other, another)
        another_grant = finish(client, another).json()["grant"]["grant_ref"]
        other_rows = client.get(API + "/resources", headers=alice, params={"grant_ref": another_grant}).json()["rows"]
        other_ref = other_rows[0]["resource_ref"]
        assert command(bob, "lock", other_ref, duration="30m").status_code == 404
        refused = command(alice, "lock", free_ref, other_ref, duration="30m")
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "cross_bot_batch"
        assert client.get(API + "/resources", headers=alice, params={"mine": True}).json()["rows"] == []
    finally:
        manager.shutdown_all()


@pytest.mark.parametrize("same_source_node", [False, True], ids=["distinct-nodes", "shared-node"])
def test_v2_metrics_keeps_bot_and_card_scope_on_shared_source(
    client, auth_header, session_factory, monkeypatch, tmp_path, same_source_node
):
    import json
    import time
    from io import BytesIO
    from unittest.mock import Mock
    from urllib.parse import parse_qs, urlencode

    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.resource_api.discovery import ensure_instance_id
    from lockbot.metrics.gateway import Gateway, Rejected

    endpoint = "https://shared-metrics.example.invalid"
    signing_key = tmp_path / "source.key"
    signing_key.write_bytes(b"shared-source-test-signing-key-123456")
    signing_key.chmod(0o600)
    registry = tmp_path / "signers.json"
    registry.touch(mode=0o600)
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [{"audience": endpoint, "key_file": str(signing_key)}],
            }
        )
    )
    monkeypatch.setenv("METRICS_GRANT_SIGNERS_FILE", str(registry))
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "v2.commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)

    nodes = {"node_1": ["p800"]}
    metric = "lockbot_accelerator_utilization_percent"
    process_metric = "lockbot_accelerator_process_info"
    entries = []
    try:
        for name in ("a", "b"):
            source_node = "host-shared" if same_source_node else f"host-{name}"
            card = f"card-{name}"
            bot_name = f"metrics-{name}"
            bot_id = (
                client.post(
                    "/api/bots",
                    headers=auth_header,
                    json={
                        "name": bot_name,
                        "bot_type": "DEVICE",
                        "cluster_configs": nodes,
                        "webhook_url": "https://example.invalid/hook",
                        "resource_api_notify_enabled": False,
                        "utilization_config": {
                            "endpoint": endpoint,
                            "profile": "lockbot-v1",
                            "accessMode": "short_lived",
                            "nodeMapping": {"node_1": source_node},
                            "deviceMapping": {"node_1:dev0": card},
                        },
                    },
                )
                .raise_for_status()
                .json()["id"]
            )
            manager.start_bot(
                bot_id,
                {
                    "BOT_TYPE": "DEVICE",
                    "BOT_NAME": bot_name,
                    "BOT_ID": bot_id,
                    "DATA_DIR": str(tmp_path / "bots"),
                    "CLUSTER_CONFIGS": nodes,
                },
            )
            challenge = issue(client)
            chat(bot_id, challenge, sender=f"user-{name}")
            headers = bearer(finish(client, challenge).raise_for_status().json()["credential"])
            (resource,) = client.get(API + "/resources", headers=headers).raise_for_status().json()["rows"]
            ref = resource["resource_ref"]
            assert "holding" not in resource and resource["metrics"] == "ready"
            metrics_url = API + f"/resources/{ref}/metrics"
            descriptor = client.get(metrics_url, headers=headers).raise_for_status().json()
            assert descriptor["source"]["endpoint"] == endpoint
            assert (descriptor["source"]["node"], descriptor["source"]["device"]) == (source_node, card)
            entries.append((bot_id, headers, metrics_url, source_node, card, descriptor["access"]["token"]))

        with session_factory() as db:
            issuer = ensure_instance_id(db)
            allowed = {db.get(Bot, bot_id).uuid: [node] for bot_id, _, _, node, _, _ in entries}
        timestamp = time.time()
        reader = Gateway(
            {"audience": endpoint, "issuers": {issuer: {"key_file": str(signing_key), "bots": allowed}}},
            clock=lambda: timestamp,
        )

        def upstream(request, *, timeout):
            query = parse_qs(request.data.decode())["query"][0]
            name = query.split("{", 1)[0]
            samples = [
                {"metric": {"__name__": name, "node": node, "device": card}, "value": [1234, "12"]}
                for _, _, _, node, card, _ in entries
                if f"^(?:{node})$" in query
            ]
            document = {"status": "success", "data": {"resultType": "vector", "result": samples}}
            return BytesIO(json.dumps(document).encode())

        reader.opener.open = Mock(side_effect=upstream)

        def query(node, token, name=metric):
            payload = urlencode({"query": f'{name}{{node=~"^(?:{node})$"}}'}).encode()
            return json.loads(reader.query("Bearer " + token, payload))["data"]["result"]

        for entry, other in (tuple(entries), tuple(reversed(entries))):
            _, headers, _, node, card, token = entry
            (series,) = query(node, token)
            assert (series["metric"]["node"], series["metric"]["device"]) == (node, card)
            (process,) = query(node, token, process_metric)
            assert (process["metric"]["__name__"], process["metric"]["device"]) == (process_metric, card)
            assert client.get(other[2], headers=headers).status_code == 404
            if not same_source_node:
                with pytest.raises(Rejected) as rejected:
                    query(other[3], token)
                assert rejected.value.status == 403

        for _, _, _, node, card, token in entries:
            assert [item["metric"]["device"] for item in query(node, token)] == [card]
        assert reader.opener.open.call_count == 4
    finally:
        manager.shutdown_all()


def test_v1_enrollment_is_closed_in_a_fresh_process():
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.pop("RESOURCE_AUTH_V1_ENROLLMENT_ENABLED", None)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from lockbot.backend.app.config import RESOURCE_AUTH_V1_ENROLLMENT_ENABLED; "
            "assert not RESOURCE_AUTH_V1_ENROLLMENT_ENABLED",
        ],
        env=env,
        check=True,
        timeout=10,
    )


def test_disable_v1_enrollment_preserves_existing_keys_and_v2_binding(
    client, auth_header, session_factory, monkeypatch
):
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    first_bot = make_bot(client, auth_header, "existing-v1")
    second_bot = make_bot(client, auth_header, "new-v2")
    bindings = "/api/resource-auth/bindings"
    recover = "/api/resource-auth/credential-challenges"
    existing = client.post(bindings).raise_for_status().json()
    chat(first_bot, existing)
    pending = client.post(bindings).raise_for_status().json()

    monkeypatch.setattr("lockbot.backend.app.config.RESOURCE_AUTH_V1_ENROLLMENT_ENABLED", False)
    refused = client.post(bindings)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "v1_enrollment_disabled"
    assert client.get(f"{bindings}/{pending['binding_id']}").json()["status"] == "expired"
    assert "v2" in chat(second_bot, pending)
    assert client.get("/api/agent/context", headers=bearer(pending["credential"])).status_code == 401

    for bot_id, expected_status in ((first_bot, "confirmed"), (second_bot, "unavailable")):
        challenge = client.post(recover).raise_for_status().json()
        chat(bot_id, challenge)
        result = client.get(f"{recover}/{challenge['challenge_id']}", headers=bearer(challenge["proof"])).json()
        assert result["status"] == expected_status
        has_original_key = result.get("credential") == existing["credential"]
        assert has_original_key is (expected_status == "confirmed")

    response = client.get("/api/agent/context", headers=bearer(existing["credential"]))
    assert response.status_code == 200 and response.json()["bot"]["id"] == first_bot
    v2 = issue(client)
    chat(second_bot, v2)
    assert finish(client, v2).json()["credential"].startswith("lbv2_")


def test_removed_resource_ref_cannot_follow_reused_name(client, auth_header, db_session):
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.resource_api.v2.identity import identities

    bot = db_session.get(Bot, make_bot(client, auth_header, "ref-test"))
    original = identities(db_session, bot)["node-23", -1].ref
    db_session.commit()
    assert identities(db_session, bot)["node-23", -1].ref == original
    bot.cluster_configs = '{"unrelated":"active"}'
    db_session.commit()
    bot.cluster_configs = '{"node-23":"new","unrelated":"active"}'
    db_session.commit()
    assert identities(db_session, bot)["node-23", -1].ref != original


@pytest.mark.parametrize("change", ["revoke", "replace-key"])
@pytest.mark.parametrize("scope", ["exact", "cross_group"])
def test_agent_v2_drops_inflight_resource_snapshot_when_access_changes(
    client, auth_header, session_factory, monkeypatch, tmp_path, change, scope
):
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.resource_api.v2 import resources as v2_resources
    from lockbot.backend.app.resource_api.v2.binding import PersonalCredential, PersonalGrant, _replace_key
    from sqlalchemy import select

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager()
    monkeypatch.setattr(v2_resources, "bot_manager", manager)
    bot_id = make_bot(client, auth_header, "inflight-read")
    manager.start_bot(
        bot_id,
        {
            "BOT_ID": bot_id,
            "BOT_TYPE": "NODE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["node-23"],
        },
    )
    try:
        challenge = issue(client)
        chat(bot_id, challenge)
        headers = bearer(finish(client, challenge).raise_for_status().json()["credential"])
        first = client.get(API + "/resources", headers=headers).raise_for_status().json()
        ref = first["rows"][0]["resource_ref"]
        original = v2_resources.project_rows

        def change_access_during_projection(*args, **kwargs):
            result = original(*args, **kwargs)
            with session_factory() as db:
                if change == "revoke":
                    grant = db.scalar(select(PersonalGrant).where(PersonalGrant.bot_id == bot_id))
                    db.delete(grant)
                else:
                    principal = db.scalar(select(PersonalCredential))
                    principal.generation += 1
                    _replace_key(principal, service._utcnow())
                db.commit()
            return result

        monkeypatch.setattr(v2_resources, "project_rows", change_access_during_projection)
        params = {"resource_ref": ref} if scope == "exact" else {}
        response = client.get(API + "/resources", headers=headers, params=params)
        if change == "replace-key":
            assert response.status_code == 401
        elif scope == "exact":
            assert response.status_code == 404
        else:
            assert response.raise_for_status().json()["rows"] == []
        assert ref not in response.text
    finally:
        manager.stop_bot(bot_id)


@pytest.mark.parametrize(
    "bot_type,unit",
    [
        ("QUEUE", "node"),
        ("QUEUE_DEVICE", "card"),
        ("NODE", "node"),
        ("DEVICE", "card"),
    ],
)
def test_lb_binary_uses_v2_end_to_end(
    client,
    auth_header,
    session_factory,
    monkeypatch,
    tmp_path,
    record_property,
    bot_type,
    unit,
):
    import json
    import os
    import subprocess
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from threading import Thread

    from lockbot.backend.app.bots.manager import BotManager

    cli = Path(os.environ.get("LOCKBOT_LB_BIN", "/__lb_cli_not_supplied__")).resolve()
    if not cli.is_file():
        pytest.skip("Set LOCKBOT_LB_BIN to the private lb executable.")
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager(max_inflight=5, drain_timeout=2)
    for module in ("v2.resources", "v2.commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    node = "node-23"
    nodes = {node: ["p800"]} if unit == "card" else [node]
    card_selection = ["--card", "0"] if unit == "card" else []
    result = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "queue-lab",
            "bot_type": bot_type,
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": nodes,
        },
    )
    assert result.status_code == 201, result.text
    bot_id = result.json()["id"]
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": bot_type,
            "BOT_NAME": "queue-lab",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": nodes,
        },
    )

    observed_routes = []

    class Bridge(BaseHTTPRequestHandler):
        def relay(self):
            observed_routes.append((self.command, self.path.split("?", 1)[0]))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower()
                in {
                    "authorization",
                    "content-type",
                    "idempotency-key",
                }
            }
            response = client.request(self.command, self.path, headers=headers, content=body)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = relay
        do_POST = relay

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def run(actor, *argv, as_json=True):
        env = {**os.environ, "LB_CONFIG_DIR": str(tmp_path / f"lb-{actor}"), "LB_PYTHON": sys.executable}
        command = [str(cli), *argv, *(["--json"] if as_json else [])]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if as_json else result.stdout

    try:
        queue_enabled = bot_type in {"QUEUE", "QUEUE_DEVICE"}
        actors = ("alice", "bob", "carol", "dana") if queue_enabled else ("alice", "bob")
        for actor in actors:
            challenge = run(actor, "login", origin)
            chat(bot_id, challenge, sender=actor)
            assert run(actor, "login")["status"] == "confirmed"
        page = run("alice", "resource", "list", "--available", unit)
        ref = page["rows"][0]["resource_ref"]
        checked = run("alice", "resource", "view", node)["rows"][0]
        assert (checked["resource_ref"], checked["unit"]) == (ref, unit)
        if unit == "card":
            (card,) = run("alice", "resource", "view", node, "--card", "0")["rows"]
            assert (card["resource_ref"], card["device"]) == (ref, 0)
        if not queue_enabled:
            assert set(checked["operations"]) == {"free", "lock", "slock"}
            for actor, duration in (("alice", "30m"), ("bob", "45m")):
                locked = run(actor, "lock", ref, duration, "--shared")["resources"][0]
                assert locked["holding"]["mode"] == "shared"
            shared = run("alice", "resource", "view", ref)["rows"][0]
            assert shared["status"] == "shared"
            assert [(person["name"], person["duration_seconds"]) for person in shared["holders"]] == [
                ("alice", 1800),
                ("bob", 2700),
            ]
            output = run("alice", "resource", "view", ref, "--people", as_json=False)
            record_property("resource_view", output)
            assert "alice (you), bob" in output and "EXPIRES AT" in output
            assert "30m" in output and "45m" in output
            run("alice", "free", ref)
            remaining = run("bob", "resource", "view", ref)["rows"][0]
            assert remaining["status"] == "shared" and [person["name"] for person in remaining["holders"]] == ["bob"]
            assert run("bob", "free", ref)["resources"][0]["status"] == "idle"
            return
        assert set(checked["operations"]) == {"book", "free", "lock"}
        observed_routes.clear()
        assert run("alice", "lock", ref, "30m")["resources"][0]["holding"]["mode"] == "exclusive"
        assert observed_routes == [("POST", API + "/commands")]
        assert run("alice", "resource", "list", "--mine")["rows"][0]["resource_ref"] == ref
        run("bob", "book", node, "20m", *card_selection)
        run("carol", "book", ref, "10m")
        run("dana", "book", ref, "15m")
        queued = run("alice", "resource", "view", ref)["rows"][0]["queue"]
        assert [(person["name"], person["position"], person["duration_seconds"]) for person in queued] == [
            ("bob", 1, 1200),
            ("carol", 2, 600),
            ("dana", 3, 900),
        ]
        assert [person["estimated_wait_seconds"] - queued[0]["estimated_wait_seconds"] for person in queued] == [
            0,
            1200,
            1800,
        ]
        output = run("alice", "resource", "view", ref, "--people", as_json=False)
        record_property("resource_view", output)
        assert all(name in output for name in ("bob", "carol", "dana")) and "EST. WAIT" in output
        assert "book, free, lock" in output and "book, claim" not in output
        assert run("bob", "resource", "wait")["rows"][0]["estimated_seconds"] > 0
        run("carol", "free", ref)
        run("dana", "free", ref)
        assert run("alice", "free", node, *card_selection)["resources"][0]["status"] == "idle"
        holding = run("bob", "lock", node, *card_selection)["resources"][0]["holding"]
        assert holding["mode"] == "exclusive"
        assert run("bob", "resource", "view", ref)["rows"][0]["holders"][0]["duration_seconds"] == 1200
        booking = run("carol", "book", ref, "10m", "--task-category", "code_review")["resources"][0]["booking_ref"]
        assert run("bob", "free", ref)["resources"][0]["status"] == "idle"
        locked_booking = run("carol", "lock", booking)
        assert locked_booking["operation"] == "lock"
        assert locked_booking["resources"][0]["holding"]["mode"] == "exclusive"
        holder = run("carol", "resource", "view", ref)["rows"][0]["holders"][0]
        assert (holder["duration_seconds"], holder["task_category"]) == (600, "code_review")
        assert run("carol", "free", ref)["resources"][0]["status"] == "idle"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        manager.shutdown_all()


def test_feishu_keys_are_scoped_to_the_confirming_application(client, auth_header, session_factory, monkeypatch):
    from lockbot.backend.app.bots.encryption import encrypt
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute

    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    ids = [make_bot(client, auth_header, "feishu-first"), make_bot(client, auth_header, "feishu-second")]
    groups = ["oc_first", "oc_second"]
    with session_factory() as db:
        for index, (bot_id, group_id) in enumerate(zip(ids, groups, strict=True)):
            bot = db.get(Bot, bot_id)
            bot.platform = "Feishu"
            bot.group_id = group_id
            app = FeishuApplication(
                app_id=f"cli_fixture_{index}",
                name=f"App {index}",
                tenant_key="tenant",
                bot_open_id=f"ou_bot_{index}",
                app_secret=encrypt("unused"),
                verification_token=encrypt("unused"),
                encrypt_key=encrypt(""),
            )
            db.add(app)
            db.flush()
            db.add(FeishuGroupRoute(application_id=app.id, bot_id=bot_id, chat_id=group_id))
        db.commit()

    def confirm(challenge, index):
        service.handle_chat_binding(
            platform="feishu",
            external_user_id="ou_same_fixture",
            bot_id=ids[index],
            chat_id=groups[index],
            action="bind",
            argument=challenge["bind_code"],
        )

    first = issue(client)
    confirm(first, 0)
    key1 = finish(client, first).json()["credential"]
    extra = issue(client, key1)
    confirm(extra, 1)
    assert finish(client, extra).json() == {"status": "pending"}
    own = client.get(API + "/bindings", headers=bearer(key1)).json()["grants"]
    assert [(item["name"], item["bot_id"], item["platform"], item["chat_id"]) for item in own] == [
        ("feishu-first", ids[0], "feishu", groups[0])
    ]

    second = issue(client)
    confirm(second, 1)
    key2 = finish(client, second).json()["credential"]
    assert key2 != key1


def test_v2_idle_recovery_notifies_and_confirms_exact_holding(
    client,
    auth_header,
    session_factory,
    monkeypatch,
    tmp_path,
):
    import asyncio
    import json
    import time
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    from fastapi import Request
    from lockbot.backend.app.bots.manager import BotManager
    from lockbot.backend.app.resource_api.idle_observation import IdleEvidence
    from lockbot.backend.app.resource_api.idle_recovery import IdleRecovery
    from lockbot.backend.app.resource_api.v2 import watch

    start = int(time.time())
    clock = start
    monkeypatch.setattr(time, "time", lambda: clock)
    monkeypatch.setattr(watch, "_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    manager = BotManager()
    for module in ("v2.resources", "v2.commands"):
        monkeypatch.setattr(f"lockbot.backend.app.resource_api.{module}.bot_manager", manager)
    recovery = IdleRecovery(manager, clock=lambda: clock)
    monkeypatch.setattr(client.app.state, "idle_observer", SimpleNamespace(recovery=recovery), raising=False)
    cluster = {"node-a": "demo", "node-b": "demo"}
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "idle-lab",
            "bot_type": "QUEUE",
            "cluster_configs": cluster,
            "webhook_url": "https://example.invalid/hook",
        },
    ).raise_for_status()
    bot_id = created.json()["id"]
    manager.start_bot(
        bot_id,
        {
            "BOT_ID": bot_id,
            "BOT_TYPE": "QUEUE",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": cluster,
        },
    )
    with manager.admit(bot_id) as lease:
        bot, generation = lease.bot, lease.generation

    def connect(actor):
        challenge = issue(client)
        chat(bot_id, challenge, sender=actor)
        return bearer(finish(client, challenge).json()["credential"])

    def post(headers, **operation):
        return client.post(API + "/commands", headers=headers, json=operation)

    @asynccontextmanager
    async def stream(headers):
        async def receive():
            await asyncio.Future()

        request = Request(
            {
                "type": "http",
                "app": client.app,
                "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            },
            receive=receive,
        )
        response = await watch.watch_resources(request)
        try:
            yield response.body_iterator
        finally:
            await response.body_iterator.aclose()
            await response.background()

    async def frame(events):
        message = await asyncio.wait_for(anext(events), 3)
        if message.startswith(":"):
            return "heartbeat", {}
        fields = dict(line.split(": ", 1) for line in message.strip().splitlines())
        return fields["event"], json.loads(fields["data"])

    async def exercise():
        nonlocal clock
        holder, other = connect("holder"), connect("other")
        grants = client.get(API + "/bindings", headers=holder).raise_for_status().json()["grants"]
        policy = grants[0]["low_utilization"]
        assert policy["enabled"] and policy["sampling_required"]
        assert policy["trigger_any"] == ["resource_has_waiting_user", "bot_has_no_available_resources"]
        assert (policy["observation_seconds"], policy["confirm_window_seconds"], policy["keep_extension_seconds"]) == (
            1200,
            600,
            600,
        )
        assert policy["overnight_pause"]["enabled"] is False
        resources = client.get(API + "/resources", headers=holder).raise_for_status().json()["rows"]
        refs = {row["node"]: row["resource_ref"] for row in resources}
        ref = refs["node-a"]
        post(holder, operation="lock", resource_refs=[ref], duration="1h").raise_for_status()
        post(other, operation="book", resource_refs=[ref], duration="10m").raise_for_status()

        key = (bot_id, "node-a", None)
        version = bot.op_holding_version(bot.published_snapshot().state, "node-a", None, "holder", at=clock)
        assert version is not None
        clock = start + 1200
        evidence = IdleEvidence(version, start, clock, "holder", "api", generation, 1.2, 5)
        recovery.reconcile(0, {key: evidence})
        async with stream(holder) as watched, stream(other) as unwatched:
            assert (await frame(watched))[0] == "snapshot"
            assert (await frame(unwatched))[0] == "snapshot"
            assert (await frame(unwatched))[0] == "heartbeat"
            event, warning = await frame(watched)
            assert (event, warning["resource_ref"], warning["confirm_within_seconds"]) == ("idle.notice", ref, 600)
            assert (warning["utilization_percent"], warning["threshold_percent"]) == (1.2, 5)
            assert warning["trigger"] == "resource_has_waiting_user"
            assert (warning["observation_seconds"], warning["keep_extension_seconds"]) == (1200, 600)
            assert (await frame(watched))[0] == "heartbeat"

            body = {"operation": "keep", "notice_ref": warning["notice_ref"]}
            assert post(holder, resource_refs=[ref], **{**body, "operation": "confirm_idle"}).status_code == 422
            assert post(holder, operation="keep", resource_refs=[ref]).status_code == 422
            assert post(other, resource_refs=[ref], **body).status_code == 409
            assert post(holder, resource_refs=[refs["node-b"]], **body).status_code == 409
            confirm = post(holder, resource_refs=[ref], **body).raise_for_status()
            assert (confirm.json()["operation"], confirm.json()["status"]) == ("keep", "completed")

            clock = start + 1800
            assert recovery.reclaim_ready({key: replace(evidence, observed_at=clock)}, 240) == []
            assert bot.published_snapshot().state["node-a"]["status"] == "exclusive"
            clock = start + 2400
            assert len(recovery.reclaim_ready({key: replace(evidence, observed_at=clock)}, 240)) == 1
            assert bot.published_snapshot().state["node-a"]["status"] == "idle"
            event, updated = await frame(watched)
            assert event == "resource.changed"
            assert updated["ended"][0]["kind"] == "holding_reclaimed"
            event, updated = await frame(unwatched)
            assert event in ("resource.changed", "heartbeat")
            assert "ended" not in updated

    try:
        asyncio.run(exercise())
    finally:
        manager.shutdown_all()
