"""Bounded device pages through the packaged CLI; synthetic localhost sources only."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def cards(count, *, offset=0):
    return [
        {
            "id": number + offset,
            "dev_model": "P800" if number % 2 else "H20",
            "lock_status": "idle",
            "holders": [],
            "booking_list": [],
        }
        for number in range(count)
    ]


def invoke(aggregate, *arguments, code=0, text=False):
    process = subprocess.run(
        [sys.executable, "-I", "-S", str(aggregate.script), *map(str, arguments)],
        env={**os.environ, "LOCKBOT_HOME": str(aggregate.home)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == code, process.stderr
    assert all(state.key not in process.stdout + process.stderr for state in aggregate.states)
    assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
    return process.stdout if text else json.loads(process.stderr if code else process.stdout)


def setup(aggregate, *, count=200):
    state = aggregate.server(1)
    state.bot_type = "QUEUE_DEVICE"
    path = aggregate.record(state, "first")
    state.extra["resources"] = [{"id": "node07", "devices": cards(count)}]
    ref = helper.resource_reference(json.loads(path.read_text()), "node07", width=64)
    return state, path, ref


def page(aggregate, reference, *arguments, fields="id,model,state", limit=20):
    return invoke(aggregate, "resource", "devices", reference, "--json", fields, "--limit", limit, *arguments)["result"]


def test_two_hundred_cards_traverse_in_numeric_order_without_cache_writes(aggregate):
    state, _path, ref = setup(aggregate)
    peer = aggregate.server(2)
    aggregate.record(peer, "second")
    before = {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}
    result, found = page(aggregate, ref), []
    for _ in range(12):
        assert result["node_ref"] == ref and result["unit"] == "card"
        assert result["total_devices"] == result["matched_devices"] == 200
        assert result["http_requests"] == 2 and result["displayed_devices"] == 20
        found.extend(row["id"] for row in result["rows"])
        if not result["has_more"]:
            break
        result = invoke(aggregate, *result["next_command"])["result"]
    assert found == list(range(200)) and not result["scope_complete"]
    assert not peer.events and len(state.events) == 20
    assert before == {str(path): path.read_bytes() for path in aggregate.home.rglob("*") if path.is_file()}


def test_explicit_compact_fields_can_return_all_two_hundred_ids_once(aggregate):
    _state, _path, ref = setup(aggregate)
    result = page(aggregate, ref, fields="id,model", limit=200)
    assert len(result["rows"]) == 200 and result["scope_complete"] and not result["has_more"]
    assert result["http_requests"] == 2


def test_one_thousand_card_ids_take_four_bounded_compact_pages(aggregate):
    state, _path, ref = setup(aggregate, count=1000)
    result, found = page(aggregate, ref, fields="id", limit=256), []
    for _ in range(4):
        found.extend(row["id"] for row in result["rows"])
        assert result["total_devices"] == result["matched_devices"] == 1000
        if not result["has_more"]:
            break
        result = invoke(aggregate, *result["next_command"])["result"]
    assert found == list(range(1000)) and not result["has_more"]
    assert len(state.events) == 8


def test_model_filter_keeps_sparse_exact_ids_and_does_not_renumber(aggregate):
    _state, _path, ref = setup(aggregate)
    result = page(aggregate, ref, "--model", "p800", fields="id,model", limit=200)
    assert [row["id"] for row in result["rows"]] == list(range(1, 200, 2))
    assert result["total_devices"] == 200 and result["matched_devices"] == 100


@pytest.mark.parametrize("change", ["model", "mine", "fields", "limit", "node", "key_path"])
def test_changed_cursor_query_or_target_is_rejected_before_http(aggregate, change):
    state, path, ref = setup(aggregate)
    first = page(aggregate, ref)
    state.events.clear()
    args = ["resource", "devices", ref, "--json", "id,model,state", "--limit", "20", "--cursor", first["next_cursor"]]
    if change == "model":
        args += ["--model", "P800"]
    elif change == "mine":
        args += ["--mine"]
    elif change == "fields":
        args[4] = "id"
    elif change == "limit":
        args[6] = "10"
    elif change == "node":
        args[2] = helper.resource_reference(json.loads(path.read_text()), "other-node", width=64)
    else:
        config = json.loads(path.read_text())
        config["key_file"] = "missing"
        path.write_text(json.dumps(config))
    result = invoke(aggregate, *args, code=2)
    expected = (
        "resource_devices_cursor_target_changed"
        if change in {"node", "key_path"}
        else "resource_devices_cursor_query_changed"
    )
    assert result["error"] == expected and not state.events


def test_bare_field_discovery_without_a_reference_reads_nothing(aggregate):
    state, path, _ref = setup(aggregate)
    (path.parent / "key").unlink()
    result = invoke(aggregate, "resource", "devices", "--json")
    assert {"id", "model", "held_by_you", "booked_by_you"} <= set(result["json_fields"])
    assert result["required_row_fields"] == ["id"] and not state.events


@pytest.mark.parametrize(
    "arguments", [["--limit", "0"], ["--json", "holders"], ["--model", "x" * 81], ["--cursor", "rl1.wrong-kind"]]
)
def test_bad_arguments_never_open_a_key(aggregate, arguments):
    state, path, ref = setup(aggregate)
    (path.parent / "key").unlink()
    invoke(aggregate, "resource", "devices", ref, *arguments, code=2)
    assert not state.events


def test_duplicate_ids_are_not_hidden_by_model_filter(aggregate):
    state, _path, ref = setup(aggregate, count=2)
    state.extra["resources"][0]["devices"][1]["id"] = "0"
    result = invoke(aggregate, "resource", "devices", ref, "--model", "P800", code=2)
    assert result["error"] == "device_inventory_identity_ambiguous"
    assert all(method == "GET" for _path, method, _auth in state.events)


def test_whole_node_named_dev0_is_not_an_allocatable_card(aggregate):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    state.extra["resources"] = [{"id": "dev0", "lock_status": "idle", "holders": [], "booking_list": []}]
    ref = helper.resource_reference(json.loads(path.read_text()), "dev0", width=64)
    result = invoke(aggregate, "resource", "devices", ref, code=2)
    assert result["error"] == "resource_uses_whole_nodes"


def test_mine_keeps_unknown_ownership_separate_from_known_absence(aggregate):
    state, _path, ref = setup(aggregate, count=3)
    units = state.extra["resources"][0]["devices"]
    units[0].update(lock_status="exclusive", holders=[{"external_user_id": state.actor["external_user_id"]}])
    units[1].pop("holders")
    result = page(aggregate, ref, "--mine", fields="id,held_by_you,booked_by_you")
    assert [row["id"] for row in result["rows"]] == [0, 1]
    assert result["rows"][0]["held_by_you"] is True
    assert result["rows"][1]["held_by_you"] is None and result["rows"][1]["ownership_match_unknown"]
    assert result["matched_devices"] is None and not result["scope_complete"]


def test_live_resume_handles_deleted_boundary_and_new_ids_after_it(aggregate):
    state, _path, ref = setup(aggregate, count=10)
    first = page(aggregate, ref, fields="id", limit=3)
    assert [row["id"] for row in first["rows"]] == [0, 1, 2]
    state.extra["resources"][0]["devices"] = cards(8, offset=3)
    second = invoke(aggregate, *first["next_command"])["result"]
    assert [row["id"] for row in second["rows"]] == [3, 4, 5] and not second["scope_complete"]


def test_explicit_connection_continuation_preserves_selected_credential(aggregate):
    state, path, ref = setup(aggregate, count=30)
    original = state.key
    state.key = "lbr_" + "secondmaterial" * 5
    aggregate.record(state, "same-scope-peer")
    state.accepted_keys, state.key = [state.key], original
    result = invoke(aggregate, "--connection", path, "resource", "devices", ref, "--json", "id")["result"]
    assert result["next_command"][:2] == ["--connection", str(path)]
    tail = invoke(aggregate, *result["next_command"])["result"]
    assert [row["id"] for row in tail["rows"]] == list(range(20, 30))
    assert all(auth is None or auth == "Bearer " + original for _path, _method, auth in state.events)


def test_default_table_reports_cards_without_dumping_owner_rows(aggregate):
    _state, _path, ref = setup(aggregate, count=3)
    result = invoke(aggregate, "resource", "devices", ref, text=True)
    assert "ID  MODEL  STATE" in result and "P800" in result
    assert "external_user_id" not in result


def test_encoded_secret_position_is_rejected_before_opening_a_key(aggregate):
    import base64

    state, path, ref = setup(aggregate)
    first = page(aggregate, ref)
    token = first["next_cursor"]
    raw = token[4:]
    payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    unsafe = (
        "dc1." + helper.resource_list_cursor(payload[1], payload[2], 0, "lbr_" + "synthetic-private-position" * 2)[4:]
    )
    state.events.clear()
    (path.parent / "key").unlink()
    arguments = first["next_command"][:-1] + [unsafe]
    result = invoke(aggregate, *arguments, code=2)
    assert result["error"] == "resource_devices_cursor_invalid" and not state.events


def test_aging_rebuilds_mine_prefix_instead_of_skipping_newly_unknown_ids(aggregate, monkeypatch):
    state, path, ref = setup(aggregate, count=3)
    state.extra["resources"][0]["devices"][2].update(
        lock_status="exclusive", holders=[{"external_user_id": state.actor["external_user_id"]}]
    )
    now, count = [0], [0]
    original = helper.device_summary_rows

    def aging(*args, **kwargs):
        result = original(*args, **kwargs)
        count[0] += 1
        if count[0] == 1:
            now[0] = 6
        return result

    monkeypatch.setattr(helper, "device_summary_rows", aging)
    client = helper.Client(path, root=aggregate.script.parents[1], clock=lambda: now[0])
    query = helper.resource_devices_query(mine=True, fields=["id", "held_by_you"], limit=1)
    result = json.loads(helper.resource_devices(client, ref, query=query))["result"]
    assert result["rows"][0]["id"] == 0 and result["rows"][0]["ownership_match_unknown"]
    assert result["rows"][0]["held_by_you"] is None and result["has_more"]
    assert not result["snapshot_fresh"] and result["matched_devices"] is None


def test_windows_ascii_framing_keeps_unicode_ids_bounded_and_resumable(monkeypatch):
    context = {
        "server": {"instance_id": "synthetic-instance"},
        "bot": {"uuid": "synthetic-bot", "type": "DEVICE"},
        "actor": {"platform": "infoflow", "external_user_id": "synthetic-subject"},
    }
    node = "节点" * 64
    snapshot = {
        "context": context,
        "age_seconds": 0,
        "health": "running",
        "snapshot_fresh_for_write": True,
        "resources": [
            {"id": node, "devices": [{**card, "id": "卡" * 128 + str(number)} for number, card in enumerate(cards(12))]}
        ],
    }
    config = {
        "approved_origin": "http://127.0.0.1:8002",
        "instance_id": "synthetic-instance",
        "bot_uuid": "synthetic-bot",
        "actor": context["actor"],
        "key_file": "synthetic-key-path",
    }
    client = SimpleNamespace(
        config=config,
        connection_dir=Path("/synthetic/private"),
        node_access=None,
        inventory=lambda: deepcopy(snapshot),
        clock=lambda: 0,
        _resource_request_started=0,
        secrets=[],
        calls=2,
        api_profile="modern",
        needs_update=False,
        redacted=lambda value: helper.redacted_values(value),
    )
    ref = helper.resource_reference(config, node, width=64)
    query = helper.resource_devices_query(fields=["id", "model"], limit=20)
    monkeypatch.setattr(helper, "os", SimpleNamespace(name="nt"))
    cursor, found = None, []
    for _ in range(16):
        encoded = helper.resource_devices(client, ref, query=query, cursor=cursor)
        assert encoded.isascii() and len(encoded.encode()) + 1 <= helper.SUMMARY_BYTE_LIMIT
        result = json.loads(encoded)["result"]
        found.extend(row["id"] for row in result["rows"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(found) == len(set(found)) == 12
