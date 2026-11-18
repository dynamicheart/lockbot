"""A fresh explicit chat-style relock extends only the same verified own lease."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.

from datetime import datetime, timezone

import pytest

from backend.test_packaged_cli_auto_scope import command, no_post, sdk  # noqa: F401
from backend.test_resource_api_resources import ACTOR, api  # noqa: F401


def own_selected(sdk, operation="lock", *, all_nodes=False, coholder=False):
    """Populate backend state with fixed start/expiry, not a sliding fake lease."""
    node = sdk.bots[0].state.bot_state
    entries = list(node.values()) if all_nodes else [node["n1"]]
    units = [unit for entry in entries for unit in (entry if isinstance(entry, list) else [entry])]
    started = int(datetime.now(timezone.utc).timestamp()) - 60
    for unit in units:
        unit["status"] = "shared" if operation == "slock" else "exclusive"
        unit["current_users"] = [{"user_id": ACTOR, "start_time": started, "duration": 7200, "is_notified": False}]
        if coholder:
            unit["current_users"].append(
                {"user_id": "neighbor", "start_time": started, "duration": 7200, "is_notified": False}
            )
    sdk.bots[0].config.set_val("FORBID_RELOCK", False)
    sdk.bots[0].config.set_val("DEFAULT_DURATION", 7200)
    return units


def selection(snapshot):
    return snapshot["resources"][0].get("devices", [snapshot["resources"][0]])


@pytest.mark.parametrize(
    ("api", "operation"),
    [
        ("NODE", "lock"),
        ("QUEUE", "lock"),
        ("DEVICE", "lock"),
        ("QUEUE_DEVICE", "lock"),
        ("NODE", "slock"),
        ("DEVICE", "slock"),
    ],
    indirect=["api"],
)
@pytest.mark.parametrize("duration", ["4h", None])
def test_owned_relock_maps_to_one_renewal_with_unchanged_full_selection(sdk, operation, duration):
    units = own_selected(sdk, operation, coholder=operation == "slock")
    before = [row["current_users"][0]["duration"] for row in units]
    files = {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}
    flags = ("--duration", duration) if duration else ()
    output, events = command(sdk, "n1", operation, *flags, "--exact-lease-times")
    result = output["result"]
    assert result["verified"]
    resolution = result["resolution"]
    assert resolution["requested_operation"] == operation and resolution["effective_operation"] == "renew"
    assert resolution["operation_mapping_reason"] == "fresh_own_same_mode_relock"
    assert resolution["requested_duration"] == duration
    assert resolution["effective_duration"] == (duration or "120m")
    assert resolution["selection_checked_before_post"] is True
    assert resolution["duration_source"] == ("user-request" if duration else "group-default")
    if duration is None:
        assert result["duration_source"] == "group-default"
    devices = " dev0,1" if sdk.api["bot"].config.get_val("BOT_TYPE") in {"DEVICE", "QUEUE_DEVICE"} else ""
    assert sdk.transport.commands == [operation + " n1" + devices + " " + (duration or "120m")]
    assert [event["method"] for event in events] == ["GET"] * 6 + ["POST"]
    increment = 14400 if duration else 7200
    assert [row["current_users"][0]["duration"] for row in units] == [value + increment for value in before]
    if operation == "slock":
        assert all(row["current_users"][1]["duration"] == 7200 for row in units)
    assert files == {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}


def test_original_node_hint_group_and_model_constraints_survive_mapping(sdk):
    own_selected(sdk)
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "198.51.100.19"})
    sdk.client.patch(
        f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": [{"model": "P800", "count": 8}]}},
    ).raise_for_status()
    output, events = command(
        sdk,
        "19",
        "lock",
        "--duration",
        "4h",
        "--model",
        "P800",
        "--minimum-model-count",
        "8",
        "--group",
        "resbot-NODE",
        "--whole-node",
    )
    result = output["result"]
    assert result["verified"] and result["resolution"]["matches"] == [
        {"hint": "19", "node": "n1", "match_kinds": ["public_ipv4_alias_octet"]}
    ]
    assert sdk.transport.commands == ["lock n1 4h"]
    assert sum(event["method"] == "POST" for event in events) == 1


@pytest.mark.parametrize("fresh_only", [False, True])
@pytest.mark.parametrize("fault", ["missing-rule", "forbid", "false-string", "false-number", "missing-rules"])
def test_relock_requires_an_explicit_current_false_policy(sdk, fresh_only, fault):
    own_selected(sdk)

    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        if fresh_only and sdk.transport.reads[body["bot_id"]] != 2:
            return
        rules = body["context"]["bot_rules"]
        if fault == "missing-rule":
            rules.pop("forbid_relock")
        elif fault == "missing-rules":
            body["context"].pop("bot_rules")
        else:
            rules["forbid_relock"] = {"forbid": True, "false-string": "false", "false-number": 0}[fault]

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_relock_not_allowed"
    no_post(events)


@pytest.mark.parametrize("value", [None, False, 0, -1, "3600", 1.5])
def test_default_increment_must_be_a_positive_live_integer(sdk, value):
    own_selected(sdk)

    def transform(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["default_duration_seconds"] = value

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", code=2)
    assert output["error"] == "group_default_duration_unavailable"
    no_post(events)


@pytest.mark.parametrize("explicit", [False, True])
def test_default_change_is_pinned_only_when_the_request_uses_it(sdk, explicit):
    own_selected(sdk)

    def transform(path, body):
        if path == "/api/agent/resources" and body["bot_id"] == sdk.bot_ids[0]:
            body["context"]["bot_rules"]["default_duration_seconds"] = (
                10800 if sdk.transport.reads[body["bot_id"]] == 1 else 7200
            )

    sdk.transport.transform = transform
    flags = ("--duration", "1h") if explicit else ()
    output, events = command(sdk, "n1", "lock", *flags, code=0 if explicit else 2)
    if explicit:
        assert output["result"]["verified"] and sdk.transport.commands == ["lock n1 1h"]
    else:
        assert output["error"] == "automatic_relock_changed"
        no_post(events)


def test_explicit_increment_never_requires_a_default(sdk):
    own_selected(sdk)

    def transform(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"].pop("default_duration_seconds")

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", "--duration", "1h")
    assert output["result"]["verified"] and sdk.transport.commands == ["lock n1 1h"]
    assert sum(event["method"] == "POST" for event in events) == 1


@pytest.mark.parametrize("fresh_only", [False, True])
@pytest.mark.parametrize(
    "fault", ["expired", "missing-expiry", "naive-expiry", "zero-remaining", "queued", "duplicate-own"]
)
def test_unverifiable_expired_or_conflicting_leases_cannot_be_renewed(sdk, fresh_only, fault):
    own_selected(sdk, "slock")

    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        if fresh_only and sdk.transport.reads[body["bot_id"]] != 2:
            return
        unit = selection(body)[0]
        holder = unit["holders"][0]
        if fault == "expired":
            holder["expires_at"] = "2000-01-01T00:00:00Z"
        elif fault == "missing-expiry":
            holder.pop("expires_at")
        elif fault == "naive-expiry":
            holder["expires_at"] = "2099-01-01T00:00:00"
        elif fault == "zero-remaining":
            holder["remaining_seconds"] = 0
        elif fault == "queued":
            unit["booking_list"] = [{"external_user_id": "neighbor"}]
        else:
            unit["holders"].append(dict(holder))

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "slock", "--duration", "1h", code=2)
    assert output["error"] in {
        "automatic_lease_unverifiable",
        "automatic_resource_conflict",
        "automatic_relock_selection_conflict",
    }
    no_post(events)


@pytest.mark.parametrize("fault", ["expiry", "start", "lost-own", "mode", "idle", "actor", "stale"])
def test_fresh_lease_replacement_owner_mode_or_age_change_stops(sdk, fault):
    own_selected(sdk)

    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        if sdk.transport.reads[body["bot_id"]] != 2:
            return
        unit = selection(body)[0]
        if fault == "expiry":
            unit["holders"][0]["expires_at"] = "2099-01-01T00:00:00Z"
        elif fault == "start":
            unit["holders"][0]["start_time"] = "2000-01-01T00:00:00Z"
        elif fault == "lost-own":
            unit["holders"][0]["external_user_id"] = "neighbor"
        elif fault == "mode":
            unit["lock_status"] = "shared"
        elif fault == "idle":
            unit.update(lock_status="idle", holders=[])
        elif fault == "actor":
            body["context"]["actor"]["external_user_id"] = "other"
        else:
            body["age_seconds"] = 6

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] in {
        "automatic_relock_changed",
        "automatic_relock_selection_conflict",
        "resource_snapshot_stale_or_unverifiable",
        "context_actor_mismatch_reinitialize_connection_without_rotating_key",
        "automatic_snapshot_stale",
    }
    no_post(events)


@pytest.mark.parametrize("api", ["NODE", "DEVICE"], indirect=True)
@pytest.mark.parametrize("fault", ["idle", "other-owner", "other-mode"])
def test_mixed_owned_selection_is_never_partially_acquired_or_renewed(sdk, fault):
    units = own_selected(sdk, all_nodes=True)
    changed = units[-1]
    if fault == "idle":
        changed.update(status="idle", current_users=[])
    elif fault == "other-owner":
        changed["current_users"][0]["user_id"] = "neighbor"
    else:
        changed["status"] = "shared"
    flags = ("--node", "n2") if sdk.api["bot"].config.get_val("BOT_TYPE") == "NODE" else ()
    output, events = command(sdk, "n1", "lock", *flags, "--duration", "1h", code=2)
    assert output["error"] == "automatic_relock_selection_conflict"
    no_post(events)


def test_renewal_does_not_count_existing_resources_as_new_acquisitions(sdk):
    own_selected(sdk)
    sdk.bots[0].config.set_val("MAX_RESOURCES_PER_USER", 1)

    def limit(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["max_resources_per_user"] = 1
            body["resource_policy"]["claimed_resource_count"] = 1

    sdk.transport.transform = limit
    output, events = command(sdk, "n1", "lock", "--duration", "1h")
    assert output["result"]["verified"]
    assert sdk.transport.commands == ["lock n1 1h"] and sum(row["method"] == "POST" for row in events) == 1


def test_remaining_plus_increment_obeys_the_effective_duration_ceiling(sdk):
    own_selected(sdk)

    def ceiling(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["effective_max_lock_duration_seconds"] = 7200

    sdk.transport.transform = ceiling
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_duration_limit"
    no_post(events)


@pytest.mark.parametrize("fault", ["expiry", "mode", "owner", "default", "forbid", "queue", "ceiling"])
def test_final_callback_rechecks_mapping_conditions_after_raw_model_validation(sdk, monkeypatch, fault):
    own_selected(sdk)
    sdk.client.patch(
        f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": [{"model": "P800", "count": 8}]}},
    ).raise_for_status()
    matches = sdk.module.Client._matches_model
    calls = []
    contexts = []

    read_context = sdk.module.Client._read_context

    def capture_context(client, **kwargs):
        result = read_context(client, **kwargs)
        contexts.append(result)
        return result

    def mutate(unit, *arguments):
        calls.append(True)
        if len(calls) == 2:
            if fault == "expiry":
                unit["holders"][0]["expires_at"] = "2099-01-01T00:00:00Z"
            elif fault == "mode":
                unit["lock_status"] = "shared"
            elif fault == "owner":
                unit["holders"][0]["external_user_id"] = "neighbor"
            elif fault == "queue":
                unit["booking_list"] = [{"external_user_id": "neighbor"}]
            else:
                rule = {
                    "default": "default_duration_seconds",
                    "forbid": "forbid_relock",
                    "ceiling": "effective_max_lock_duration_seconds",
                }[fault]
                contexts[-1]["bot_rules"][rule] = {"default": 3600, "forbid": True, "ceiling": 1}[fault]
        return matches(unit, *arguments)

    monkeypatch.setattr(sdk.module.Client, "_read_context", capture_context)
    monkeypatch.setattr(sdk.module.Client, "_matches_model", staticmethod(mutate))
    output, events = command(sdk, "n1", "lock", "--model", "P800", code=2)
    assert output["error"] in {
        "automatic_relock_changed",
        "automatic_relock_selection_conflict",
        "automatic_relock_not_allowed",
        "automatic_resource_conflict",
        "automatic_duration_limit",
        "renewal_not_allowed_or_not_owned",
    }
    assert len(calls) >= 2
    no_post(events)


def test_polling_and_raw_scoped_lock_do_not_implicitly_renew(sdk):
    units = own_selected(sdk)
    original = units[0]["current_users"][0]["duration"]
    for _ in range(2):
        _, events = sdk.run("resources-all")
        no_post(events)
    output, events = sdk.run(
        "--connection",
        str(sdk.connections[0]),
        "command",
        "--operation",
        "lock",
        "--node",
        "n1",
        "--whole-node",
        "--duration",
        "1h",
        code=2,
    )
    assert output["error"] == "already_owned_use_explicit_renew"
    no_post(events)
    assert units[0]["current_users"][0]["duration"] == original


@pytest.mark.parametrize("value", [1, 59, 61, 3599])
def test_non_minute_default_increment_preserves_exact_live_seconds(sdk, value):
    units = own_selected(sdk)
    before = units[0]["current_users"][0]["duration"]

    def transform(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["default_duration_seconds"] = value

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock")
    assert output["result"]["verified"]
    assert units[0]["current_users"][0]["duration"] == before + value
    assert len(sdk.transport.commands) == 1
    assert sum(event["method"] == "POST" for event in events) == 1


@pytest.mark.parametrize("value", [None, False, "-1", -2])
def test_unknown_duration_ceiling_blocks_automatic_relock(sdk, value):
    own_selected(sdk)

    def transform(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["effective_max_lock_duration_seconds"] = value

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_policy_invalid"
    no_post(events)


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_shared_relock_does_not_enable_unsupported_wire_operation(sdk):
    own_selected(sdk, "slock")
    output, events = command(sdk, "n1", "slock", "--duration", "1h", code=2)
    assert output["error"] == "operation_not_supported_by_bot"
    no_post(events)
