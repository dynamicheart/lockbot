"""Qualified resources dispatch one exact operation without unrelated scope reads."""

# ruff: noqa: F811 -- imported pytest fixtures
import io
import json
from copy import deepcopy
from urllib.error import URLError

import pytest

from backend.test_packaged_cli_auto_scope import sdk  # noqa: F401
from backend.test_resource_api_resources import ACTOR, _hold, api  # noqa: F401


def reference(sdk, index=0, node="n1"):
    return sdk.module.resource_reference(json.loads(sdk.connections[index].read_text()), node, width=64)


def command(sdk, ref=None, operation="lock", *options, code=0):
    return sdk.run("command", "--operation", operation, "--ref", ref or reference(sdk), *options, code=code)


def test_qualified_whole_node_uses_one_manifest_one_observation_one_post(sdk):
    selected = reference(sdk)
    # Local stale records have no Key and must not participate in this qualified task.
    template = json.loads(sdk.connections[0].read_text())
    for index in range(10):
        directory = sdk.catalog / f"unrelated-{index}"
        directory.mkdir(mode=0o700)
        path = directory / "connection.json"
        path.write_text(json.dumps({**template, "bot_uuid": f"unrelated-{index}", "key_file": "missing"}))
        path.chmod(0o600)
    sdk.transport.transform = lambda _path, _body: None
    output, events = command(sdk, selected, "lock", "--whole-node", "--duration", "1h")
    assert output["result"]["verified"] and output["result"]["target_reference"] == selected
    assert [event["method"] for event in events] == ["GET", "GET", "POST"]
    assert sdk.transport.commands == ["lock n1 1h"]
    assert sdk.transport.reads[sdk.bot_ids[0]] == 1 and sdk.transport.reads[sdk.bot_ids[1]] == 0


def test_explicit_reference_ignores_a_different_nodes_alias_matching_the_exact_id(sdk):
    bot = sdk.register({"node7": "first", "node07": "second"})
    bot.config.set_val("NODE_ALIASES", {"node7": "node07"})
    output, events = command(sdk, reference(sdk, -1, "node07"), "lock", "--whole-node", "--duration", "1h")
    assert output["result"]["verified"] and sdk.transport.commands == ["lock node07 1h"]
    assert sum(event["method"] == "POST" for event in events) == 1
    state = bot.state_snapshot()[1]
    assert not state["node7"]["current_users"] and state["node07"]["current_users"][0]["user_id"] == ACTOR


def test_qualified_card_zero_remains_one_card_and_checks_model(sdk):
    sdk.register({"node07": ["P800", "H20"]}, "DEVICE")
    output, events = command(
        sdk, reference(sdk, -1, "node07"), "lock", "--device", "dev0", "--model", "P800", "--duration", "1h"
    )
    assert output["result"]["verified"] and sdk.transport.commands == ["lock node07 dev0 1h"]
    assert [event["method"] for event in events] == ["GET", "GET", "POST"]
    resources = output["result"]["verified_snapshot"]["resources"][0]["devices"]
    assert resources[0]["holders"] and not resources[1]["holders"]


@pytest.mark.parametrize("change", ["scope", "node", "devices", "stale", "health", "duration"])
def test_qualified_target_changes_and_limits_stop_before_post(sdk, change):
    selected = reference(sdk)
    if change == "scope":
        config = json.loads(sdk.connections[0].read_text())
        config["actor"]["external_user_id"] = "changed-subject"
        sdk.connections[0].write_text(json.dumps(config))
    else:

        def changed(path, body):
            if path != "/api/agent/resources":
                return
            if change == "node":
                body["resources"].append(deepcopy(body["resources"][0]))
            elif change == "devices":
                body["resources"][0]["devices"] = []
            elif change == "stale":
                body["age_seconds"] = 30
            elif change == "health":
                body["health"] = "error"
            else:
                body["context"]["bot_rules"]["effective_max_lock_duration_seconds"] = 60

        sdk.transport.transform = changed
    output, events = command(sdk, selected, "lock", "--whole-node", "--duration", "1h", code=2)
    assert output["command_sent"] is False and output["retry_mutation"] is False
    assert not any(event["method"] == "POST" for event in events)
    if change == "scope":
        assert events == [] and output["error"] == "resource_reference_scope_unavailable"


@pytest.mark.parametrize("action", ["resources", "command"])
def test_loaded_connection_pins_are_rechecked_after_local_reference_resolution(sdk, monkeypatch, action):
    selected = reference(sdk)
    original = sdk.module.Client

    def changed(*args, **kwargs):
        config = json.loads(sdk.connections[0].read_text())
        config["actor"]["external_user_id"] = "changed-between-resolution-and-load"
        sdk.connections[0].write_text(json.dumps(config))
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk.module, "Client", changed)
    arguments = (
        ["resources", "--ref", selected, "--summary"]
        if action == "resources"
        else ["command", "--operation", "lock", "--ref", selected, "--whole-node", "--duration", "1h"]
    )
    output, events = sdk.run(*arguments, code=2)
    assert output["error"] == "resource_reference_scope_mismatch" and events == []


