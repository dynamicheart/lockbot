"""HTTP monitoring counts real responses without retaining request secrets or growing unbounded."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.service_traffic import SOURCES, ServiceTrafficMiddleware, TrafficStats, source_for
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient


def test_totals_statuses_bytes_and_histogram_quantile():
    stats = TrafficStats(clock=lambda: 120)
    stats.record(
        source="agent", route="GET /api/bots/{bot_id}/resources", status=200, duration_ms=8, received=10, sent=20
    )
    stats.record(
        source="operator", route="POST /api/bots/{bot_id}/opkick", status=403, duration_ms=100, received=30, sent=40
    )
    stats.record(source="agent", route="GET /api/bots/{bot_id}/resources", status=500, duration_ms=15000, failed=True)
    body = stats.snapshot()
    assert body["totals"]["requests"] == 3
    assert body["totals"]["statuses"]["2xx"] == 1
    assert body["totals"]["client_errors"] == body["totals"]["server_errors"] == 1
    assert body["totals"]["exceptions"] == 1
    assert body["totals"]["request_bytes"] == 40 and body["totals"]["response_bytes"] == 60
    assert body["totals"]["p95_ms"] == 15000
    assert body["last_minute"]["requests"] == 3
    assert len(body["history"]) == 60
    json.dumps(body, allow_nan=False)


def test_retention_and_route_cardinality_are_bounded():
    now = [120.0]
    stats = TrafficStats(clock=lambda: now[0])
    for index in range(200):
        stats.record(source="unknown", route=f"GET /registered/{index}", status=200, duration_ms=1)
        now[0] += 61
    assert len(stats.minutes) <= 60 and len(stats.seconds) <= 60
    assert len(stats.routes) <= 101 and set(stats.sources) == set(SOURCES)
    assert stats.sources["other"].requests == 200
    now[0] += 3600
    body = stats.snapshot()
    assert body["totals"]["requests"] == 200
    assert body["last_minute"]["requests"] == 0
    assert all(row["requests"] == 0 for row in body["history"])
    assert len(body["routes"]) <= 50
    assert TrafficStats(clock=lambda: now[0]).snapshot()["totals"]["requests"] == 0


def test_parallel_requests_do_not_lose_counts():
    stats = TrafficStats()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda n: stats.record(source="agent", route="GET /known", status=200, duration_ms=n % 100), range(1000)
            )
        )
    body = stats.snapshot()
    assert body["totals"]["requests"] == 1000
    assert sum(row["requests"] for row in body["sources"]) == 1000
    assert sum(row["requests"] for row in body["history"]) == 1000


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("/api/bots/1/resources", "agent"),
        ("/api/bots/1/op/resources", "operator"),
        ("/api/bots/1/opkick", "operator"),
        ("/api/bots/1/kicklock", "operator"),
        ("/api/op-skill/SKILL.md", "operator"),
        ("/api/resource-auth/bindings", "enrollment"),
        ("/api/bots/webhook/1", "webhook"),
        ("/api/bot-views/1/state", "resource_view"),
        ("/api/admin/users", "management"),
        ("/api/admin/service-traffic", "monitoring"),
        ("/assets/any-file.js", "static"),
        ("/api/health", "health"),
    ],
)
def test_sources_have_fixed_names(path, source):
    assert source_for(path) == source


def test_streaming_bodies_and_template_paths_are_observed_without_content():
    stats = TrafficStats()
    app = FastAPI()
    app.add_middleware(ServiceTrafficMiddleware, stats=stats)

    @app.post("/api/example/{value}")
    async def stream(value: str, request: Request):
        await request.body()

        async def content():
            yield b"hello"
            yield b"world"

        return StreamingResponse(content(), status_code=201)

    with TestClient(app) as client:
        response = client.post(
            "/api/example/lbr_secret-path?view=lbv1.secret.code",
            headers={"Authorization": "Bearer confidential-header"},
            content=b"private-body",
        )
    assert response.content == b"helloworld"
    body = stats.snapshot()
    assert body["totals"]["request_bytes"] == len(b"private-body")
    assert body["totals"]["response_bytes"] == 10
    assert body["routes"][0]["route"] == "POST /api/example/{value}"
    encoded = json.dumps(body)
    for secret in ("lbr_secret-path", "lbv1.secret.code", "confidential-header", "private-body"):
        assert secret not in encoded


def test_unmatched_paths_and_exceptions_are_aggregated():
    stats = TrafficStats()
    app = FastAPI()
    app.add_middleware(ServiceTrafficMiddleware, stats=stats)

    @app.get("/api/failure")
    async def failure():
        raise RuntimeError("test-only")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/unknown/lbr_private-value").status_code == 404
        assert client.get("/api/failure").status_code == 500
    body = stats.snapshot()
    assert body["totals"]["requests"] == 2 and body["totals"]["server_errors"] == 1
    assert body["totals"]["exceptions"] == 1
    assert "lbr_private-value" not in json.dumps(body)


def test_non_http_transports_are_not_counted():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    stats = TrafficStats()
    asyncio.run(ServiceTrafficMiddleware(app, stats)({"type": "websocket"}, None, None))
    assert called == ["websocket"] and stats.snapshot()["totals"]["requests"] == 0


def test_global_monitor_requires_current_super_admin(client, admin_header, auth_header, db_session):
    endpoint = "/api/admin/service-traffic"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=auth_header).status_code == 403
    assert client.get(endpoint, headers=admin_header).status_code == 403
    assert client.get(endpoint, headers={"Authorization": "Bearer lbk_wrong-scope"}).status_code == 401
    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    response = client.get(endpoint, headers=admin_header)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert len(response.json()["history"]) == 60
    assert response.json()["totals"]["requests"] >= 4
    admin.role = "admin"
    db_session.commit()
    assert client.get(endpoint, headers=admin_header).status_code == 403
