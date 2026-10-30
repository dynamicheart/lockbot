"""Authenticated snapshots must not persist in browser or intermediary HTTP caches."""

# ruff: noqa: F811

import pytest

from backend.test_operator_api import operator  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("kind", ["agent", "operator"])
def test_resource_snapshot_success_is_not_http_cacheable(client, operator, kind):
    suffix = "/resources" if kind == "agent" else "/op/resources"
    headers = operator["headers"] if kind == "agent" else operator["op_headers"]
    response = client.get(f"/api/bots/{operator['bot_id']}" + suffix, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["resources"]
    assert response.json()["age_seconds"] >= 0


@pytest.mark.parametrize("suffix", ["/resources", "/op/resources"])
def test_resource_snapshot_auth_and_validation_errors_are_not_http_cacheable(client, operator, suffix):
    response = client.get(f"/api/bots/{operator['bot_id']}" + suffix)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    response = client.get("/api/bots/not-an-id" + suffix, headers=operator["op_headers"])
    assert response.status_code in {401, 422}
    assert response.headers["cache-control"] == "no-store"
    if suffix.startswith("/op/"):
        response = client.get(
            f"/api/bots/{operator['bot_id']}" + suffix + "?usage_limit=0", headers=operator["op_headers"]
        )
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", ["agent", "operator"])
def test_resource_snapshot_stopped_bot_is_not_http_cacheable(client, operator, kind):
    operator["manager"].stop_bot(operator["bot_id"])
    suffix = "/resources" if kind == "agent" else "/op/resources"
    headers = operator["headers"] if kind == "agent" else operator["op_headers"]
    response = client.get(f"/api/bots/{operator['bot_id']}" + suffix, headers=headers)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_resource_snapshot_rate_limit_remains_retryable_but_not_cacheable(client, operator, monkeypatch):
    monkeypatch.setattr("lockbot.backend.app.resource_api.resources.credential_budget.check", lambda *args: (False, 7))
    response = client.get(f"/api/bots/{operator['bot_id']}/resources", headers=operator["headers"])
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "7"
