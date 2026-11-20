"""A missing new-lock default must not hide an already-proven booking claim."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.

import io
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from backend.test_packaged_cli_auto_scope import command, sdk  # noqa: F401
from backend.test_resource_api_resources import ACTOR, api  # noqa: F401


def book(sdk, owner=ACTOR):
    node = sdk.bots[0].state.bot_state["n1"]
    unit = node[0] if isinstance(node, list) else node
    unit["booking_list"].append(
        {
            "user_id": owner,
            "start_time": int(datetime.now(timezone.utc).timestamp()),
            "duration": 7200,
            "is_notified": True,
        }
    )


def missing_default(sdk, change=None):
    def transform(path, body):
        if path == "/api/agent/resources" and body["bot_id"] == sdk.bot_ids[0]:
            body["context"]["bot_rules"].pop("default_duration_seconds", None)
            if change:
                change(body)

    sdk.transport.transform = transform


def scoped_lock(sdk, *extra, error="group_default_duration_unavailable"):
    selection = ("--device", "0") if isinstance(sdk.bots[0].state.bot_state["n1"], list) else ("--whole-node",)
    output, events = sdk.run(
        "--connection",
        str(sdk.connections[0]),
        "command",
        "--operation",
        "lock",
        "--node",
        "n1",
        *selection,
        *extra,
        code=2,
    )
    assert output["error"] == error
    assert [row["method"] for row in events] == ["GET", "GET"]
    assert sdk.transport.commands == []
    return output


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_missing_default_guides_explicit_claim_without_dispatch_or_extra_reads(sdk):
    book(sdk)
    missing_default(sdk)
    scoped_lock(sdk, error="booking_claimable_use_explicit_claim")
    selected = ("--device", "0") if isinstance(sdk.bots[0].state.bot_state["n1"], list) else ()
    output, events = command(sdk, "n1", "lock", *selected, code=2)
    assert output["error"] == "booking_claimable_use_explicit_claim"
    assert output["command_sent"] is False
    assert [row["method"] for row in events] == ["GET"] * 5
    assert sdk.transport.commands == []


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_explicit_claim_still_uses_booked_lease_and_exactly_one_post_without_default(sdk):
    book(sdk)
    missing_default(sdk)
    selected = ("--device", "0") if isinstance(sdk.bots[0].state.bot_state["n1"], list) else ()
    output, events = command(sdk, "n1", "claim", *selected, "--exact-lease-times")
    assert output["result"]["verified"]
    assert [row["method"] for row in events] == ["GET"] * 5 + ["POST"]
    assert sdk.transport.commands == ["lock n1 dev0" if selected else "lock n1"]
    node = output["result"]["verified_snapshot"]["resources"][0]
    unit = node["devices"][0] if selected else node
    assert unit["booking_list"] == []
    assert 0 < unit["holders"][0]["remaining_seconds"] <= 7200


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "no-booking",
        "other-owner",
        "stale",
        "missing-bookings",
        "missing-holders",
        "wrong-command",
        "not-idle",
        "held",
        "malformed-booking",
        "scope",
        "duplicate-node",
        "health",
        "mode",
        "unsupported",
    ],
)
def test_missing_stale_or_unowned_evidence_keeps_the_missing_default_failure(sdk, fault):
    if fault != "no-booking":
        book(sdk, owner="neighbor" if fault == "other-owner" else ACTOR)

    def change(body):
        node = body["resources"][0]
        if fault == "other-owner":
            node["claimable_by_you"], node["claim_command"] = True, "lock n1"
        elif fault == "stale":
            body["age_seconds"] = 6
        elif fault in {"missing-bookings", "missing-holders"}:
            node.pop("booking_list" if fault == "missing-bookings" else "holders")
        elif fault == "wrong-command":
            node["claim_command"] = "lock n2"
        elif fault == "not-idle":
            node["lock_status"] = "exclusive"
        elif fault == "held":
            node["holders"] = [{"external_user_id": "neighbor"}]
        elif fault == "malformed-booking":
            node["booking_list"] = [None]
        elif fault == "scope":
            body["actor"]["external_user_id"] = "other"
        elif fault == "duplicate-node":
            body["resources"].append(dict(node))
        elif fault == "health":
            body["health"] = "error"
        elif fault == "mode":
            body["bot_type"] = body["context"]["bot"]["type"] = "NODE"
        elif fault == "unsupported":
            body["supported_operations"].remove("lock")

    missing_default(sdk, change)
    scoped_lock(sdk)


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_unknown_required_model_does_not_become_claim_guidance(sdk):
    book(sdk)
    missing_default(sdk)
    scoped_lock(sdk, "--model", "P800", "--minimum-model-count", "2")


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_changed_selector_cannot_borrow_an_earlier_claimable_observation(sdk):
    book(sdk)
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "19"})

    def change(body):
        if sdk.transport.reads[body["bot_id"]] == 2:
            body["resources"][0]["alias"] = "changed"

    missing_default(sdk, change)
    output, events = command(sdk, "19", "lock", code=2)
    assert output["error"] == "group_default_duration_unavailable"
    assert all(row["method"] == "GET" for row in events)
    assert len(events) == 5 and not sdk.transport.commands


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_noncoherent_missing_default_remains_fail_fast_without_inventory_read(sdk):
    book(sdk)
    original = sdk.transport.open

    def opened(request, timeout=None):
        response = original(request, timeout)
        path = urlsplit(request.full_url).path
        if path not in {"/api/agent-skill", "/api/agent/context"}:
            return response
        body = json.loads(response.getvalue())
        if path == "/api/agent-skill":
            body["capabilities"].remove("coherent_resource_snapshot")
        else:
            body["bot_rules"].pop("default_duration_seconds", None)
        changed = io.BytesIO(json.dumps(body).encode())
        changed.headers = response.headers
        return changed

    sdk.transport.open = opened
    scoped_lock(sdk)
    assert sdk.transport.events[-1]["path"] == "/api/agent/context"


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
@pytest.mark.parametrize("explicit_duration", [False, True])
def test_present_default_and_explicit_duration_keep_existing_dispatch_guards(sdk, explicit_duration):
    book(sdk)
    extra = ("--duration", "1h") if explicit_duration else ()
    scoped_lock(sdk, *extra, error="booking_claimable_use_explicit_claim")
    output, events = command(sdk, "n1", "lock", *extra, code=2)
    assert output["error"] == "automatic_resource_conflict"
    assert all(row["method"] == "GET" for row in events)
    assert not sdk.transport.commands


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
@pytest.mark.parametrize("fault", ["untyped-same-code", "typed-other", "unexpected"])
def test_unrelated_precondition_failures_never_become_claim_guidance(sdk, fault):
    book(sdk)
    missing_default(sdk)

    def precondition(*_args, **_kwargs):
        if fault == "untyped-same-code":
            raise sdk.module.ClientError("automatic_resource_conflict")
        if fault == "typed-other":
            raise sdk.module.AutomaticScopeError("automatic_target_changed", "Target changed.")
        raise RuntimeError("Unexpected precondition failure")

    start = len(sdk.transport.events)
    client = sdk.module.Client(str(sdk.connections[0]))
    expected = RuntimeError if fault == "unexpected" else sdk.module.ClientError
    message = "Unexpected precondition failure" if fault == "unexpected" else "group_default_duration_unavailable"
    with pytest.raises(expected, match=message):
        client.command("lock", ["n1"], [], None, _selection_precondition=precondition)
    assert [row["method"] for row in sdk.transport.events[start:]] == ["GET", "GET"]
    assert not sdk.transport.commands
