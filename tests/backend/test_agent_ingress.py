import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from lockbot.backend.app.resource_api.ingress import AgentIngressMiddleware, IngressBudget


@pytest.mark.parametrize(
    "path",
    [
        "/api/bots/4/resources",
        "/api/agent/context",
        "/api/agent/resources",
        "/api/agent/resources/query",
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


def test_bearer_buckets_isolate_users_on_the_same_ip_without_trusting_identity():
    calls = []

    def auth():
        calls.append(1)
        raise HTTPException(401, "Invalid credential")

    app = FastAPI()
    budget = IngressBudget(source_limit=2, global_limit=4)
    app.add_middleware(AgentIngressMiddleware, budget=budget)

    @app.get("/api/agent/context", dependencies=[Depends(auth)])
    def endpoint():
        raise AssertionError("Cannot reach route")

    client = TestClient(app)
    first = {"Authorization": "Bearer fictional-one"}
    second = {"Authorization": "Bearer fictional-two"}
    assert client.get("/api/agent/context", headers=first).status_code == 401
    assert client.get("/api/agent/context", headers=first).status_code == 401
    assert client.get("/api/agent/context", headers=first).status_code == 429
    assert client.get("/api/agent/context", headers=second).status_code == 401
    assert client.get("/api/agent/context", headers=second).status_code == 401
    # Rotating invalid bearer strings cannot bypass the aggregate admission budget.
    assert client.get("/api/agent/context", headers={"Authorization": "Bearer third"}).status_code == 429
    assert len(calls) == 4
    assert all("fictional" not in key for key in budget.sources)
