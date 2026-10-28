from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from lockbot.backend.app.resource_api.ingress import AgentIngressMiddleware, IngressBudget


def test_invalid_credentials_are_limited_before_authentication():
    calls = []

    def auth():
        calls.append(1)
        raise HTTPException(401, "Invalid credential")

    app = FastAPI()
    app.add_middleware(AgentIngressMiddleware, budget=IngressBudget(source_limit=2))

    @app.get("/api/bots/4/resources", dependencies=[Depends(auth)])
    def endpoint():
        raise AssertionError("Cannot reach route")

    @app.get("/api/bots/webhook/4")
    def webhook():
        return {"untouched": True}

    client = TestClient(app)
    assert client.get("/api/bots/4/resources").status_code == 401
    assert client.get("/api/bots/4/resources").status_code == 401
    response = client.get("/api/bots/4/resources")
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
