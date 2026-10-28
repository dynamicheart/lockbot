"""Scoped telemetry is attributed after authentication, with bounded private history reads."""

# ruff: noqa: F811

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.resource_api import event_log
from lockbot.backend.app.service_traffic import TrafficStats

from backend.test_bots import _sample_bot
from backend.test_resource_api_resources import api  # noqa: F401


def hit(stats, bot_id, credential_id="shared", user="alice", **kwargs):
    stats.record(
        source="agent",
        route="GET /api/bots/{bot_id}/resources",
        status=kwargs.get("status", 200),
        duration_ms=3,
        bot_id=bot_id,
        credential={"id": credential_id, "kind": "agent", "user": user, "platform": "infoflow"},
    )


def test_scoped_counts_ranking_and_lru_bounds():
    stats = TrafficStats(clock=lambda: 120)
    hit(stats, 1)
    hit(stats, 2, user="bob")
    hit(stats, 2, user="bob", status=429)
    scoped = stats.snapshot(1)
    assert scoped["totals"]["requests"] == 1
    assert [(row["bot_id"], row["requests"]) for row in stats.snapshot()["credentials"]] == [(2, 2), (1, 1)]
    assert "bob" not in json.dumps(scoped["credentials"])
    for index in range(1300):
        hit(stats, index + 10, str(index))
    assert len(stats.bots) == 256 and len(stats.credentials) == 256
    assert stats.snapshot(1)["credentials"] == []
    for index in range(1100):
        hit(stats, 1309, f"extra-{index}")
    assert len(stats.credentials) == 1024
    assert stats.snapshot(1)["totals"]["requests"] == 0
    assert len(stats.snapshot()["credentials"]) == 50
    assert stats.snapshot()["totals"]["requests"] == 2403


def test_concurrent_scoped_updates_and_restart():
    stats = TrafficStats()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hit(stats, 1), range(1000)))
    assert stats.snapshot(1)["totals"]["requests"] == 1000
    assert stats.snapshot()["credentials"][0]["requests"] == 1000
    assert TrafficStats().snapshot(1)["totals"]["requests"] == 0


def test_bot_and_global_permissions_do_not_leak_other_scopes(client, auth_header, admin_header, db_session):
    own = client.post("/api/bots", headers=auth_header, json=_sample_bot("mine")).json()["id"]
    other = client.post("/api/bots", headers=admin_header, json=_sample_bot("other")).json()["id"]
    stats = client.app.state.service_traffic
    hit(stats, own, "mine")
    hit(stats, other, "other", "private-other-user")
    assert client.get(f"/api/bots/{own}/service-traffic").status_code == 401
    response = client.get(f"/api/bots/{own}/service-traffic", headers=auth_header)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "private-other-user" not in response.text and "bot_options" not in response.json()
    assert client.get(f"/api/bots/{other}/service-traffic", headers=auth_header).status_code == 404
    assert client.get(f"/api/bots/{other}/agent-history", headers=auth_header).status_code == 404
    assert client.get("/api/admin/service-traffic", headers=admin_header).status_code == 403
    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    filtered = client.get("/api/admin/service-traffic", params={"bot_id": own}, headers=admin_header)
    assert filtered.status_code == 200
    assert all(row["bot_id"] == own for row in filtered.json()["credentials"])


def test_invalid_agent_key_is_not_attributed_to_asserted_bot(client, api):
    stats = client.app.state.service_traffic
    before = stats.snapshot(api["bot_id"])["totals"]["requests"]
    response = client.get(f"/api/bots/{api['bot_id']}/resources", headers={"Authorization": "Bearer lbr_made_up"})
    assert response.status_code == 401
    assert stats.snapshot(api["bot_id"])["totals"]["requests"] == before
    assert stats.snapshot()["unattributed_requests"] > 0
    client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"])
    snapshot = stats.snapshot(api["bot_id"])
    assert snapshot["totals"]["requests"] == before + 1
    assert next(row for row in snapshot["credentials"] if row["kind"] == "agent")["user"] == "alice"
    assert api["headers"]["Authorization"][7:] not in json.dumps(snapshot)


