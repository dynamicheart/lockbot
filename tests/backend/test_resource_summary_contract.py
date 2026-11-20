"""Opt-in summaries from the real standalone package preserve resource meaning."""

import copy
import importlib.util
import io
import json
from urllib.parse import quote
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill


@pytest.fixture
def packaged(tmp_path):
    with ZipFile(io.BytesIO(skill.installation_bundle())) as bundle:
        bundle.extractall(tmp_path)
    path = tmp_path / "lockbot-agent/scripts/lockbot_cli.py"
    spec = importlib.util.spec_from_file_location("summary_contract_packaged", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ref(node):
    return "s-example/" + quote(node, safe="")


def card(identifier, model, *, state="idle", holders=None, queue=None):
    return {
        "id": identifier,
        "dev_model": model,
        "lock_status": state,
        "holders": [] if holders is None else holders,
        "booking_list": [] if queue is None else queue,
    }


def observation(rows, *, kind="DEVICE", fresh=True):
    return {
        "resources": rows,
        "health": "running",
        "snapshot_fresh_for_write": fresh,
        "context": {"bot": {"type": kind, "name": "Unhelpful duplicate name"}},
    }


def project(packaged, snapshot, **options):
    return packaged.node_summary_rows(snapshot, reference_factory=ref, **options)


def test_mixed_model_projection_selects_real_ids_and_separates_total_hardware(packaged):
    snapshot = observation(
        [{"id": "node07", "devices": [card(0, "A30"), card(2, "A100"), card(5, "a100"), card(9, "3090")]}]
    )
    before = copy.deepcopy(snapshot)
    result = project(packaged, snapshot, model="A100")
    (row,) = result["rows"]
    assert row["total_cards"] == row["total_units"] == 4
    assert row["matched_cards"] == row["matched_units"] == 2
    assert row["matched_device_ids"] == row["idle_device_ids"] == [2, 5]
    assert row["states"] == {"idle": 2, "busy": 0, "shared": 0, "queued": 0, "unknown": 0}
    assert row["hardware"] == [
        {"model": "3090", "count": 1},
        {"model": "A100", "count": 2},
        {"model": "A30", "count": 1},
    ]
    assert snapshot == before


def test_zero_id_and_unknown_models_remain_distinct(packaged):
    snapshot = observation([{"id": "node7", "devices": [card(0, "P800"), card(7, None)]}])
    (row,) = project(packaged, snapshot, model="p800")["rows"]
    assert row["matched_device_ids"] == [0] and type(row["matched_device_ids"][0]) is int
    assert row["hardware_incomplete"] is True and row["total_cards"] == 2 and row["matched_cards"] == 1
    unknown = project(packaged, observation([{"id": "named-P800", "devices": [card(0, None)]}]), model="P800")
    assert unknown["rows"] == [] and unknown["unknown_match_nodes"] == 1


def test_queued_shared_busy_and_unknown_are_not_idle_candidates(packaged):
    holder = [{"external_user_id": "synthetic-peer"}]
    queue = [{"external_user_id": "synthetic-next"}]
    cards = [
        card(0, "P800"),
        card(2, "P800", queue=queue),
        card(5, "P800", state="shared", holders=holder),
        card(7, "P800", state="exclusive", holders=holder),
        card(9, "P800", state="idle", holders=holder),
    ]
    (row,) = project(packaged, observation([{"id": "node07", "devices": cards}], kind="QUEUE_DEVICE"))["rows"]
    assert row["idle_device_ids"] == [0]
    assert row["states"] == {"idle": 1, "busy": 1, "shared": 1, "queued": 1, "unknown": 1}


@pytest.mark.parametrize("fresh,health", [(False, "running"), (True, "stopped"), (True, None)])
def test_stale_or_unhealthy_observations_do_not_show_idle(packaged, fresh, health):
    snapshot = observation([{"id": "node07", "devices": [card(0, "P800")]}], fresh=fresh)
    snapshot["health"] = health
    (row,) = project(packaged, snapshot)["rows"]
    assert row["snapshot_fresh"] is False and row["idle_device_ids"] == []
    assert row["states"]["unknown"] == 1


def test_dev0_whole_node_has_no_invented_card_ids(packaged):
    snapshot = observation(
        [
            {
                "id": "dev0",
                "lock_status": "idle",
                "holders": [],
                "booking_list": [],
                "accelerators": [{"model": "P800", "count": 6}, {"model": "P900", "count": 2}],
            }
        ],
        kind="QUEUE",
    )
    (row,) = project(packaged, snapshot, model="P800")["rows"]
    assert row["unit"] == "whole_node" and row["total_units"] == row["matched_units"] == 1
    assert row["total_cards"] == 8 and row["matched_cards"] == 6
    assert "matched_device_ids" not in row and row["states"]["idle"] == 1


def test_whole_node_name_cannot_supply_hardware(packaged):
    snapshot = observation([{"id": "P800", "alias": "P800", "lock_status": "idle", "holders": []}], kind="NODE")
    result = project(packaged, snapshot, model="P800")
    assert result["rows"] == [] and result["unknown_match_nodes"] == 1
    (row,) = project(packaged, snapshot)["rows"]
    assert row["total_cards"] is None and row["hardware_incomplete"]


@pytest.mark.parametrize("devices", [None, {}, "not a list"])
def test_unreported_device_inventory_is_unknown_not_zero(packaged, devices):
    snapshot = observation([{"id": "node07", "devices": devices}])
    (row,) = project(packaged, snapshot)["rows"]
    assert row["total_cards"] is row["matched_cards"] is row["total_units"] is None
    assert row["device_inventory_complete"] is False
    assert set(row["states"].values()) == {None}


def test_duplicate_node_ids_are_not_merged_or_assigned_one_reference(packaged):
    result = project(packaged, observation([{"id": "node07", "devices": []}, {"id": "node07", "devices": []}]))
    assert result["rows"] == [] and result["unidentified_nodes"] == 2
    assert result["projection_complete"] is False


def test_ambiguous_device_ids_cannot_supply_candidates_or_exact_counts(packaged):
    (row,) = project(packaged, observation([{"id": "node07", "devices": [card(0, "P800"), card("0", "P800")]}]))["rows"]
    assert row["matched_device_ids"] == row["idle_device_ids"] == []
    assert row["unidentified_devices"] == 2 and row["device_inventory_complete"] is False
    assert row["total_cards"] is row["matched_cards"] is None
    assert row["devices_has_more"] is False


def test_candidate_and_hardware_arrays_are_bounded(packaged):
    cards = [card(index, "P800") for index in range(10000)]
    (row,) = project(packaged, observation([{"id": "node07", "devices": cards}]))["rows"]
    assert row["total_cards"] == row["matched_cards"] == 10000
    assert len(row["matched_device_ids"]) == len(row["idle_device_ids"]) == 20
    assert row["devices_has_more"] and row["idle_devices_has_more"]


def test_reference_callback_preserves_exact_node_id_and_no_zero_normalization(packaged):
    result = project(packaged, observation([{"id": "node7", "devices": []}, {"id": "node07", "devices": []}]))
    assert [(row["node"], row["ref"]) for row in result["rows"]] == [("node7", ref("node7")), ("node07", ref("node07"))]


def page_bytes(page):
    return len(json.dumps(page, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


@pytest.mark.parametrize("size", [1, 1000, 10000])
def test_page_parent_count_bytes_and_stable_order(packaged, size):
    rows = [{"ref": f"s-test/node{index:05d}", "node": f"node{index:05d}"} for index in range(size)]
    result = packaged.summary_page(rows)
    assert result["displayed_nodes"] == min(size, 20) and page_bytes(result) <= 8192
    assert result["has_more"] is (size > 20) and result["scope_complete"] is True
    assert result["page_kind"] == "local_observation_projection"
    assert packaged.summary_page(list(reversed(rows))) == result


def test_byte_bound_keeps_contiguous_prefix_and_next_offset(packaged):
    rows = [{"ref": f"s-test/node{index:03d}", "node": "节" * 256} for index in range(50)]
    original, seen, offset = copy.deepcopy(rows), [], 0
    for _ in range(len(rows) + 1):
        result = packaged.summary_page(rows, offset=offset)
        assert page_bytes(result) <= 8192
        seen.extend(row["ref"] for row in result["rows"])
        if not result["has_more"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    else:
        pytest.fail("projection continuation looped")
    assert seen == [row["ref"] for row in rows] and len(seen) == len(set(seen))
    assert rows == original


def test_single_oversized_row_retains_reference_and_does_not_skip_following_row(packaged):
    rows = [{"ref": "s-test/a", "description": "x" * 10000}, {"ref": "s-test/b", "node": "small"}]
    result = packaged.summary_page(rows)
    assert [row["ref"] for row in result["rows"]] == ["s-test/a", "s-test/b"]
    assert result["rows"][0] == {"ref": "s-test/a", "detail_deferred": True, "reason": "row_exceeds_output_budget"}
    assert result["has_more"] is False and result["detail_deferred_nodes"] == 1


def test_output_mutation_cannot_rewrite_projection_input(packaged):
    rows = [{"ref": "s-test/a", "nested": {"ids": [0, 2]}}]
    result = packaged.summary_page(rows)
    result["rows"][0]["nested"]["ids"].append(5)
    assert rows[0]["nested"]["ids"] == [0, 2]


@pytest.mark.parametrize("count,more", [(1, False), (30, True)])
def test_scope_gaps_and_display_paging_are_independent(packaged, count, more):
    result = packaged.summary_page([{"ref": f"s-test/{i}"} for i in range(count)], scope_complete=False)
    assert result["scope_complete"] is False and result["has_more"] is more
    assert result["matched_nodes"] is None


@pytest.mark.parametrize(
    "options", [{"limit": 0}, {"limit": True}, {"offset": -1}, {"byte_limit": 500}, {"scope_complete": "true"}]
)
def test_invalid_page_parameters_fail_without_echoing_input(packaged, options):
    with pytest.raises(packaged.ClientError, match="^invalid_summary_page$"):
        packaged.summary_page([], **options)


def test_duplicate_page_references_are_not_silently_deduplicated(packaged):
    with pytest.raises(packaged.ClientError, match="^duplicate_summary_reference$"):
        packaged.summary_page([{"ref": "s-test/a"}, {"ref": "s-test/a"}])


def test_placeholder_with_an_oversized_reference_fails_explicitly(packaged):
    with pytest.raises(packaged.ClientError, match="^summary_reference_exceeds_output_budget$"):
        packaged.summary_page([{"ref": "s-test/" + "x" * 10000}])
