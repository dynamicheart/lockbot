"""Stage-5 per-credential traffic budget.

The budget is a per-credential (per durable binding) sliding window, separate from the per-IP
limiter and the per-bot admission cap. Unit tests pin the window logic; route tests confirm the
``/commands`` (write) and ``/resources`` (read) endpoints enforce it with a 429 + Retry-After.
"""

# ruff: noqa: F811 -- the imported `api` pytest fixture is injected by parameter name

from lockbot.backend.app.resource_api.traffic import (
    _CredentialBudget,
    _ResourceTierGate,
    credential_budget,
    resource_tier_gate,
)

from backend.test_resource_api_resources import api  # noqa: F401


def test_read_and_write_budgets_are_separate_and_per_credential():
    b = _CredentialBudget(read_limit=2, write_limit=1)
    assert b.check(1, "read")[0] is True
    assert b.check(1, "read")[0] is True
    ok, retry = b.check(1, "read")
    assert ok is False and retry >= 1
    # the write bucket is independent of the read bucket
    assert b.check(1, "write")[0] is True
    assert b.check(1, "write")[0] is False
    # a different credential has its own budget
    assert b.check(2, "read")[0] is True


def test_budget_window_rolls_off():
    b = _CredentialBudget(read_limit=1, write_limit=1, window=60)
    assert b.check(1, "read", now=1000.0)[0] is True
    assert b.check(1, "read", now=1030.0)[0] is False  # still inside the window
    assert b.check(1, "read", now=1061.0)[0] is True  # the first hit has aged out


def test_commands_route_enforces_write_budget(client, api):
    credential_budget.write_limit = 2
    url = f"/api/bots/{api['bot_id']}/commands"
    assert client.post(url, headers=api["headers"], json={"command": "query"}).status_code == 200
    assert client.post(url, headers=api["headers"], json={"command": "query"}).status_code == 200
    third = client.post(url, headers=api["headers"], json={"command": "query"})
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "rate_limited"
    assert third.headers["Retry-After"]


def test_resources_route_enforces_read_budget(client, api):
    credential_budget.read_limit = 2
    url = f"/api/bots/{api['bot_id']}/resources"
    assert client.get(url, headers=api["headers"]).status_code == 200
    assert client.get(url, headers=api["headers"]).status_code == 200
    third = client.get(url, headers=api["headers"])
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "rate_limited"
    assert third.headers["Retry-After"]


def test_resource_tier_gate_floors_by_stake():
    g = _ResourceTierGate(active=10, booking=20, discovery=40)
    assert g.floor_for("own_lock_expiry") == 10
    assert g.floor_for("node_frees") == 10  # head-of-queue counts as active
    assert g.floor_for("queue_estimate") == 20
    assert g.floor_for(None) == 40
    # First poll is always allowed; an immediate re-poll is shed for the recorded tier floor.
    assert g.check(7, now=100.0) == (True, 0)
    g.record(7, "own_lock_expiry", now=100.0)  # active floor = 10s
    ok, retry = g.check(7, now=105.0)
    assert ok is False and retry >= 1
    assert g.check(7, now=111.0)[0] is True  # the floor has elapsed


def test_resources_route_enforces_poll_tier(client, api):
    resource_tier_gate.discovery = 40
    url = f"/api/bots/{api['bot_id']}/resources"
    assert client.get(url, headers=api["headers"]).status_code == 200  # first poll allowed
    second = client.get(url, headers=api["headers"])  # immediate re-poll, discovery tier
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "rate_limited"
    assert second.headers["Retry-After"]


def test_commands_route_enforces_identity_holdings_cap(client, api, monkeypatch):
    import lockbot.backend.app.resource_api.commands as commands_mod

    monkeypatch.setattr(commands_mod, "RESOURCE_API_MAX_HELD_PER_IDENTITY", 0)
    resp = client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": "lock n1 1h"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "identity_cap_reached"
