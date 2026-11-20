"""Normalized next steps retain actual request-stage and identity boundaries."""

# ruff: noqa: F811 -- imported pytest fixtures use parameter names.
import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest
from dev.test_skill_cli_eval import install, prepare, run
from lockbot.backend.app.resource_api.traffic import credential_budget

from backend.test_packaged_cli_auto_scope import command, no_post, sdk  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("fault", ["http504", "network", "unverified"])
def test_actual_write_attempt_has_readonly_unknown_followup_without_replay(sdk, fault):
    original = sdk.transport.open
    attempted = []

    def broken(request, timeout=None):
        if request.get_method() != "POST":
            return original(request, timeout)
        attempted.append(request.full_url)
        if fault == "network":
            raise URLError("synthetic transport loss before response")
        response = original(request, timeout)
        if fault == "http504":
            response.close()
            raise HTTPError(request.full_url, 504, "lost response", Message(), io.BytesIO())
        body = io.BytesIO(b"{}")
        body.headers = response.headers
        return body

    sdk.transport.open = broken
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert len(attempted) == 1
    assert output["command_outcome"] == "unknown" and output["mutation_attempted"] is True
    assert output["retry_mutation"] is False
    assert "无法确认" in output["message_zh"] and "不能证明" in output["message_zh"]
    assert "已核对" not in output["message_zh"]  # No follow-up read ran yet.
    assert output["next_command"] == ["resources-all", "--mine"]
    assert "Do not retry" in output["next_step"] and "idle or absent" in output["next_step"]
    assert output["resolution"]["selection_checked_before_post"] is True
    if fault == "network":
        assert "command_sent" not in output
        no_post(events)
    else:
        assert output["command_sent"] is True
        assert sum(x["method"] == "POST" for x in events) == 1
    _, reads = sdk.run(*output["next_command"])
    no_post(reads)
    assert len(attempted) == 1


def test_local_post_budget_exhaustion_is_not_unknown_or_sent(sdk, monkeypatch):
    original = sdk.module.Client._post_command

    def exhaust(client, *args):
        client.calls = sdk.module.MAX_CALLS
        return original(client, *args)

    monkeypatch.setattr(sdk.module.Client, "_post_command", exhaust)
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "request_budget_exhausted"
    assert output["command_sent"] is False
    assert "command_outcome" not in output and "mutation_attempted" not in output
    no_post(events)


def test_http_rejection_is_distinct_from_execution_or_unknown(sdk, monkeypatch):
    monkeypatch.setattr(credential_budget, "write_limit", 1)
    sdk.client.post(
        "/api/agent/commands", headers=sdk.api["headers"], json={"command": "lock n2 1h"}
    ).raise_for_status()
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "http_429" and output["command_outcome"] == "rejected"
    assert "message_zh" not in output
    assert output["command_sent"] is True and output["mutation_attempted"] is True
    assert output["retry_mutation"] is False
    assert sum(x["method"] == "POST" for x in events) == 1
    assert not sdk.bots[0].state.bot_state["n1"]["current_users"]


@pytest.mark.parametrize("fault", [None, "capability", "manifest-url", "context-url"])
def test_init_recovery_hint_requires_verified_capability_and_both_endpoints(sdk, fault):
    def transform(path, body):
        if path == "/api/agent-skill":
            if fault == "capability":
                body["capabilities"] = [x for x in body["capabilities"] if x != "own_binding_recovery"]
            elif fault == "manifest-url":
                body["binding_recovery_url"] = "https://unapproved.example/recover"
        elif path == "/api/agent/context" and fault == "context-url":
            body["links"]["binding_recovery"] = "https://unapproved.example/recover"

    # This fixture's transform wrapper covers manifests/resources; wrap context too.
    original = sdk.transport.open

    def transformed(request, timeout=None):
        response = original(request, timeout)
        if request.full_url.endswith("/api/agent/context") and fault == "context-url":
            body = json.loads(response.getvalue())
            transform("/api/agent/context", body)
            changed = io.BytesIO(json.dumps(body).encode())
            changed.headers = response.headers
            return changed
        return response

    sdk.transport.transform = transform
    sdk.transport.open = transformed
    config = json.loads(sdk.connections[0].read_text())
    key = (sdk.connections[0].parent / config["key_file"]).resolve()
    destination = sdk.catalog.parent / "new-seed.json"
    output, events = sdk.run(
        "init", "--origin", "http://testserver", "--key-file", str(key), "--destination", str(destination)
    )
    result = output["result"]
    assert result["connection_created"] is True and destination.is_file()
    if fault is None:
        assert result["next_command"] == ["--connection", str(destination), "recover", "--scope", "user"]
    else:
        assert "next_command" not in result
    no_post(events)  # Guidance never secretly performs binding recovery.


@pytest.mark.parametrize("fault", ["issue403", "issue504", "capability403", "context403", "identity"])
def test_panel_error_home_requires_fresh_verified_context_and_never_reissues(tmp_path, monkeypatch, fault):
    fixture = prepare(tmp_path, monkeypatch, "panel")
    assert install(fixture)["installed"]
    original = fixture.http
    posts = []

    def failed(args):
        url, method = args["url"], args.get("method", "GET")
        if method == "POST":
            posts.append(url)
            if fault in {"issue403", "issue504"}:
                return {"status": 403 if fault == "issue403" else 504}
        if url.endswith("/capabilities") and fault == "capability403":
            return {"status": 403}
        if url.endswith("/api/agent/context"):
            if fault == "context403":
                return {"status": 403}
            response = original(args)
            if fault == "identity":
                response["body"]["actor"]["external_user_id"] = "other-user"
            return response
        return original(args)

    monkeypatch.setattr(fixture, "http", failed)
    result = run(fixture, "panel-link")
    assert result["retry_mutation"] is False
    if fault.startswith("issue"):
        assert result["home"] == fixture.panel_home
        assert "Do not request another panel-link" in result["next_step"]
        assert len(posts) == 1
        assert "login_url" not in result
    else:
        assert "home" not in result and not posts
