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
    first = event_log.read_history(1, limit=2, private=True)
    assert [r["command"] for r in first["items"]] == ["4", "3"]
    current.rename(Path(str(current) + ".1"))
    write_rows(tmp_path, 1, [row(1, 6)])
    second = event_log.read_history(1, limit=2, private=True, cursor=first["next_cursor"])
    assert [r["command"] for r in second["items"]] == ["2", "1"]
    third = event_log.read_history(1, limit=2, private=True, cursor=second["next_cursor"])
    assert [r["command"] for r in third["items"]] == ["0"]
    assert third["next_cursor"] is None


def test_history_is_bounded_filters_and_tolerates_partial_corrupt_lines(tmp_path, monkeypatch):
    rows = [row(1, i, purpose="target" if i % 2 else "other") for i in range(100)]
    path = write_rows(tmp_path, 1, rows)
    with path.open("ab") as file:
        file.write(b'not json\n{"bot_id":1')
    monkeypatch.setattr(event_log, "READ_BUDGET", 500)
    result = event_log.read_history(1, limit=25, private=True, query="target")
    assert result["scanned_bytes"] <= 500 and result["next_cursor"]
    assert all(r["purpose"] == "target" for r in result["items"])
    seen = {r["command"] for r in result["items"]}
    while result["next_cursor"]:
        result = event_log.read_history(1, limit=25, private=True, query="target", cursor=result["next_cursor"])
        assert not seen.intersection(r["command"] for r in result["items"])
        seen.update(r["command"] for r in result["items"])
    assert len(seen) == 50


