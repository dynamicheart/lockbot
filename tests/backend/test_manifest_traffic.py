"""Public identity checks must accommodate independent CLI users behind one NAT."""

# ruff: noqa: F811 -- imported fixture names are injected by pytest

import pytest
from fastapi.testclient import TestClient
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.traffic import credential_budget

from backend.test_packaged_cli_traffic import ORIGIN, api, packaged_cli  # noqa: F401


@pytest.fixture
def isolated_route_limiter(monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "_limiter", FixedWindowRateLimiter(storage))
    yield storage
    storage.reset()


def test_independent_packaged_cli_users_share_ip_without_manifest_starvation(
    packaged_cli, api, tmp_path, isolated_route_limiter, monkeypatch
):
    run, first_connection, transport = packaged_cli
    other_key = tmp_path / "other.key"
    other_key.write_text(api["other_headers"]["Authorization"].removeprefix("Bearer "))
    other_key.chmod(0o600)
    second_connection = tmp_path / "other.json"
    run("init", "--origin", ORIGIN, "--key-file", str(other_key), "--destination", str(second_connection))
    credential_budget.reset()
    isolated_route_limiter.reset()
    start = len(transport.events)
    authentication = []
    original_open = transport.open

    def record_auth(request, timeout=None):
        authentication.append((request.full_url, bool(request.get_header("Authorization"))))
        return original_open(request, timeout)

    monkeypatch.setattr(transport, "open", record_auth)
    # Eighty anonymous identity checks from one transport IP, while neither
    # authenticated user has approached its 120-read allowance.
    for _ in range(40):
        for connection in (first_connection, second_connection):
            result, events = run("--connection", str(connection), "resources")
            assert result["result"]["resources"]
            assert [(event["path"], event["status"]) for event in events] == [
                ("/api/agent-skill", 200),
                ("/api/agent/resources", 200),
            ]
    assert len(transport.events[start:]) == 160

    # This adjustment must not merge authenticated allowances or remove them.
    for _ in range(80):
        run("--connection", str(first_connection), "resources")
    rejected, events = run("--connection", str(first_connection), "resources", expected_code=2)
    assert rejected["error"] == "http_429"
    assert [(event["path"], event["status"]) for event in events] == [
        ("/api/agent-skill", 200),
        ("/api/agent/resources", 429),
    ]
    assert run("--connection", str(second_connection), "resources")[0]["result"]["resources"]
    assert all(not authenticated for url, authenticated in authentication if url.endswith("/api/agent-skill"))
    assert all(authenticated for url, authenticated in authentication if url.endswith("/api/agent/resources"))


def test_public_manifest_keeps_a_finite_ip_bound_independent_of_zip(client, isolated_route_limiter):
    # Seed the real fixed-window counter, avoiding thousands of redundant HTTP
    # requests or a timing-sensitive wait for a minute boundary.
    assert limiter.limiter.hit(parse("6000/minute"), "testclient", "/api/agent-skill", cost=5999)
    response = client.get("/api/agent-skill")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert not {"actor", "bot", "credential"} & response.json().keys()
    assert client.get("/api/agent-skill").status_code == 429
    assert client.get("/api/agent-skill/lockbot-agent.zip").status_code == 200
    # A different transport source retains its own public allowance.
    other = TestClient(client.app, client=("198.51.100.2", 50000))
    try:
        assert other.get("/api/agent-skill").status_code == 200
    finally:
        other.close()


def test_zip_downloads_keep_the_lower_independent_ip_allowance(client, isolated_route_limiter):
    path = "/api/agent-skill/lockbot-agent.zip"
    assert limiter.limiter.hit(parse("60/minute"), "testclient", path, cost=59)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=300"
    assert client.get(path).status_code == 429
    assert client.get("/api/agent-skill").status_code == 200
