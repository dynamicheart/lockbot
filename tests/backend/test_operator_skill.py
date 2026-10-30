import hashlib
import io
import zipfile
from pathlib import Path


def test_operator_skill_is_public_distinct_and_content_addressed(client):
    meta = client.get("/api/op-skill").json()
    text = client.get(meta["skill_url"])
    bundle = client.get(meta["download_url"])
    assert text.status_code == bundle.status_code == 200
    assert meta["name"] == "lockbot-operator-api"
    assert "public_agent_operation_history" in meta["capabilities"]
    assert meta["op_history_path_template"] == "/api/bots/{bot_id}/op/history"
    assert hashlib.sha256(bundle.content).hexdigest() == meta["sha256"]
    assert hashlib.sha256(text.content).hexdigest() == meta["skill_sha256"]
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as zipped:
        assert zipped.namelist() == ["lockbot-operator-api/SKILL.md"]
        assert zipped.read(zipped.namelist()[0]) == text.content
    assert text.content == (Path(__file__).parents[2] / "docs/op-api/SKILL.md").read_bytes()
    assert client.get(meta["download_url"], headers={"If-None-Match": bundle.headers["etag"]}).status_code == 304
    paths = client.get("/api/public-openapi.json").json()["paths"]
    assert paths[meta["skill_url"]]["get"]["security"] == []
    assert paths["/api/bots/{bot_id}/opkick"]["post"]["security"] == [{"OpApiKey": []}]
    assert client.get("/api/agent-skill").json()["name"] == "lockbot-agent-api"


def test_ordinary_skills_do_not_distribute_detailed_note_authorization(client):
    private_contracts = (
        b"audit-credentials",
        b"/api/audit/bots",
        b"TaskNoteAuditCredential",
        b"lba1",
        b"task_notes:read",
    )
    for prefix, bundles in (
        ("/api/agent-skill", ("lockbot-agent-api.zip", "lockbot-resource-agent.zip")),
        ("/api/op-skill", ("lockbot-operator-api.zip", "lockbot-operator-agent.zip")),
    ):
        markdown = client.get(prefix + "/SKILL.md")
        assert markdown.status_code == 200
        for marker in private_contracts:
            assert marker not in markdown.content
        for bundle in bundles:
            response = client.get(prefix + "/" + bundle)
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
                for name in zipped.namelist():
                    for marker in private_contracts:
                        assert marker not in zipped.read(name)


def test_public_resource_history_uses_operator_credentials_not_personal_keys(client):
    doc = client.get("/api/public-openapi.json").json()
    for suffix in ("history", "lease-history", "lifecycle-audit", "resources"):
        route = doc["paths"]["/api/bots/{bot_id}/op/" + suffix]["get"]
        assert route["tags"] == ["OP API"]
        assert route["security"] == [{"OpApiKey": []}]
    for path, method in (
        ("/api/bots/{bot_id}/resources", "get"),
        ("/api/bots/{bot_id}/commands", "post"),
        ("/api/agent/context", "get"),
    ):
        route = doc["paths"][path][method]
        assert route["tags"] == ["Agent API"]
        assert route["security"] == [{"AgentKey": []}]
