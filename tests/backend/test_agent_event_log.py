"""Agent purposes survive as private bounded files, independently of live declaration cleanup."""

# ruff: noqa: F811

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.bots.models import BotLog
from lockbot.backend.app.resource_api import event_log
from lockbot.backend.app.resource_auth.dependencies import ResourceActor

from backend.test_resource_api_resources import api  # noqa: F401


@pytest.fixture(autouse=True)
def event_managers(api, monkeypatch):
    from fake_adapter import FakeAdapter

    api["bot"].adapter = FakeAdapter(config=api["bot"].config)
    monkeypatch.setattr("lockbot.backend.app.resource_api.usage.bot_manager", api["manager"])


def records(tmp_path, api):
    path = tmp_path / "bots" / str(api["bot_id"]) / "agent-operations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def command(client, api, text, **metadata):
    return client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": text, **metadata}
    )


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE"], indirect=True)
def test_operation_metadata_goes_to_files_and_does_not_claim_success(client, api, tmp_path, db_session):
    text = "lock n1 dev0 1h" if api["bot"].config.get_val("BOT_TYPE") == "DEVICE" else "lock n1 1h"
    before_audit = db_session.query(AuditLog).count()
    response = command(client, api, text, agent_label="build-agent", purpose="Develop feature X")
    assert response.status_code == 200
    entry = records(tmp_path, api)[-1]
    assert entry["actor"] == {"platform": "infoflow", "external_user_id": "alice"}
    assert entry["agent_label"] == "build-agent" and entry["purpose"] == "Develop feature X"
    assert entry["http_status"] == 200 and entry["outcome"] == "dispatched"
    assert "success" not in entry and entry["duration_ms"] >= 0
    expected = {"resource_id": "n1"}
    if "dev0" in text:
        expected["device_ids"] = [0]
    assert entry["requested_resources"] == [expected]
    observed = entry["observed_state"]["resources"]
    assert observed[0]["holders"][0]["duration"] == 3600
    assert observed[0]["holders"][0]["source"] == "api"
    assert entry["observed_state"]["captured_at"]
    assert db_session.query(BotLog).count() == 0
    assert db_session.query(AuditLog).count() == before_audit
    path = tmp_path / "bots" / str(api["bot_id"]) / "agent-operations.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600


def test_rejected_core_reply_is_still_only_a_dispatch(client, api, tmp_path):
    assert command(client, api, "lock n1 1h").status_code == 200
    response = client.post(
        f"/api/bots/{api['bot_id']}/commands", headers=api["other_headers"], json={"command": "lock n1 1h"}
    )
    assert response.status_code == 200
    event = records(tmp_path, api)[-1]
    assert event["actor"]["external_user_id"] == "bob" and event["outcome"] == "dispatched"
    assert event["observed_state"]["resources"][0]["holders"] == []
    assert api["bot"].state.bot_state["n1"]["current_users"][0]["user_id"] == "alice"


def test_usage_changes_and_release_keep_history_after_live_cleanup(client, api, tmp_path):
    assert command(client, api, "lock n1 1h", agent_label="dev-agent", purpose="Original task").status_code == 200
    path = f"/api/bots/{api['bot_id']}/usage-declarations/n1"
    assert (
        client.put(path, headers=api["headers"], json={"purpose": "New task", "agent_label": "test-agent"}).status_code
        == 200
    )
    assert client.delete(path, headers=api["headers"]).status_code == 200
    assert command(client, api, "free n1", purpose="Task completed").status_code == 200
    events = records(tmp_path, api)
    assert [row["operation"] for row in events] == ["command", "usage.update", "usage.delete", "command"]
    assert events[1]["purpose"] == "New task" and events[1]["outcome"] == "applied"
    assert events[-1]["purpose"] == "Task completed"
    assert events[-1]["observed_state"]["resources"][0]["holders"] == []
    assert not api["bot"].state.bot_state["n1"]["current_users"]
    current = client.get(f"/api/bots/{api['bot_id']}/usage-declarations", headers=api["headers"])
    assert current.json()["items"] == []
    assert records(tmp_path, api) == events


