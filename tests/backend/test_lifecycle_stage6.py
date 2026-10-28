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
