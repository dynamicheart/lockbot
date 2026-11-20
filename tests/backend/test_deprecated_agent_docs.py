"""Legacy Agent operations have an explicit migration-only public document."""

import json
import re

import pytest


@pytest.mark.parametrize("language", ["zh", "en"])
def test_legacy_agent_document_is_scoped_deprecated_and_resolvable(client, language):
    response = client.get(
        "/api/public-openapi.json", params={"scope": "deprecated_agent"}, headers={"Accept-Language": language}
    ).raise_for_status()
    document = response.json()
    assert response.headers["content-language"] == language
    expected = {
        ("/api/bots/{bot_id}/resources", "get"),
        ("/api/bots/{bot_id}/commands", "post"),
        ("/api/bots/{bot_id}/usage-declarations", "get"),
        ("/api/bots/{bot_id}/usage-declarations/{resource_id}", "put"),
        ("/api/bots/{bot_id}/usage-declarations/{resource_id}", "delete"),
    }
    operations = {(path, method) for path, methods in document["paths"].items() for method in methods}
    assert operations == expected
    assert set(document["components"]["securitySchemes"]) == {"AgentKey"}
    assert len(document["tags"]) == 1
    tag = document["tags"][0]["name"]
    assert ("已弃用" if language == "zh" else "deprecated") in tag
    assert "BotCreate" not in document["components"]["schemas"]
    references = set(re.findall(r'"#/components/schemas/([^"/]+)"', json.dumps(document)))
    assert references <= document["components"]["schemas"].keys()
    current = client.get("/api/public-openapi.json", params={"scope": "agent"}).raise_for_status().json()
    combined = client.get("/api/public-openapi.json").raise_for_status().json()
    for path, method in expected:
        operation = document["paths"][path][method]
        replacement = path.replace("/api/bots/{bot_id}", "/api/agent", 1)
        assert operation["deprecated"] is True
        assert operation["security"] == [{"AgentKey": []}]
        assert operation["tags"] == [tag]
        assert operation["x-replacement-operation"] == {"method": method.upper(), "path": replacement}
        assert f"{method.upper()} {replacement}" in operation["description"]
        assert method in current["paths"][replacement]
        assert path not in current["paths"] and path not in combined["paths"]


def test_directory_and_current_docs_link_to_the_legacy_document(client):
    target = "/api/public-docs/deprecated_agent"
    for language, title in (("zh", "旧版 Agent API（已弃用）"), ("en", "Legacy Agent API (deprecated)")):
        headers = {"Accept-Language": language}
        directory = client.get("/api/public-docs", headers=headers).raise_for_status().text
        current = client.get("/api/public-docs/agent", headers=headers).raise_for_status().text
        legacy = client.get(target, headers=headers).raise_for_status().text
        assert f'href="{target}"' in directory and title in directory
        assert f'href="{target}"' in current
        assert 'href="/api/public-docs/agent"' in legacy
        assert "/api/public-openapi.json?scope=deprecated_agent" in legacy


def test_deprecated_directory_does_not_admit_unrelated_legacy_routes(client):
    def deprecated_operator():
        return {}

    path = "/api/bots/{bot_id}/op/legacy-debug"
    client.app.add_api_route(path, deprecated_operator, methods=["GET"], tags=["Public API"], deprecated=True)
    client.app.openapi_schema = None
    document = client.get("/api/public-openapi.json?scope=deprecated_agent").raise_for_status().json()
    assert path not in document["paths"]
