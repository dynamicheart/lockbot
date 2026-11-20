"""Real queue-state fixtures and scope-proven aggregate STOP observations, v26 only."""

import copy
from datetime import datetime, timezone

import pytest
from dev.test_skill_cli_eval import evaluation, run
from dev.test_skill_cli_v20 import events, versioned
from lockbot.backend.app.resource_api.snapshot import build_resources
from lockbot.core.io import empty_node_state

intent = evaluation.intent


def test_whole_machine_fixture_queues_match_core_and_production_snapshot():
    state = empty_node_state(["node-20", "node-21", "node-22", "node-23"])
    state["node-22"].update(status="exclusive", current_users=[{"user_id": "neighbor"}])
    production = build_resources(
        captured_at=datetime.now(timezone.utc),
        state=state,
        bot_type="QUEUE",
        platform="infoflow",
        caller_id="demo-user",
    )
    for scenario in intent.WHOLE_MACHINE_MODELS:
        fixture = intent.inventory(8, scenario=scenario, fixture_version=26)["resources"]
        assert {row["id"]: row["booking_list"] for row in fixture} == {
            row["id"]: row["booking_list"] for row in production
        }
        assert next(row for row in fixture if row["id"] == "node-22")["lock_status"] == "exclusive"


@pytest.mark.parametrize("scenario", list(intent.PROMPTS))
@pytest.mark.parametrize("bot", [7, 8])
def test_v26_only_adds_missing_queue_lists_in_whole_machine_positive_inventory(monkeypatch, scenario, bot):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 18, tzinfo=timezone.utc)

    monkeypatch.setattr(intent, "datetime", FrozenDateTime)
    old = intent.inventory(bot, scenario=scenario, fixture_version=25)
    current = intent.inventory(bot, scenario=scenario, fixture_version=26)
    expected = copy.deepcopy(old)
    if bot == 8 and scenario in intent.WHOLE_MACHINE_MODELS:
        for row in expected["resources"]:
            row.setdefault("booking_list", [])
    assert current == expected


@pytest.mark.parametrize("version", [25, 34])
@pytest.mark.parametrize(
    "scenario,model,count", [("intent-model-whole-machine", "P900", 2), ("intent-model-p800-whole-machine", "P800", 6)]
)
def test_current_sdk_whole_machine_command_keeps_the_legacy_missing_queue_guard(
    tmp_path, monkeypatch, version, scenario, model, count
):
    _, fixture = versioned(tmp_path, monkeypatch, scenario, version)
    result = run(
        fixture,
        "command",
        nodes=["node-23"],
        operation="lock",
        model=model,
        whole_node=True,
        minimum_model_count=count,
        duration="2h",
    )
    wire = events(fixture, "cli-http.jsonl")
    posts = [row for row in wire if row["method"] == "POST"]
    if version == 25:
        assert result["error"] == "automatic_inventory_invalid"
        assert result["command_sent"] is False and not posts
    else:
        assert result["result"]["verified"]
        assert len(posts) == 1 and posts[0]["command"] == "lock node-23 2h"
        assert {row["resource_bot"] for row in wire if row["url"].endswith("/resources")} == {7, 8}
        assert evaluation.score(fixture.root, f"node-23 {model}整机已锁定两小时。")["observable_pass"]


def test_v26_does_not_weaken_the_sdk_missing_queue_guard(tmp_path, monkeypatch):
    original = intent.inventory

    def malformed(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if snapshot["bot_id"] == 8:
            snapshot["resources"][0].pop("booking_list", None)
        return snapshot

    monkeypatch.setattr(intent, "inventory", malformed)
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-whole-machine", 26)
    result = run(fixture, "command", nodes=["node-23"], operation="lock", whole_node=True, model="P800", duration="2h")
    assert result["error"] == "automatic_inventory_invalid"
    assert result["command_sent"] is False
    assert not any(row["method"] == "POST" for row in events(fixture, "cli-http.jsonl"))


def aggregate_observations(fixture):
    return [
        row
        for row in evaluation.verified_catalog_reads(fixture, events(fixture), events(fixture, "cli-http.jsonl"))
        if row["args"]["action"] == "resources-all"
    ]


@pytest.mark.parametrize("version,stopped", [(25, False), (26, True)])
def test_raw_grouped_observation_is_consumed_only_when_v26_wire_verified(tmp_path, monkeypatch, version, stopped):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", version)
    run(fixture, "resources-all", model="P800")
    observations = aggregate_observations(fixture)
    assert intent.observed_unknown_machine(observations, "P800", include_aggregate=version >= 26) is stopped
    assert not intent.observed_unknown_machine(events(fixture), "P800", include_aggregate=True)


@pytest.mark.parametrize(
    "fault", ["other_group", "wrong_actor", "wrong_instance", "wrong_uuid", "no_wire", "forged_marker"]
)
def test_aggregate_unknown_cannot_borrow_another_scope_or_fabricate_proof(tmp_path, monkeypatch, fault):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 26)
    run(fixture, "resources-all", model="P800")
    trace, wire = events(fixture), events(fixture, "cli-http.jsonl")
    groups = trace[0]["result"]["result"]["groups"]
    machine = next(item for item in groups if item["identity"]["bot"]["id"] == 8)
    if fault == "other_group":
        groups.remove(machine)
        # A card-scope node may reuse the same node ID and carry unknown metadata.
        groups[0]["snapshot"]["selection"] = {"unknown_hardware_nodes": ["node-20"]}
    elif fault == "wrong_actor":
        machine["identity"]["actor"] = {"external_user_id": "someone-else"}
    elif fault == "wrong_instance":
        machine["identity"]["instance_id"] = "different-instance"
    elif fault == "wrong_uuid":
        machine["identity"]["bot"]["uuid"] = "different-bot"
    elif fault == "no_wire":
        wire = []
    else:
        trace[0]["verified_group_snapshots"] = [copy.deepcopy(machine)]
        groups.clear()
    observations = [
        row
        for row in evaluation.verified_catalog_reads(fixture, trace, wire)
        if row["args"]["action"] == "resources-all"
    ]
    assert not intent.observed_unknown_machine(observations, "P800", include_aggregate=True)


@pytest.mark.parametrize("aggregate", [False, True])
def test_known_requested_model_on_the_node_is_not_overridden_by_another_unknown_card(tmp_path, monkeypatch, aggregate):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 26)
    run(fixture, "resources-all")
    observations = aggregate_observations(fixture)
    machine = next(item for item in observations[0]["verified_group_snapshots"] if item["identity"]["bot"]["id"] == 8)
    snapshot = machine["snapshot"]
    snapshot["selection"] = {"unknown_hardware_nodes": ["node-20"]}
    snapshot["resources"][0]["accelerators"] = [{"model": "P800", "count": 1}, {"model": None, "count": 7}]
    if not aggregate:
        observations = [{"args": {"action": "resources", "group": intent.MACHINES}, "result": {"result": snapshot}}]
    assert not intent.observed_unknown_machine(observations, "P800", include_aggregate=True)
    if not aggregate:
        assert intent.observed_unknown_machine(observations, "P800")  # Keep v25's earlier ordering unchanged.
