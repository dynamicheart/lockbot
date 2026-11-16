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
        for path, method in operations:
            operation = schema["paths"][path][method]
            assert operation == combined["paths"][path][method]
            assert operation["security"] in ([], [{key: []}])
            if not operation["security"]:
                prefix = ("/api/agent-skill", "/api/resource-auth/") if scope == "agent" else ("/api/op-skill",)
                assert scope != "configuration" and path.startswith(prefix)
        refs = set(re.findall(r'"#/components/schemas/([^"/]+)"', json.dumps(schema)))
        assert refs <= schema["components"]["schemas"].keys()
    assert seen == {(path, method) for path, methods in combined["paths"].items() for method in methods}
    assert client.get("/api/public-docs/unknown").status_code == 404


def test_public_schema_uses_detailed_notes_wording_without_removing_personal_fields(client):
    for url in ("/api/public-openapi.json", "/openapi.json"):
        doc = client.get(url).json()
        encoded = json.dumps(doc).lower()
        assert "private task" not in encoded and "private note" not in encoded
        request = doc["components"]["schemas"]["CommandRequest"]["properties"]
        assert "purpose" in request and "agent_label" in request
        assert "Detailed Agent task note" in request["purpose"]["description"]
