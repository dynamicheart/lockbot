import asyncio

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from lockbot.backend.app.resource_api.ingress import V2_RESOURCE_LIST_PATH, AgentIngressMiddleware, IngressBudget


@pytest.mark.parametrize(
    "path",
    [
        "/api/bots/4/resources",
        "/api/agent/context",
        "/api/agent/resources",
        "/api/agent/commands",
        "/api/agent/usage-declarations",
        "/api/agent/usage-declarations/n1",
    ],
)
def test_invalid_credentials_are_limited_before_authentication(path):
    calls = []

    def auth():
        calls.append(1)
        raise HTTPException(401, "Invalid credential")

    app = FastAPI()
    app.add_middleware(AgentIngressMiddleware, budget=IngressBudget(source_limit=2))

    @app.get(path, dependencies=[Depends(auth)])
    def endpoint():
        raise AssertionError("Cannot reach route")

    @app.get("/api/bots/webhook/4")
    def webhook():
        return {"untouched": True}

    client = TestClient(app)
    assert client.get(path).status_code == 401
    assert client.get(path).status_code == 401
    response = client.get(path)
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    assert len(calls) == 2
    assert client.get("/api/bots/webhook/4").status_code == 200


def test_source_limit_global_limit_expiry_and_memory_cap():
    budget = IngressBudget(source_limit=2, global_limit=3, max_sources=2)
    assert budget.check("a", now=0)[0]
    assert budget.check("a", now=0)[0]
    assert not budget.check("a", now=0)[0]
    assert budget.check("b", now=0)[0]
    assert not budget.check("b", now=0)[0]
    assert not budget.check("c", now=0)[0]
    assert len(budget.sources) == 2
    assert budget.check("c", now=61)[0]
    assert len(budget.sources) == 1


def test_v2_resource_list_has_separate_capacity_but_shares_global_protection():
    budget = IngressBudget(source_limit=1, global_limit=4, resource_list_source_limit=3)
    assert [budget.check("office", now=0, resource_list=True)[0] for _ in range(4)] == [True, True, True, False]
    assert budget.check("office", now=0)[0]
    assert not budget.check("other-office", now=0, resource_list=True)[0]


def test_read_flood_preserves_shared_ingress_tokens_for_commands():
    budget = IngressBudget(global_limit=20)
    for _ in range(18):
        assert budget.check("office", now=0, resource_list=True, protect_writes=True)[0]
    assert not budget.check("office", now=0, resource_list=True, protect_writes=True)[0]
    assert budget.check("office", now=0)[0]
    assert budget.check("office", now=0)[0]


def test_snapshot_sources_do_not_consume_the_command_source_bucket():
    budget = IngressBudget(source_limit=2)
    assert budget.check("office", now=0, protect_writes=True)[0]
    assert budget.check("office", now=0, protect_writes=True)[0]
    assert not budget.check("office", now=0, protect_writes=True)[0]
    assert budget.check("office", now=0)[0]


def test_v2_key_cannot_spend_other_readers_or_writers_tokens():
    budget = IngressBudget(global_limit=20, resource_list_source_limit=20)
    options = {"resource_list": True, "protect_writes": True}
    for _ in range(5):
        assert budget.check("office", now=0, read_key=b"busy", **options)[0]

    before = budget.total
    for _ in range(100):
        assert not budget.check("another-office", now=0, read_key=b"busy", **options)[0]
    assert budget.total == before
    assert ("list", "another-office") not in budget.sources
    assert budget.check("office", now=0, read_key=b"normal", **options)[0]
    assert budget.check("office", now=0)[0]
    assert budget.check("another-office", now=61, read_key=b"busy", **options)[0]


def test_one_v2_key_cannot_fill_both_query_lanes():
    async def scenario():
        app = FastAPI()
        app.add_middleware(AgentIngressMiddleware)
        busy = {"Authorization": "Bearer lbv2_busy"}
        normal = {"Authorization": "Bearer lbv2_normal"}
        entered = asyncio.Event()
        resume = asyncio.Event()
        count = 0

        @app.get(V2_RESOURCE_LIST_PATH)
        async def resources(request: Request):
            nonlocal count
            if request.headers.get("Authorization") == busy["Authorization"] and not resume.is_set():
                count += 1
                if count == 2:
                    entered.set()
                await resume.wait()
            return {"ok": True}

        @app.post("/api/agent/v2/commands")
        async def commands():
            return {"ok": True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            requests = [
                asyncio.create_task(client.get(V2_RESOURCE_LIST_PATH, headers=busy)),
                asyncio.create_task(
                    client.get(V2_RESOURCE_LIST_PATH, headers=busy, params={"resource_ref": "fixture"})
                ),
            ]
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                blocked = await client.get(V2_RESOURCE_LIST_PATH, headers=busy)
                assert blocked.status_code == 503
                assert blocked.json()["detail"]["code"] == "resource_query_busy"
                assert (await client.get(V2_RESOURCE_LIST_PATH, headers=normal)).status_code == 200
                assert (await client.post("/api/agent/v2/commands", headers=busy)).status_code == 200
            finally:
                resume.set()
                responses = await asyncio.gather(*requests)
            assert all(response.status_code == 200 for response in responses)
            assert (await client.get(V2_RESOURCE_LIST_PATH, headers=busy)).status_code == 200

    asyncio.run(scenario())
