"""Literal native resource discovery through synthetic packaged SDK reads."""

# ruff: noqa: F811 -- imported pytest fixture
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def execute(aggregate, *args, code=0):
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(aggregate.script), *map(str, args)],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == code, result.stderr
    assert len(result.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    assert all(state.key not in result.stdout + result.stderr for state in aggregate.states)
    return json.loads(result.stdout if code == 0 else result.stderr)


def search(aggregate, text, *options, fields="node", limit=20):
    return execute(aggregate, "resource", "list", "--search", text,
                   "--json", fields, "--limit", limit, *options)["result"]


def card(model="P800", *, number=0, owner=None):
    return {
        "id": number, "dev_model": model, "lock_status": "exclusive" if owner else "idle",
        "holders": [{"external_user_id": owner, "remaining_seconds": 3600,
                     "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}] if owner else [],
        "booking_list": [],
    }


def node(name, model="P800", *, alias=None, owner=None):
    return {"id": name, "alias": alias, "devices": [card(model, owner=owner)]}


def test_absent_search_preserves_preexisting_six_key_query_and_digest():
    original = helper.resource_list_query()
    assert original == {
        "nodes": [], "model": None, "fields": None, "limit": 20, "mine": False, "legacy_read": False,
    }
    assert helper.resource_list_query(search=None) == original
    # SHA-256 of the pre-search canonical default query; no optional null key is added.
    old_default_digest = "1dd2177bacee2ad2580ed5f8f17345cb684daab042568b31b10ebf06c65d2ef3"
    assert helper.resource_list_query_digest(original) == old_default_digest


@pytest.mark.parametrize("positive", [False, True])
def test_model_filter_alone_cannot_prove_complete_matching_with_unknown_hardware(aggregate, positive):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    resource = node("mixed" if positive else "unknown", None)
    if positive:
        resource["devices"].append(card("P800", number=1))
    state.extra["resources"] = [resource]
    filtered = execute(aggregate, "resource", "list", "--model", "P800", "--json", "node")["result"]
    assert len(filtered["rows"]) == int(positive)
    assert filtered["unknown_hardware_nodes"] == 1
    assert not filtered["scope_complete"]
    unfiltered = execute(aggregate, "resource", "list", "--json", "node")["result"]
    assert len(unfiltered["rows"]) == 1 and unfiltered["scope_complete"]


def test_query_normalizes_nfkc_casefold_spaces_and_distinct_terms_without_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("search normalization must not open a connection or Key")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "private_bytes", forbidden)
    wide = helper.resource_list_query(search="  Ｐ８００   ＮＯＤＥ０７ p800 ")
    canonical = helper.resource_list_query(search="node07 P800")
    assert wide == canonical and set(wide) == set(helper.resource_list_query()) | {"search"}
    assert helper.resource_list_query_digest(wide) == helper.resource_list_query_digest(canonical)
    eight = helper.resource_list_query(search="h g f e d c b a a b")
    assert eight == helper.resource_list_query(search="a b c d e f g h")
    assert helper.resource_list_query(search="é" * 80)["search"]


@pytest.mark.parametrize("value", ["", "   ", True, ["p800"], "p800\x00", "p800\x1b07", "p800\u200b",
                                    "a b c d e f g h i", "x" * 161, "é" * 81, "ΐ" * 30])
def test_invalid_search_shape_limits_precede_io(monkeypatch, value):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid search must not open a connection or Key")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "private_bytes", forbidden)
    with pytest.raises(helper.ClientError, match="^resource_search_invalid$"):
        helper.resource_list_query(search=value)


def test_model_and_name_terms_are_anded_across_observed_public_fields(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("node07"), node("node08"), node("rack07", "H20"), node("P80007", "H20")]
    page = search(aggregate, "P800 07")
    # A literal ID is a valid fact too, even when its hardware model differs.
    assert {row["node"] for row in page["rows"]} == {"node07", "P80007"}
    literal = search(aggregate, "P80007")
    assert [row["node"] for row in literal["rows"]] == ["P80007"]
    qualified = search(aggregate, "P800 07", "--model", "P800")
    assert [row["node"] for row in qualified["rows"]] == ["node07"]


@pytest.mark.parametrize("text,expected", [
    (".*", ["node.*07"]), ("[blue]", ["node.*07"]), ("a|b", ["literal-pipe"]),
    ("node.+07", []), ("nodeXX07", ["nodeXX07"]),
])
def test_punctuation_is_literal_without_regex_or_edit_distance(aggregate, text, expected):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("node.*07", alias="rack[blue]"), node("nodeXX07"),
                                node("literal-pipe", alias="a|b")]
    page = search(aggregate, text)
    assert [row["node"] for row in page["rows"]] == expected
    assert page["scope_complete"]


def test_public_alias_and_unicode_normalization_do_not_rewrite_exact_ref_id(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("Ｎｏｄｅ０７", alias="共享-GPU"), node("node08")]
    page = search(aggregate, "Ｐ８００ node07")
    assert [row["node"] for row in page["rows"]] == ["Ｎｏｄｅ０７"]
    assert helper.parse_resource_reference(page["rows"][0]["ref"])[1] == "Ｎｏｄｅ０７"
    alias = search(aggregate, "共享-gpu")
    assert alias["rows"][0]["ref"] == page["rows"][0]["ref"]


def test_duplicate_names_and_aliases_across_sources_stay_distinct(aggregate):
    states = [aggregate.server(1), aggregate.server(2)]
    for number, state in enumerate(states):
        state.bot_type = "DEVICE"
        aggregate.record(state, str(number))
        state.extra["resources"] = [node("node07", alias="shared")]
    page = search(aggregate, "shared p800")
    assert [row["node"] for row in page["rows"]] == ["node07", "node07"]
    assert len({row["ref"] for row in page["rows"]}) == 2
    assert page["scope_complete"]


def test_search_combines_with_exact_node_model_and_mine_before_fields(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    actor = state.actor["external_user_id"]
    state.extra["resources"] = [node("node07", owner=actor), node("node08", owner=actor),
                                node("rack07", "H20", owner=actor), node("free07")]
    page = search(aggregate, "p800 07", "--node", "node07", "--model", "P800", "--mine", fields="holding_units")
    assert len(page["rows"]) == 1 and page["rows"][0]["holding_units"] == 1
    assert set(page["rows"][0]) == {"ref", "holding_units"}
    none = search(aggregate, "p800 07", "--node", "free07", "--model", "P800", "--mine")
    assert none["rows"] == []


def test_cursor_binds_search_and_next_command_preserves_canonical_query(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node(f"node{number:02}") for number in range(4)]
    first = search(aggregate, "Ｐ８００ node", limit=1)
    command = first["next_command"]
    assert "--search" in command and first["next_cursor"]
    next_text = command[command.index("--search") + 1]
    assert helper.resource_search_text(next_text) == helper.resource_search_text("p800 node")
    second = execute(aggregate, *command)["result"]
    assert first["rows"][0]["ref"] != second["rows"][0]["ref"]
    state.events.clear()
    rejected = execute(aggregate, "resource", "list", "--search", "p800 node02",
                       "--json", "node", "--limit", 1, "--cursor", first["next_cursor"], code=2)
    assert rejected["error"] == "resource_list_cursor_query_changed" and state.events == []
    equivalent = search(aggregate, "NODE p800 p800", "--cursor", first["next_cursor"], limit=1)
    assert equivalent["rows"][0]["ref"] == second["rows"][0]["ref"]


@pytest.mark.parametrize("extra", [[], ["--model", "P800"]])
def test_missing_hardware_keeps_negative_search_coverage_partial(aggregate, extra):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("unknown-node", None)]
    page = search(aggregate, "P800", *extra, fields="ref")
    assert page["rows"] == [] and not page["scope_complete"]
    assert page["search_unknown_nodes"] == 1


def test_alias_positive_match_does_not_require_inventing_unknown_hardware(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("unknown-node", None, alias="blue-gpu")]
    page = search(aggregate, "blue-gpu")
    assert len(page["rows"]) == 1 and page["search_unknown_nodes"] == 0
    assert page["scope_complete"]


@pytest.mark.parametrize("needle", ["credentialneedle", "ownerneedle", "taskneedle", "group"])
def test_credentials_owners_tasks_and_group_labels_are_not_search_facts(aggregate, needle):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    state.key = "lbr_" + "credentialneedle" * 3
    aggregate.record(state, "scope")
    row = node("node07", owner="ownerneedle")
    row.update(purpose="taskneedle", task="taskneedle")
    row["devices"][0]["holders"][0]["purpose"] = "taskneedle"
    state.extra["resources"] = [row]
    page = search(aggregate, needle)
    assert page["rows"] == [] and page["scope_complete"] and page["search_unknown_nodes"] == 0


@pytest.mark.parametrize("allocation", ["DEVICE", "NODE"])
def test_search_uses_models_beyond_twenty_displayed_hardware_entries(aggregate, allocation):
    state = aggregate.server(1)
    state.bot_type = allocation
    aggregate.record(state, "scope")
    models = [f"ACC{number:02}" for number in range(20)] + ["P800"]
    if allocation == "DEVICE":
        row = {"id": "node07", "devices": [card(model, number=number) for number, model in enumerate(models)]}
    else:
        row = {"id": "node07", "holders": [], "booking_list": [], "lock_status": "idle",
               "accelerators": [{"model": model, "count": 1} for model in models]}
    state.extra["resources"] = [row]
    page = search(aggregate, "P800", fields="node,hardware,hardware_has_more")
    assert len(page["rows"]) == 1
    assert len(page["rows"][0]["hardware"]) == 20 and page["rows"][0]["hardware_has_more"]
    assert "P800" not in {item["model"] for item in page["rows"][0]["hardware"]}


def test_exact_view_does_not_accept_search_or_start_io(aggregate):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    ref = helper.resource_reference(json.loads(path.read_text()), "same-node", width=64)
    (path.parent / "key").unlink()
    rejected = execute(aggregate, "resource", "view", ref, "--search", "p800", code=2)
    assert rejected["error"] == "invalid_cli_arguments" and state.events == []


def test_undisplayed_alias_tail_keeps_conflicting_same_scope_credentials_separate(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    original = state.key
    aggregate.record(state, "first")
    second = "lbr_" + "second-search-credential" * 3
    state.key = second
    aggregate.record(state, "second")
    state.key, state.accepted_keys = original, [second]
    common = "a" * 160
    first_row = node("node07", alias=common + "wanted-tail")
    state.extra["resources"] = [first_row]
    state.extra_per_key = {
        "Bearer " + second: {"resources": [node("node07", alias=common + "different-tail")]},
    }
    page = search(aggregate, "wanted-tail", fields="node,alias,hardware,states")
    assert len(page["rows"]) == 1 and set(page["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert page["rows"][0]["observation_conflict"] and not page["scope_complete"]
    assert page["conflicting_observations"] == 1 and len(state.events) == 3


def test_exact_node_excludes_unrelated_unknown_hardware_from_search_coverage(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    state.extra["resources"] = [node("selected", "H20"), node("unrelated", None)]
    page = search(aggregate, "P800", "--node", "selected", "--model", "P800", fields="ref")
    assert page["rows"] == [] and page["scope_complete"]
    assert page["search_unknown_nodes"] == 0


def test_hidden_model_tail_conflict_cannot_confirm_matching_hardware(aggregate):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    original = state.key
    aggregate.record(state, "first")
    second = "lbr_" + "second-model-credential" * 3
    state.key = second
    aggregate.record(state, "second")
    state.key, state.accepted_keys = original, [second]
    common = [card(f"ACC{number:02}", number=number) for number in range(20)]
    state.extra["resources"] = [{"id": "node07", "devices": [*common, card("P800", number=20)]}]
    state.extra_per_key = {
        "Bearer " + second: {"resources": [{"id": "node07", "devices": [*common, card("H20", number=20)]}]},
    }
    page = search(aggregate, "P800", fields="node,hardware,states")
    assert len(page["rows"]) == 1 and set(page["rows"][0]) == {"ref", "node", "observation_conflict"}
    assert page["rows"][0]["observation_conflict"] and not page["scope_complete"]
    assert page["conflicting_observations"] == 1 and len(state.events) == 3


@pytest.mark.parametrize("separator", ["\t", "\n", "\u00a0", "\u3000"])
def test_unicode_and_control_whitespace_share_canonical_search_digest(separator):
    query = helper.resource_list_query(search=f"ＮＯＤＥ０７{separator}Ｐ８００")
    canonical = helper.resource_list_query(search="p800 node07")
    assert query == canonical
    assert helper.resource_list_query_digest(query) == helper.resource_list_query_digest(canonical)


@pytest.mark.parametrize("wide", [False, True])
def test_token_shaped_echoed_model_is_not_a_search_oracle(aggregate, wide):
    state = aggregate.server(1)
    state.bot_type = "DEVICE"
    aggregate.record(state, "scope")
    model = state.key
    if wide:
        model = "".join(chr(ord(char) + 0xFEE0) if 33 <= ord(char) <= 126 else char for char in state.key)
    state.extra["resources"] = [node("node07", model)]
    page = search(aggregate, "11111111")
    assert page["rows"] == [] and page["search_unknown_nodes"] == 1
    assert not page["scope_complete"]