def test_oversized_newline_terminated_record_always_advances_without_losing_neighbors(tmp_path, monkeypatch):
    budget = 512
    monkeypatch.setattr(event_log, "READ_BUDGET", budget)
    monkeypatch.setattr(event_log, "MAX_LINE_BYTES", 256)
    before = row(1, "lock", actor={"platform": "infoflow", "external_user_id": "before"})
    after = row(1, "lock", actor={"platform": "infoflow", "external_user_id": "after"})
    target = write_rows(tmp_path, 1, [before])
    with target.open("ab") as output:
        output.write(b"x" * (budget * 3) + b"\n")
        output.write(json.dumps(after).encode() + b"\n")
    cursor, seen_cursors, actors = None, set(), []
    for _ in range(10):
        page = event_log.read_history(1, cursor=cursor)
        assert page["scanned_bytes"] <= budget
        actors.extend(item["actor"]["external_user_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors, "bad lines must not cause a repeated cursor"
        seen_cursors.add(cursor)
    else:
        pytest.fail("bounded history reader did not finish after skipping the oversized line")
    assert actors == ["after", "before"]


@pytest.mark.parametrize("duration", [10**1000, -(10**1000), -1, float("inf"), float("nan")])
def test_history_ignores_invalid_duration_numbers_without_failing(tmp_path, duration):
    write_rows(tmp_path, 1, [row(1, "lock", duration_ms=duration)])
    items = event_log.read_history(1)["items"]
    assert len(items) == 1 and "duration_ms" not in items[0]


@pytest.mark.parametrize("duration", [0, 12, 1.5])
def test_history_preserves_normal_nonnegative_duration(tmp_path, duration):
    write_rows(tmp_path, 1, [row(1, "lock", duration_ms=duration)])
    assert event_log.read_history(1)["items"][0]["duration_ms"] == duration


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
    assert "Regression complete" not in response.text and "test-agent" not in response.text
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


def test_business_operations_are_bounded_and_separate_from_http():
    stats = TrafficStats()
    stats.record_operation(bot_id=1, source="chat", command="lock", outcome="dispatched")
    stats.record_operation(bot_id=2, source="agent", command="book", outcome="rejected")
    stats.record_operation(bot_id=1, source="operator", command="opkick", outcome="uncertain")
    for command in ("query", "help", "user supplied text", "lock secret"):
        stats.record_operation(bot_id=1, source="chat", command=command, outcome="dispatched")
    stats.record_operation(bot_id=1, source="invented", command="lock", outcome="dispatched")
    assert stats.snapshot()["business_operations"]["total"] == 3
    assert stats.snapshot()["totals"]["requests"] == 0
    assert stats.snapshot(1)["business_operations"]["total"] == 2
    assert stats.snapshot(2)["business_operations"]["rows"] == [
        {"source": "agent", "command": "book", "outcome": "rejected", "count": 1}
    ]
    for index in range(300):
        stats.record_operation(bot_id=index + 10, source="chat", command="lock", outcome="dispatched")
    assert len(stats.bots) == 256
    assert stats.snapshot(1)["business_operations"]["total"] == 0
    assert TrafficStats().snapshot()["business_operations"]["total"] == 0


def test_business_dispatch_is_transport_independent_and_best_effort(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from lockbot.core import command_context
    from lockbot.core.handler import CommandNotAllowed, execute_text_command

    stats = TrafficStats()
    monkeypatch.setattr(command_context, "operation_observer", stats.record_operation)
    config = SimpleNamespace(get_val=lambda key, default=None: 17 if key == "BOT_ID" else default)
    bot = SimpleNamespace(config=config, supported_commands=lambda: {"lock", "free", "query", "help"})
    bot.lock = Mock(return_value="a business reply, not proof of acquisition")
    bot.unlock = Mock(return_value="released")
    bot.query = Mock(return_value="state")
    bot.print_help = Mock(return_value="help")
    # No Request/HTTP middleware: this is also the seam for long-connection chat.
    execute_text_command("not retained", "lock private-resource 1h", bot)
    execute_text_command("not retained", "free private-resource", bot, source="api")
    execute_text_command("not retained", "query", bot)
    execute_text_command("not retained", "help", bot)
    with pytest.raises(CommandNotAllowed):
        execute_text_command("not retained", "lock private-resource", bot, allowed_commands={"query"}, source="api")
    bot.lock.side_effect = RuntimeError("maybe applied")
    with pytest.raises(RuntimeError):
        execute_text_command("not retained", "lock private-resource", bot)
    payload = stats.snapshot(17)["business_operations"]
    assert payload["total"] == 4
    assert "private-resource" not in json.dumps(payload) and "not retained" not in json.dumps(payload)
    assert {row["outcome"] for row in payload["rows"]} == {"dispatched", "rejected", "uncertain"}
    monkeypatch.setattr(command_context, "operation_observer", Mock(side_effect=RuntimeError("telemetry failure")))
    assert execute_text_command("u", "free resource", bot) == "released"


def test_direct_operator_attempt_uses_same_collector(monkeypatch):
    from lockbot.core import command_context

    stats = TrafficStats()
    monkeypatch.setattr(command_context, "operation_observer", stats.record_operation)
    with command_context.operation_attempt(3, "operator", "opkick"):
        pass
    with pytest.raises(ValueError), command_context.operation_attempt(3, "operator", "kicklock"):
        raise ValueError("bad resource")
    assert stats.snapshot(3)["business_operations"]["total"] == 2
    assert {row["outcome"] for row in stats.snapshot(3)["business_operations"]["rows"]} == {"dispatched", "rejected"}


def test_business_operations_concurrent_updates():
    stats = TrafficStats()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: stats.record_operation(bot_id=2, source="chat", command="lock", outcome="dispatched"),
                range(1000),
            )
        )
    assert stats.snapshot()["business_operations"]["total"] == 1000
    assert stats.snapshot(2)["business_operations"]["total"] == 1000
    assert sum(p["sources"]["chat"] for p in stats.snapshot(2)["business_operations"]["history"]) == 1000


def test_real_agent_command_is_counted_once_but_reads_are_not(client, api):
    bot_id = api["bot_id"]
    stats = client.app.state.service_traffic
    before = stats.snapshot(bot_id)["business_operations"]["total"]
    response = client.post(f"/api/bots/{bot_id}/commands", headers=api["headers"], json={"command": "lock n1 5m"})
    assert response.status_code == 200
    snapshot = stats.snapshot(bot_id)["business_operations"]
    assert snapshot["total"] == before + 1
    assert snapshot["rows"][-1]["source"] == "agent"
    for _ in range(2):
        client.get(f"/api/bots/{bot_id}/resources", headers=api["headers"])
    assert stats.snapshot(bot_id)["business_operations"]["total"] == before + 1


def test_private_history_projection_precedes_search_pagination_and_export(client, auth_header, db_session, tmp_path):
    from lockbot.backend.app.audit.models import AuditLog

    bot_id = client.post("/api/bots", headers=auth_header, json=_sample_bot("privacy")).json()["id"]
    rows = [
        row(
            bot_id,
            "alias node1 private-alias-body",
            agent_label="private-agent",
            purpose="private-task",
            credential_ref="private-credential",
            task_category="operator_testing",
            actor={"platform": "infoflow", "external_user_id": "alice", "future_private": "private-task"},
            reply="private-reply",
            future_private="private-future",
        )
        for _ in range(3)
    ]
    write_rows(tmp_path, bot_id, rows)
    url = f"/api/bots/{bot_id}/agent-history"
    for needle in ("private-task", "private-agent", "private-credential", "private-alias-body", "private-reply"):
        result = client.get(url, params={"query": needle}, headers=auth_header)
        assert result.status_code == 200 and result.json()["items"] == []
    first = client.get(url, params={"limit": 1}, headers=auth_header).json()
    second = client.get(url, params={"limit": 1, "cursor": first["next_cursor"]}, headers=auth_header).json()
    exported = json.dumps(first["items"] + second["items"])
    assert "private-" not in exported and "future_private" not in exported
    assert all(item["command"] == "alias" and item["public_task"] == "算子测试" for item in first["items"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    owner = db_session.query(User).filter_by(username="testuser").one()
    owner.role = "super_admin"
    db_session.commit()
    private = client.get(url, params={"query": "private-task", "include_private": True}, headers=auth_header)
    assert len(private.json()["items"]) == 3 and "private-agent" in private.text
    audit = db_session.query(AuditLog).filter_by(action="task_private.read").one()
    assert json.loads(audit.detail) == {"surface": "management.agent_history"}
    assert "private-task" not in audit.detail
    owner.role = "user"
    db_session.commit()
    assert client.get(url, params={"query": "private-task"}, headers=auth_header).json()["items"] == []


def test_bot_monitoring_hides_credential_identifiers_but_retains_caller_counts(client, auth_header, db_session):
    bot_id = client.post("/api/bots", headers=auth_header, json=_sample_bot("metrics-privacy")).json()["id"]
    hit(client.app.state.service_traffic, bot_id, "private-credential-identifier", "alice")
    url = f"/api/bots/{bot_id}/service-traffic"
    result = client.get(url, headers=auth_header)
    assert result.status_code == 200 and "private-credential-identifier" not in result.text
    assert any(row["user"] == "alice" and row["requests"] == 1 for row in result.json()["credentials"])
    owner = db_session.query(User).filter_by(username="testuser").one()
    owner.role = "super_admin"
    db_session.commit()
    assert (
        "private-credential-identifier" in client.get(url, params={"include_private": True}, headers=auth_header).text
    )


def test_operation_minutes_preserve_scope_sources_and_do_not_invent_prior_history():
    now = [3600.0]
    stats = TrafficStats(clock=lambda: now[0])
    stats.record_operation(bot_id=1, source="chat", command="lock", outcome="dispatched")
    stats.record_operation(bot_id=1, source="agent", command="book", outcome="rejected")
    stats.record_operation(bot_id=2, source="operator", command="opkick", outcome="uncertain")
    history = stats.snapshot()["business_operations"]["history"]
    assert len(history) == 60 and not any(point["observed"] for point in history[:-1])
    assert history[-1]["observed"] and history[-1]["sources"] == {"chat": 1, "agent": 1, "operator": 1}
    assert stats.snapshot(1)["business_operations"]["history"][-1]["sources"]["operator"] == 0
    assert stats.snapshot()["history"][-1]["requests"] == 0
    now[0] += 60
    last = stats.snapshot()["business_operations"]["history"][-1]
    assert last["observed"] and not any(last["sources"].values())
    assert stats.snapshot()["business_operations"]["total"] == 3


def test_operation_minutes_are_bounded_and_expire_without_new_operations():
    now = [3600.0]
    stats = TrafficStats(clock=lambda: now[0])
    for index in range(70):
        now[0] = 3600 + index * 60
        stats.record_operation(bot_id=1, source="chat", command="lock", outcome="dispatched")
    assert len(stats.operation_minutes) == len(stats.bots[1].operation_minutes) == 60
    assert all(len(bucket) <= 9 for bucket in stats.operation_minutes.values())
    assert sum(point["sources"]["chat"] for point in stats.snapshot()["business_operations"]["history"]) == 60
    now[0] += 3600
    assert not any(sum(point["sources"].values()) for point in stats.snapshot()["business_operations"]["history"])
    assert stats.snapshot()["business_operations"]["total"] == 70