def test_refusals_and_invalid_bodies_do_not_log_raw_payloads(client, api, tmp_path):
    assert command(client, api, "bind SECRET123").status_code == 403
    assert records(tmp_path, api)[-1]["command"] == "bind [redacted]"
    response = command(client, api, "lock n1 1h", external_user_id="bob", purpose="secret raw body")
    assert response.status_code == 422
    event = records(tmp_path, api)[-1]
    assert event["actor"]["external_user_id"] == "alice" and event["outcome"] == "rejected"
    assert "secret raw body" not in json.dumps(event) and "bob" not in json.dumps(event)


def test_unverified_requests_and_resource_polls_have_no_history_file(client, api, tmp_path):
    assert (
        client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers={"Authorization": "Bearer lbr_wrong"},
            json={"command": "free"},
        ).status_code
        == 401
    )
    assert client.get(f"/api/bots/{api['bot_id']}/resources", headers=api["headers"]).status_code == 200
    assert records(tmp_path, api) == []


def test_file_failure_does_not_fail_or_replay_the_resource_command(client, api, tmp_path, monkeypatch):
    original_open = event_log.os.open

    def fail(path, *args, **kwargs):
        if str(path).endswith("agent-operations.jsonl"):
            raise PermissionError("test-only")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(event_log.os, "open", fail)
    response = command(client, api, "lock n1 1h", purpose="one attempt")
    assert response.status_code == 200
    holder = api["bot"].state.bot_state["n1"]["current_users"][0]
    assert holder["duration"] == 3600
    assert records(tmp_path, api) == []


def _actor(bot_id=17):
    return ResourceActor("fakeim", "alice", bot_id, 1, "reference")


def test_known_keys_tokens_and_view_codes_are_redacted(tmp_path, api):
    event = {
        "operation": "command",
        "stage": "dispatched",
        "command": "query",
        "purpose": "lbr_private-key lbk_operator-key token=hidden-value password=hidden-password",
        "reply": (
            "http://example.invalid/#/bot/1?view=lbv1.nonce.signature "
            "Bearer arbitrary-credential eyJheader.payload.signature"
        ),
    }
    event_log.write_event(actor=_actor(api["bot_id"]), event=event, status=200, duration_ms=1)
    encoded = json.dumps(records(tmp_path, api))
    for secret in [
        "lbr_private",
        "lbk_operator",
        "hidden-value",
        "hidden-password",
        "lbv1.nonce",
        "arbitrary-credential",
        "eyJheader",
    ]:
        assert secret not in encoded
    assert "[redacted]" in encoded


def test_size_rotation_is_bounded_and_preserves_parseable_lines(tmp_path, api, monkeypatch):
    monkeypatch.setattr(event_log, "MAX_BYTES", 1000)
    monkeypatch.setattr(event_log, "BACKUPS", 2)
    for sequence in range(20):
        event_log.write_event(
            actor=_actor(), event={"operation": "command", "sequence": sequence}, status=200, duration_ms=1
        )
    files = sorted((tmp_path / "bots/17").glob("agent-operations.jsonl*"))
    assert len(files) == 3
    for path in files:
        assert path.stat().st_size <= 1000 and path.stat().st_mode & 0o777 == 0o600
        assert all(json.loads(line)["schema_version"] == 1 for line in path.read_text().splitlines())
    assert json.loads((tmp_path / "bots/17/agent-operations.jsonl").read_text().splitlines()[-1])["sequence"] == 19


def test_concurrent_writers_do_not_interleave_records(tmp_path, api):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda n: event_log.write_event(actor=_actor(), event={"sequence": n}, status=200, duration_ms=1),
                range(50),
            )
        )
    lines = (tmp_path / "bots/17/agent-operations.jsonl").read_text().splitlines()
    assert sorted(json.loads(line)["sequence"] for line in lines) == list(range(50))


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_log_cannot_follow_aliases_to_another_file(tmp_path, api, kind):
    outside = tmp_path / "preserve.txt"
    outside.write_text("keep")
    directory = tmp_path / "bots/17"
    directory.mkdir(parents=True)
    path = directory / "agent-operations.jsonl"
    if kind == "symlink":
        path.symlink_to(outside)
    else:
        path.hardlink_to(outside)
    event_log.write_event(actor=_actor(), event={"operation": "command"}, status=200, duration_ms=1)
    assert outside.read_text() == "keep"
