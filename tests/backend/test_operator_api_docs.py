# ruff: noqa: F811 -- imported pytest fixtures
import io
import json
import zipfile

import pytest

from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_list_metrics import listing, metrics_source, prepare  # noqa: F401


def test_operator_documentation_does_not_offer_a_skill_download(client):
    obsolete = (
        "/api/op-skill",
        "/api/op-skill/SKILL.md",
        "/api/op-skill/lockbot-operator-api.zip",
        "/api/op-skill/lockbot-operator-agent.zip",
    )
    for path in obsolete:
        assert client.get(path).status_code == 404
    assert client.get("/api/public-docs/op").status_code == 200
    paths = client.get("/api/public-openapi.json", params={"scope": "op"}).json()["paths"]
    assert all(not path.startswith("/api/op-skill") for path in paths)
    assert paths["/api/bots/{bot_id}/opkick"]["post"]["security"] == [{"OpApiKey": []}]


def test_resource_skill_does_not_distribute_detailed_note_authorization(client):
    private_contracts = (
        b"audit-credentials",
        b"/api/audit/bots",
        b"TaskNoteAuditCredential",
        b"task_notes:read",
    )
    response = client.get("/api/agent-skill/lockbot-agent.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
        for name in zipped.namelist():
            content = zipped.read(name)
            for marker in private_contracts:
                assert marker not in content
            # The SDK recognizes this prefix only to refuse credential forwarding.
            # It must still be absent from instructions and other packaged assets.
            if name != "lockbot-agent/scripts/lockbot_cli.py":
                assert b"lba1" not in content


@pytest.mark.parametrize("credential", ["lba1.synthetic.private", "lbk_" + "K" * 43])
def test_resource_skill_refuses_privileged_credentials_for_metrics(aggregate, metrics_source, credential):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    state.extra["context"]["utilization"]["access"]["token"] = credential
    # Even a source that would accept the credential must never receive it.
    metrics_source.tokens.add("Bearer " + credential)
    result = listing(aggregate)
    assert metrics_source.events == []
    assert result["scope_complete"] and not result["metrics_read_complete"]
    assert "resource_key_for_metrics_refused" in result["metrics_unread_reasons"]
    assert result["rows"][0]["states"]["idle"] == 1
    assert credential not in json.dumps(result)
    assert len(state.events) == 2
    assert all(method == "GET" for _path, method, _auth in state.events)


def test_resource_skill_allows_scoped_metrics_grants(aggregate, metrics_source):
    state, _path, _mapping = prepare(aggregate, metrics_source)
    credential = state.extra["context"]["utilization"]["access"]["token"]
    result = listing(aggregate)
    assert result["scope_complete"] and result["metrics_read_complete"]
    assert result["metrics_unread_reasons"] == []
    assert result["rows"][0]["metrics"]["cpu"]["percent"] == 0
    assert len(metrics_source.events) == 1
    assert metrics_source.events[0]["authorization"] == "Bearer " + credential
    assert state.key not in str(metrics_source.events)
    assert credential not in json.dumps(result)


def test_public_resource_history_uses_operator_credentials_not_personal_keys(client):
    doc = client.get("/api/public-openapi.json").json()
    for suffix in ("history", "lease-history", "lifecycle-audit", "resources"):
        route = doc["paths"]["/api/bots/{bot_id}/op/" + suffix]["get"]
        assert route["tags"] == ["OP API"]
        assert route["security"] == [{"OpApiKey": []}]
    for path, method in (
        ("/api/agent/resources", "get"),
        ("/api/agent/context", "get"),
    ):
        route = doc["paths"][path][method]
        assert route["tags"] == ["Agent API"]
        assert route["security"] == [{"AgentKey": []}]
    assert "/api/bots/{bot_id}/resources" not in doc["paths"]
    assert "/api/bots/{bot_id}/commands" not in doc["paths"]
