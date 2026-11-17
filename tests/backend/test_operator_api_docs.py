import io
import zipfile


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
        b"lba1",
        b"task_notes:read",
    )
    response = client.get("/api/agent-skill/lockbot-agent.zip")
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
        ("/api/agent/resources", "get"),
        ("/api/agent/context", "get"),
    ):
        route = doc["paths"][path][method]
        assert route["tags"] == ["Agent API"]
        assert route["security"] == [{"AgentKey": []}]
    assert "/api/bots/{bot_id}/resources" not in doc["paths"]
    assert "/api/bots/{bot_id}/commands" not in doc["paths"]
