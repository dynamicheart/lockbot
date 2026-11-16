"""Stage-6 public contract: the public OpenAPI must be a self-contained, leak-free closure."""

import json
import re


def test_public_openapi_is_a_resolvable_leak_free_closure(client):
    doc = client.get("/api/public-openapi.json").json()
    schemas = doc.get("components", {}).get("schemas", {})
    blob = json.dumps(doc)

    # Every local $ref anywhere in the public document resolves within its own components.
    refs = set(re.findall(r'"#/components/schemas/([^"/]+)"', blob))
    missing = refs - set(schemas)
    assert not missing, f"dangling $refs in public OpenAPI: {sorted(missing)}"

    # A nested model the old substring filter dropped (CommandResponse -> CommandActor) is now
    # pulled in by the transitive closure.
    assert "CommandActor" in schemas

    # The agent-facing command/resource routes are exposed...
    assert any(p.endswith("/commands") for p in doc["paths"]), sorted(doc["paths"])
    assert any(p.endswith("/resources") for p in doc["paths"]), sorted(doc["paths"])

    # ...but no management-only schema or path leaks into the public contract.
    assert "BotCreate" not in schemas
    assert not any(p == "/api/bots" for p in doc["paths"])


def test_public_doc_sections_partition_operations_and_authorization(client):
    combined = client.get("/api/public-openapi.json").raise_for_status().json()
    directory = client.get("/api/public-docs", headers={"Accept-Language": "en-US"}).raise_for_status().text
    assert "LockBot API documentation" in directory
    seen = set()
    for scope, key in (("agent", "AgentKey"), ("op", "OpApiKey"), ("configuration", "ConfigurationKey")):
        page = f"/api/public-docs/{scope}"
        assert f'href="{page}"' in directory
        url = f"/api/public-openapi.json?scope={scope}"
        assert url in client.get(page).raise_for_status().text
        schema = client.get(url).raise_for_status().json()
        assert set(schema["components"]["securitySchemes"]) == {key}
        operations = {(path, method) for path, methods in schema["paths"].items() for method in methods}
        assert operations and not seen & operations
        seen.update(operations)
        tags = {tag["name"] for tag in schema["tags"]}
        for path, method in operations:
            operation = schema["paths"][path][method]
            assert set(operation["tags"]) <= tags
            assert {k: v for k, v in operation.items() if k != "tags"} == {
                k: v for k, v in combined["paths"][path][method].items() if k != "tags"
            }
            assert operation["security"] in ([], [{key: []}])
            if not operation["security"]:
                assert scope == "agent" and path.startswith(("/api/agent-skill", "/api/resource-auth/"))
        refs = set(re.findall(r'"#/components/schemas/([^"/]+)"', json.dumps(schema)))
        assert refs <= schema["components"]["schemas"].keys()
    assert seen == {(path, method) for path, methods in combined["paths"].items() for method in methods}
    assert client.get("/api/public-docs/unknown").status_code == 404


def test_public_doc_sections_localize_group_names_and_credentials(client):
    scopes = (
        ("agent", "AgentKey", "Bind an Agent", "Agent 绑定", "activated through binding", "确认绑定"),
        ("op", "OpApiKey", "Operator API", "OP 运维 API", "trusted operators", "受信任运维"),
        ("configuration", "ConfigurationKey", "Configuration API", "机器人配置 API", "permissions", "配置权限"),
    )
    for scope, key, english_tag, chinese_tag, english_credential, chinese_credential in scopes:
        for lang, tag, description in (
            ("en", english_tag, english_credential),
            ("zh", chinese_tag, chinese_credential),
        ):
            headers = {"Accept-Language": lang}
            page = client.get(f"/api/public-docs/{scope}", headers=headers).raise_for_status()
            response = client.get(f"/api/public-openapi.json?scope={scope}", headers=headers).raise_for_status()
            assert page.headers["content-language"] == response.headers["content-language"] == lang
            assert "Accept-Language" in page.headers["vary"] and "Accept-Language" in response.headers["vary"]
            schema = response.json()
            assert tag in {section["name"] for section in schema["tags"]}
            assert description in schema["components"]["securitySchemes"][key]["description"]


def test_provisioning_operations_use_only_configuration_credentials(client):
    paths = ("/api/bot-provisioning/preview", "/api/bot-provisioning/apply")

    def provision():
        return {}

    # Exercise these routes even when the separate provisioning feature has not been merged.
    registered = client.app.openapi()["paths"]
    for path in paths:
        if path not in registered:
            client.app.add_api_route(path, provision, methods=["POST"], tags=["Public API"])
    client.app.openapi_schema = None

    for scope in ("agent", "op", "configuration"):
        schema = client.get("/api/public-openapi.json", params={"scope": scope}).raise_for_status().json()
        for path in paths:
            if scope == "configuration":
                assert schema["paths"][path]["post"]["security"] == [{"ConfigurationKey": []}]
            else:
                assert path not in schema["paths"]


def test_public_schema_uses_detailed_notes_wording_without_removing_personal_fields(client):
    for url in ("/api/public-openapi.json", "/openapi.json"):
        doc = client.get(url).json()
        encoded = json.dumps(doc).lower()
        assert "private task" not in encoded and "private note" not in encoded
        request = doc["components"]["schemas"]["CommandRequest"]["properties"]
        assert "purpose" in request and "agent_label" in request
        assert "Detailed Agent task note" in request["purpose"]["description"]