@pytest.mark.parametrize("operation", ["lock", "slock", "release"])
def test_another_users_holding_is_not_acquired_or_released_by_qualification(sdk, operation):
    _hold(sdk.api, "n1", "peer")
    options = ["--whole-node"] + (["--duration", "1h"] if operation != "release" else [])
    output, events = command(sdk, None, operation, *options, code=2)
    assert output["command_sent"] is False
    assert not any(event["method"] == "POST" for event in events)


def test_after_response_loss_only_one_post_and_next_step_is_targeted_read_only(sdk):
    selected = reference(sdk)
    original = sdk.transport.open

    def lost(request, timeout=None):
        response = original(request, timeout)
        if request.get_method() == "POST":
            raise URLError("synthetic response loss after actual local commit")
        return response

    sdk.transport.open = lost
    output, events = command(sdk, selected, "lock", "--whole-node", "--duration", "1h", code=2)
    assert output["command_outcome"] == "unknown" and output["retry_mutation"] is False
    assert sum(event["method"] == "POST" for event in events) == 1
    assert output["next_command"] == ["resources", "--ref", selected, "--mine", "--summary"]
    sdk.transport.open = original
    confirmed, reads = sdk.run(*output["next_command"])
    assert confirmed["result"]["rows"][0]["holding_units"] == 1
    assert len(reads) == 2 and all(event["method"] == "GET" for event in reads)


def test_ambiguous_post_observation_does_not_claim_verified_or_replay(sdk):
    selected = reference(sdk)
    original = sdk.transport.open

    def duplicate(request, timeout=None):
        response = original(request, timeout)
        if request.get_method() == "POST":
            body = json.loads(response.getvalue())
            snapshot = body["resource_snapshot"]
            snapshot["resources"].append(deepcopy(snapshot["resources"][0]))
            result = io.BytesIO(json.dumps(body).encode())
            result.headers = response.headers
            return result
        return response

    sdk.transport.open = duplicate
    output, events = command(sdk, selected, "lock", "--whole-node", "--duration", "1h", code=2)
    assert output["command_outcome"] == "unknown" and sum(event["method"] == "POST" for event in events) == 1
    assert output["error"] == "mutation_not_confirmed_read_resources_do_not_replay"


@pytest.mark.parametrize("change", ["idle_holder", "wrong_mode", "health"])
def test_inconsistent_post_state_cannot_confirm_a_qualified_mutation(sdk, change):
    original = sdk.transport.open

    def inconsistent(request, timeout=None):
        response = original(request, timeout)
        if request.get_method() == "POST":
            body = json.loads(response.getvalue())
            snapshot = body["resource_snapshot"]
            if change == "health":
                snapshot["health"] = "error"
            else:
                snapshot["resources"][0]["lock_status"] = "idle" if change == "idle_holder" else "shared"
            result = io.BytesIO(json.dumps(body).encode())
            result.headers = response.headers
            return result
        return response

    sdk.transport.open = inconsistent
    output, events = command(sdk, None, "lock", "--whole-node", "--duration", "1h", code=2)
    assert output["error"] == "mutation_not_confirmed_read_resources_do_not_replay"
    assert output["command_outcome"] == "unknown" and output["retry_mutation"] is False
    assert sum(event["method"] == "POST" for event in events) == 1


def test_qualified_shared_renew_and_release_keep_the_exact_node(sdk):
    selected = reference(sdk)
    locked, _ = command(sdk, selected, "slock", "--whole-node", "--duration", "1h")
    assert locked["result"]["verified"]
    renewed, events = command(sdk, selected, "renew", "--whole-node", "--duration", "1h")
    assert renewed["result"]["verified"] and sdk.transport.commands[-1] == "slock n1 1h"
    assert sum(event["method"] == "POST" for event in events) == 1
    released, events = command(sdk, selected, "release", "--whole-node")
    assert released["result"]["verified"] and sdk.transport.commands[-1] == "unlock n1"
    assert sum(event["method"] == "POST" for event in events) == 1


def test_qualified_queue_booking_claim_and_release(sdk):
    sdk.register({"node07": "queue-machine"}, "QUEUE")
    selected = reference(sdk, -1, "node07")
    booked, _ = command(sdk, selected, "book", "--whole-node", "--duration", "1h")
    assert booked["result"]["verified"] and sdk.transport.commands[-1] == "book node07 1h"
    claimed, _ = command(sdk, selected, "claim", "--whole-node")
    assert claimed["result"]["verified"] and sdk.transport.commands[-1] == "lock node07"
    released, _ = command(sdk, selected, "release", "--whole-node")
    assert released["result"]["verified"] and sdk.transport.commands[-1] == "unlock node07"


@pytest.mark.parametrize("options", [[], ["--whole-node", "--device", "0"]])
def test_reference_requires_explicit_nonconflicting_allocation_selection(sdk, options):
    output, events = command(sdk, None, "lock", *options, "--duration", "1h", code=2)
    assert output["command_sent"] is False and events == []


def test_legacy_mode_cannot_dispatch_a_qualified_command(sdk):
    output, events = sdk.run(
        "--legacy-read",
        "command",
        "--operation",
        "lock",
        "--ref",
        reference(sdk),
        "--whole-node",
        "--duration",
        "1h",
        code=2,
    )
    assert output["error"] == "legacy_read_only" and events == []