def write_rows(tmp_path, bot_id, rows, suffix=""):
    path = tmp_path / "bots" / str(bot_id) / ("agent-operations.jsonl" + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
    return path


def row(bot_id, number, **extra):
    return {"bot_id": bot_id, "command": str(number), "purpose": "public task", "outcome": "dispatched", **extra}


def test_history_cursor_survives_append_and_rotation(tmp_path):
    current = write_rows(tmp_path, 1, [row(1, i) for i in range(5)])
    first = event_log.read_history(1, limit=2)
    assert [r["command"] for r in first["items"]] == ["4", "3"]
    current.rename(Path(str(current) + ".1"))
    write_rows(tmp_path, 1, [row(1, 6)])
    second = event_log.read_history(1, limit=2, cursor=first["next_cursor"])
    assert [r["command"] for r in second["items"]] == ["2", "1"]
    third = event_log.read_history(1, limit=2, cursor=second["next_cursor"])
    assert [r["command"] for r in third["items"]] == ["0"]
    assert third["next_cursor"] is None


def test_history_is_bounded_filters_and_tolerates_partial_corrupt_lines(tmp_path, monkeypatch):
    rows = [row(1, i, purpose="target" if i % 2 else "other") for i in range(100)]
    path = write_rows(tmp_path, 1, rows)
    with path.open("ab") as file:
        file.write(b'not json\n{"bot_id":1')
    monkeypatch.setattr(event_log, "READ_BUDGET", 500)
    result = event_log.read_history(1, limit=25, query="target")
    assert result["scanned_bytes"] <= 500 and result["next_cursor"]
    assert all(r["purpose"] == "target" for r in result["items"])
    seen = {r["command"] for r in result["items"]}
    while result["next_cursor"]:
        result = event_log.read_history(1, limit=25, query="target", cursor=result["next_cursor"])
        assert not seen.intersection(r["command"] for r in result["items"])
        seen.update(r["command"] for r in result["items"])
    assert len(seen) == 50


def test_history_redacts_and_cannot_follow_links_or_foreign_bot(tmp_path):
    write_rows(tmp_path, 1, [row(2, "foreign"), row(1, "bind secret-code", purpose="Bearer lbr_secret")])
    result = event_log.read_history(1)
    assert len(result["items"]) == 1
    assert "lbr_secret" not in json.dumps(result)
    path = tmp_path / "bots/1/agent-operations.jsonl"
    path.unlink()
    secret = tmp_path / "secret"
    secret.write_text("secret")
    path.symlink_to(secret)
    with pytest.raises(HTTPException) as error:
        event_log.read_history(1)
    assert error.value.status_code == 503


def test_bad_or_rotated_cursor_fails_explicitly(tmp_path):
    write_rows(tmp_path, 1, [row(1, 1)])
    with pytest.raises(HTTPException) as error:
        event_log.read_history(1, cursor="not-a-cursor")
    assert error.value.status_code == 400
    result = event_log.read_history(1, limit=1)
    (tmp_path / "bots/1/agent-operations.jsonl").unlink()
    with pytest.raises(HTTPException) as error:
        event_log.read_history(1, cursor=result["next_cursor"])
    assert error.value.status_code == 409


def test_history_endpoint_preserves_file_schema_and_authentication(client, admin_header, tmp_path):
    bot_id = client.post("/api/bots", headers=admin_header, json=_sample_bot("history")).json()["id"]
    write_rows(tmp_path, bot_id, [row(bot_id, 1, agent_label="test-agent", purpose="Regression complete")])
    response = client.get(f"/api/bots/{bot_id}/agent-history", headers=admin_header)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["purpose"] == "Regression complete"
    assert response.json()["items"][0]["outcome"] == "dispatched"
    assert (
        client.get(f"/api/bots/{bot_id}/agent-history", headers={"Authorization": "Bearer lbk_fake"}).status_code == 401
    )


def test_successful_management_requests_are_scoped_but_denials_are_not(client, auth_header, admin_header):
    own = client.post("/api/bots", headers=auth_header, json=_sample_bot("mine")).json()["id"]
    other = client.post("/api/bots", headers=admin_header, json=_sample_bot("other")).json()["id"]
    stats = client.app.state.service_traffic
    client.get(f"/api/bots/{own}", headers=auth_header)
    assert stats.snapshot(own)["credentials"][0]["kind"] == "management"
    before = stats.snapshot(other)["totals"]["requests"]
    assert client.get(f"/api/bots/{other}", headers=auth_header).status_code == 404
    assert stats.snapshot(other)["totals"]["requests"] == before


def test_history_rejects_hard_links_and_non_regular_files(tmp_path):
    import os

    directory = tmp_path / "bots/1"
    directory.mkdir(parents=True)
    outside = tmp_path / "private"
    outside.write_text("private")
    target = directory / "agent-operations.jsonl"
    os.link(outside, target)
    with pytest.raises(HTTPException) as error:
        event_log.read_history(1)
    assert error.value.status_code == 503
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(HTTPException) as error:
        event_log.read_history(1)
    assert error.value.status_code == 503


def test_context_requests_have_verified_credential_attribution(client, api):
    response = client.get("/api/agent/context", headers=api["headers"])
    assert response.status_code == 200
    rows = client.app.state.service_traffic.snapshot(api["bot_id"])["credentials"]
    assert any(row["kind"] == "agent" and row["user"] == "alice" for row in rows)
