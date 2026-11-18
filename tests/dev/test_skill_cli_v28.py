"""Production-held state parity, stable lease pins and evidence-backed concise replies."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned
from lockbot.backend.app.resource_api.snapshot import build_resources
from lockbot.core.io import empty_node_state

intent = evaluation.intent


def test_elapsed_query_time_does_not_reset_the_same_runtime_lease(monkeypatch):
    now = datetime.now(timezone.utc)
    reference = now.replace(microsecond=0).isoformat()
    before = intent.inventory(8, scenario="intent-ip-tail-held", fixture_version=28, lease_reference=reference)

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(minutes=10)

    monkeypatch.setattr(intent, "datetime", LaterDateTime)
    later = intent.inventory(8, scenario="intent-ip-tail-held", fixture_version=28, lease_reference=reference)
    first_holder, later_holder = before["resources"][0]["holders"][0], later["resources"][0]["holders"][0]
    assert first_holder["expires_at"] == later_holder["expires_at"]
    assert first_holder["start_time"] == later_holder["start_time"]
    assert 599 <= first_holder["remaining_seconds"] - later_holder["remaining_seconds"] <= 600


def write_events(fixture, name, rows):
    (fixture.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_renew_fixture_uses_real_production_queue_fields_and_finite_original_lease():
    reference = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot = intent.inventory(8, scenario="intent-renew", fixture_version=28, lease_reference=reference.isoformat())
    state = empty_node_state(["node-20"])
    state["node-20"].update(
        status="exclusive",
        current_users=[
            {
                "user_id": "demo-user",
                "start_time": int(reference.timestamp()) - 5400,
                "duration": 7200,
            }
        ],
    )
    expected = build_resources(
        captured_at=datetime.fromisoformat(snapshot["captured_at"]),
        state=state,
        bot_type="QUEUE",
        platform="infoflow",
        caller_id="demo-user",
    )[0]
    assert snapshot["resources"] == [{**expected, "alias": "训练节点"}]


@pytest.mark.parametrize("scenario", ["intent-renew", "intent-ip-tail-held"])
def test_repeated_reads_keep_expiry_and_start_but_extend_once_after_a_write(scenario):
    reference = datetime.now(timezone.utc).replace(microsecond=0)
    before = intent.inventory(8, scenario=scenario, fixture_version=28, lease_reference=reference.isoformat())
    later = intent.inventory(8, scenario=scenario, fixture_version=28, lease_reference=reference.isoformat())
    after = intent.inventory(
        8,
        intent.EXPECTED[scenario][1] if scenario in intent.EXPECTED else "",
        scenario=scenario,
        fixture_version=28,
        lease_reference=reference.isoformat(),
        writes=1,
    )
    first, repeated, extended = [snap["resources"][0]["holders"][0] for snap in (before, later, after)]
    assert first["expires_at"] == repeated["expires_at"]
    assert first["start_time"] == repeated["start_time"] == extended["start_time"]
    delta = 7200 if scenario == "intent-renew" else 14400
    assert (
        datetime.fromisoformat(extended["expires_at"]) - datetime.fromisoformat(first["expires_at"])
    ).total_seconds() == delta


@pytest.mark.parametrize("version,passes", [(27, False), (28, True)])
def test_actual_sdk_renew_only_works_with_the_new_healthy_queue_fixture(tmp_path, monkeypatch, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-renew", version)
    output = run(fixture, "command", nodes=["node-20"], operation="renew", whole_node=True, duration="2h")
    posts = [row for row in events(fixture, "cli-http.jsonl") if row["method"] == "POST"]
    if passes:
        assert output["result"]["verified"] and len(posts) == 1
        assert evaluation.score(fixture.root, "node-20训练节点已续两小时，约2小时30分后到期。")["observable_pass"]
    else:
        assert output["error"] == "automatic_inventory_invalid" and not posts


@pytest.mark.parametrize("fault", ["queue", "health", "stale", "holder"])
def test_faults_after_healthy_construction_still_prevent_dispatch(tmp_path, monkeypatch, fault):
    original = intent.inventory

    def malformed(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if snapshot["bot_id"] == 8:
            if fault == "queue":
                snapshot["resources"][0].pop("booking_list")
            elif fault == "health":
                snapshot["health"] = "unknown"
            elif fault == "stale":
                snapshot["age_seconds"] = 600
            else:
                snapshot["resources"][0]["holders"] = []
        return snapshot

    monkeypatch.setattr(intent, "inventory", malformed)
    _, fixture = versioned(tmp_path, monkeypatch, "intent-renew", 28)
    output = run(fixture, "command", nodes=["node-20"], operation="renew", whole_node=True, duration="2h")
    assert output.get("command_sent") is False
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))


def held(fixture):
    output = run(fixture, "command", nodes=["19"], operation="lock", duration="4h")
    assert output.get("result", {}).get("verified"), output
    return output


def test_raw_lock_on_a_fresh_same_mode_holding_is_proven_as_exactly_one_renewal(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ip-tail-held", 28)
    output = held(fixture)
    resolution = output["result"]["resolution"]
    assert {
        key: resolution[key] for key in ("requested_operation", "effective_operation", "operation_mapping_reason")
    } == {
        "requested_operation": "lock",
        "effective_operation": "renew",
        "operation_mapping_reason": "fresh_own_same_mode_relock",
    }
    assert events(fixture)[0]["args"]["operation"] == "lock"
    posts = [row for row in events(fixture, "cli-http.jsonl") if row["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["command"] == "lock raven 4h"
    assert evaluation.score(fixture.root, "raven已续四小时，约六小时后到期。")["observable_pass"]


@pytest.mark.parametrize(
    "fault",
    [
        "reason",
        "effective",
        "requested",
        "duration",
        "source",
        "expiry",
        "start",
        "owner",
        "rule",
        "no_wire_proof",
        "fresh_expiry",
        "fresh_rule",
        "guessed_renew",
        "pre_expiry",
        "pre_owner",
        "pre_cap",
        "pre_health",
        "pre_default",
    ],
)
def test_relock_proof_cannot_ignore_lease_rule_or_original_operation_pins(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-ip-tail-held", 28)
    held(fixture)
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    result = trace[0]["result"]["result"]
    resolution = result["resolution"]
    keys = {
        "reason": "operation_mapping_reason",
        "effective": "effective_operation",
        "requested": "requested_operation",
        "duration": "effective_duration",
        "source": "duration_source",
    }
    if fault in keys:
        resolution[keys[fault]] = "forged"
    elif fault in {"expiry", "start", "owner"}:
        holder = result["verified_snapshot"]["resources"][0]["holders"][0]
        holder[{"expiry": "expires_at", "start": "start_time", "owner": "external_user_id"}[fault]] = "forged"
    elif fault == "rule":
        result["verified_snapshot"]["context"]["bot_rules"]["forbid_relock"] = "false"
    elif fault == "guessed_renew":
        trace[0]["args"]["operation"] = "renew"
    else:
        reads = [row for row in wire if "resource_relock_observation" in row]
        if fault == "no_wire_proof":
            for row in reads:
                row.pop("resource_relock_observation")
        elif fault == "fresh_rule":
            reads[-1]["resource_relock_observation"]["forbid_relock"] = True
        elif fault.startswith("pre_"):
            pre = next(row for row in reads if row["method"] == "GET" and row.get("resource_bot") == 8)
            if fault == "pre_health":
                pre["resource_read_state"]["health"] = "unknown"
            elif fault == "pre_cap":
                pre["resource_relock_observation"]["effective_max_lock_duration_seconds"] = "-1"
            elif fault == "pre_default":
                # The original4h request must never be graded as an omitted/default duration.
                resolution["requested_duration"] = None
                resolution["duration_source"] = "group-default"
            else:
                holder = pre["resource_relock_observation"]["resources"][0]["holders"][0]
                holder["expires_at" if fault == "pre_expiry" else "external_user_id"] = "forged"
        else:
            reads[-1]["resource_relock_observation"]["resources"][0]["holders"][0]["expires_at"] = (
                "2026-01-01T00:00:00+00:00"
            )
    write_events(fixture, "trace.jsonl", trace)
    write_events(fixture, "cli-http.jsonl", wire)
    grade = evaluation.score(fixture.root, "raven已续四小时。")
    assert not grade["checks"]["sdk_resolved_target_before_post"] and not grade["observable_pass"]


@pytest.mark.parametrize("version,passes", [(27, False), (28, True)])
def test_concise_verified_original_hint_is_valid_only_in_v28(tmp_path, monkeypatch, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", version)
    assert run(fixture, "command", nodes=["19"], operation="lock")["result"]["verified"]
    grade = evaluation.score(fixture.root, "已锁定19（整机），约3小时后到期。")
    assert grade["observable_pass"] is passes
    for invalid in ("已锁定119。", "已锁定node-19。", "已锁定19.example.test。", "已锁定，19小时后到期。"):
        assert not evaluation.score(fixture.root, invalid)["observable_pass"]


@pytest.mark.parametrize("fault", ["wire", "scope", "resolution"])
def test_hint_report_requires_actual_verified_unique_mapping(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias", 28)
    run(fixture, "command", nodes=["19"], operation="lock")
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    if fault == "resolution":
        trace[0]["result"]["result"]["resolution"]["matches"][0]["node"] = "cedar"
    elif fault == "wire":
        wire = []
    else:
        next(row for row in wire if row.get("resource_bot") == 7)["status"] = 503
    write_events(fixture, "trace.jsonl", trace)
    write_events(fixture, "cli-http.jsonl", wire)
    assert not evaluation.score(fixture.root, "已锁定19（整机），约3小时后到期。")["observable_pass"]


CHOICE = "“19”对应了多个可用节点，需要你选一个再锁定：\n加速卡群：cedar\n整机队列群：raven\n回复要锁定的节点名即可。"


@pytest.mark.parametrize("version,passes", [(27, False), (28, True)])
def test_new_explicit_choice_imperative_is_backed_by_real_candidates(tmp_path, monkeypatch, version, passes):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias-ambiguous", version)
    assert run(fixture, "command", nodes=["19"], operation="lock")["command_sent"] is False
    assert evaluation.score(fixture.root, CHOICE)["observable_pass"] is passes


@pytest.mark.parametrize("fault", ["negation", "narrative", "candidate", "no_wire", "post", "success"])
def test_choice_imperative_does_not_replace_candidate_and_no_post_proof(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-public-ip-alias-ambiguous", 28)
    run(fixture, "command", nodes=["19"], operation="lock")
    reply = CHOICE
    wire = events(fixture, "cli-http.jsonl")
    if fault == "negation":
        reply = reply.replace("需要你选", "不需要你选")
    elif fault == "narrative":
        reply = reply.replace("需要你选一个再锁定：", "其中一个可锁定。")
    elif fault == "candidate":
        reply = reply.replace("raven", "other")
    elif fault == "no_wire":
        wire = []
    elif fault == "post":
        wire.append({**wire[-1], "method": "POST", "status": 504})
    else:
        reply += "已申请成功。"
    write_events(fixture, "cli-http.jsonl", wire)
    assert not evaluation.score(fixture.root, reply)["observable_pass"]
