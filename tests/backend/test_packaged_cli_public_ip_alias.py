"""Public IPv4 labels select resources without becoming node-access authority."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.

import pytest

from backend.test_packaged_cli_auto_scope import command, no_post, sdk  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


def aliases(sdk, first="198.51.100.19", second="192.0.2.119"):
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": first, "n2": second})
    sdk.bots[1].config.set_val("NODE_ALIASES", {"other": second})


@pytest.mark.parametrize("api", ["NODE", "QUEUE"], indirect=True)
@pytest.mark.parametrize("hint", ["19", "198.51.100.19"])
def test_public_ipv4_alias_selects_idle_machine_once_with_live_default(sdk, hint):
    aliases(sdk)
    before = {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}
    output, events = command(sdk, hint, "lock", "--exact-lease-times")
    result = output["result"]
    assert result["verified"] and result["duration_source"] == "group-default"
    assert result["resolution"]["selection_checked_before_post"] is True
    assert result["resolution"]["matches"] == [
        {"hint": hint, "node": "n1", "match_kinds": ["alias" if "." in hint else "public_ipv4_alias_octet"]}
    ]
    assert [event["method"] for event in events] == ["GET"] * 6 + ["POST"]
    assert sdk.transport.commands == ["lock n1"]
    assert before == {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}
    observed = result["verified_snapshot"]
    node = next(item for item in observed["resources"] if item["id"] == "n1")
    assert node["alias"] == "198.51.100.19" and node["holders"]
    assert "node_access" not in node and sdk.module._automatic_addresses(node) == set()
    client = sdk.module.Client(sdk.connections[0])
    assert client._current_node_access(observed, observed["context"], 0) == {}


def test_read_only_ipv4_alias_does_not_create_access_metadata_or_cache(sdk):
    aliases(sdk)
    before = {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}
    output, events = sdk.run("resources-all")
    no_post(events)
    node = output["result"]["groups"][0]["snapshot"]["resources"][0]
    assert node["alias"] == "198.51.100.19" and "node_access" not in node
    assert sdk.module._automatic_selector_evidence(node, "19") == (["public_ipv4_alias_octet"], "198.51.100.19", ())
    assert sdk.module._automatic_addresses(node) == set()
    assert before == {str(path): path.read_bytes() for path in sdk.catalog.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "alias",
    [
        "192.0.2.119",
        "198.51.100.019",
        "256.51.100.19",
        "198.51.100.19:22",
        "198.51.100.19/24",
        "host19",
        "node-198.51.100.19",
        "198.51.100.19 backup",
        " 198.51.100.19",
        "198.51.100.19 ",
        "::ffff:198.51.100.19",
    ],
)
def test_only_exact_canonical_ipv4_labels_supply_an_octet_selector(sdk, alias):
    aliases(sdk, first=alias)
    output, events = command(sdk, "19", "lock", code=2)
    assert output["error"] == "automatic_target_missing"
    assert output["command_sent"] is False
    no_post(events)


@pytest.mark.parametrize("hint", ["019", "256", "-19", "19.0", "198.51.100"])
def test_ipv4_alias_never_accepts_approximate_or_partial_hints(sdk, hint):
    aliases(sdk)
    output, events = command(sdk, hint, "lock", code=2)
    assert output["error"] == "automatic_target_missing"
    no_post(events)


@pytest.mark.parametrize("collision", ["same_scope", "other_scope", "exact_node_id"])
def test_ipv4_alias_collisions_always_clarify_before_any_post(sdk, collision):
    aliases(sdk)
    if collision == "same_scope":
        sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "198.51.100.19", "n2": "192.0.2.19"})
    elif collision == "other_scope":
        sdk.bots[1].config.set_val("NODE_ALIASES", {"other": "192.0.2.19"})
    else:
        sdk.register({"19": "exact-id-machine"})
    output, events = command(sdk, "19", "lock", code=2)
    assert output["error"] == "automatic_target_ambiguous"
    assert output["clarification"]["candidate_count"] == 2
    assert output["retry_mutation"] is False and output["command_sent"] is False
    no_post(events)


@pytest.mark.parametrize("change", ["same_octet_new_ip", "new_octet", "remove", "duplicate", "move", "actor"])
def test_fresh_public_alias_binding_and_identity_are_rechecked(sdk, change):
    aliases(sdk)

    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        if sdk.transport.reads[body["bot_id"]] != 2:
            return
        node, other = body["resources"]
        if change == "same_octet_new_ip":
            node["alias"] = "203.0.113.19"
        elif change == "new_octet":
            node["alias"] = "198.51.100.20"
        elif change == "remove":
            node["alias"] = None
        elif change == "duplicate":
            other["alias"] = "192.0.2.19"
        elif change == "move":
            node["alias"], other["alias"] = other["alias"], node["alias"]
        else:
            body["context"]["actor"]["external_user_id"] = "different-actor"

    sdk.transport.transform = transform
    output, events = command(sdk, "19", "lock", code=2)
    assert output["error"] in {
        "automatic_target_changed",
        "context_actor_mismatch_reinitialize_connection_without_rotating_key",
    }
    no_post(events)


def test_last_dispatch_hook_rechecks_alias_even_when_its_octet_still_matches(sdk, monkeypatch):
    aliases(sdk)
    sdk.client.patch(
        f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": [{"model": "P800", "count": 2}]}},
    ).raise_for_status()
    matches = sdk.module.Client._matches_model
    calls = []

    def changed(item, *arguments):
        calls.append(True)
        if len(calls) == 2:
            item["alias"] = "203.0.113.19"
        return matches(item, *arguments)

    monkeypatch.setattr(sdk.module.Client, "_matches_model", staticmethod(changed))
    output, events = command(sdk, "19", "lock", "--model", "P800", code=2)
    assert len(calls) == 2 and output["error"] == "automatic_target_changed"
    no_post(events)


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
@pytest.mark.parametrize("fresh_only", [False, True])
def test_public_alias_does_not_make_a_missing_queue_safe(sdk, fresh_only):
    aliases(sdk)

    def transform(path, body):
        if (
            path == "/api/agent/resources"
            and body["bot_id"] == sdk.bot_ids[0]
            and (not fresh_only or sdk.transport.reads[body["bot_id"]] == 2)
        ):
            body["resources"][0].pop("booking_list")

    sdk.transport.transform = transform
    output, events = command(sdk, "19", "lock", code=2)
    assert output["error"] == "automatic_inventory_invalid"
    no_post(events)


def test_public_alias_preserves_required_model_and_explicit_group(sdk):
    aliases(sdk)
    output, events = command(sdk, "19", "lock", "--model", "P800", code=2)
    assert output["error"] == "selected_resource_model_unverified_or_insufficient"
    no_post(events)
    output, events = command(sdk, "19", "lock", "--group", "different-group", code=2)
    assert output["error"] == "automatic_target_missing"
    no_post(events)


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_public_alias_never_converts_an_explicit_whole_machine_request_to_cards(sdk):
    aliases(sdk)
    output, events = command(sdk, "19", "lock", "--whole-node", code=2)
    assert output["error"] == "automatic_resource_mode_mismatch"
    no_post(events)
