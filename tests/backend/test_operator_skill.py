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
