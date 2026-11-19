"""Pure card projection preserves configured IDs and uncertain ownership evidence."""

import base64
import json
from copy import deepcopy

import pytest

from backend.test_resource_agent_helper import helper

ACTOR = {"platform": "infoflow", "external_user_id": "verified-user"}


def card(identifier=0, **updates):
    return {"id": identifier, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": [], **updates}


def inventory(cards=None, kind="QUEUE_DEVICE"):
    return (
        {"snapshot_fresh_for_read": True, "health": "running", "resources": [{"id": "node-1", "devices": cards or []}]},
        {"actor": deepcopy(ACTOR), "bot": {"type": kind}},
    )


def project(cards, **kwargs):
    snapshot, context = inventory(cards)
    return helper.device_summary_rows(snapshot, context, node="node-1", **kwargs)


def test_two_hundred_sparse_configured_ids_are_exact_sorted_and_read_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Pure device projection must not open a connection or Key")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "private_bytes", forbidden)
    ids = [number * 7 for number in range(200)]
    snapshot, context = inventory([card(identifier) for identifier in reversed(ids)])
    before = deepcopy((snapshot, context))
    result = helper.device_summary_rows(snapshot, context, node="node-1")
    assert (snapshot, context) == before
    assert [row["id"] for row in result["rows"]] == sorted(ids)
    assert result["total_devices"] == result["matched_devices"] == 200
    assert result["projection_complete"]
    assert all(set(row) == set(helper.DEVICE_FIELDS) for row in result["rows"])
    assert all(row["state"] == "idle" and row["held_by_you"] is False for row in result["rows"])
    assert result["unidentified_devices"] == result["unknown_ownership_devices"] == result["unknown_model_devices"] == 0
    assert type(next(row["id"] for row in result["rows"] if row["id"] == 0)) is int
    assert [row["id"] for row in project([card("0")])["rows"]] == ["0"]


@pytest.mark.parametrize("ids", [[0, "0"], ["x", "x"], [17, 17]])
def test_all_canonical_id_collisions_reject_before_model_or_mine_filters(ids):
    rows = [card(identifier, dev_model="H20") for identifier in ids]
    with pytest.raises(helper.ClientError, match="^device_inventory_identity_ambiguous$"):
        project(rows, model="P800", mine=True)


def test_mixed_models_keep_unknown_coverage_separate_from_configured_counts():
    cards = [card(0), card(2, dev_model="H20"), card("sparse-42", dev_model=None), card(99, dev_model=" p800 ")]
    selected = project(cards, model=" P800 ")
    assert [row["id"] for row in selected["rows"]] == [0, 99]
    assert selected["total_devices"] == 4 and selected["matched_devices"] is None
    assert selected["unknown_model_devices"] == 1 and selected["projection_complete"] is False
    unfiltered = project(cards)
    assert len(unfiltered["rows"]) == unfiltered["matched_devices"] == 4
    assert unfiltered["unknown_model_devices"] == 1 and unfiltered["projection_complete"] is True
    excluded = project([card(1, dev_model="H20")], model="P800")
    assert excluded["rows"] == [] and excluded["matched_devices"] == 0 and excluded["projection_complete"]


def test_owned_booking_reports_its_rank_and_explicit_claimability_without_holder_notes_or_expiry():
    other = {
        "external_user_id": "another-person",
        "expires_at": "private-expiry",
        "remaining_seconds": 999,
        "task": "private-task",
    }
    device = card(
        0,
        lock_status="exclusive",
        holders=[other],
        booking_list=[
            {"external_user_id": "other-queued", "position": 0},
            {**ACTOR, "position": 7, "task": "own-private-task"},
        ],
        claimable_by_you=False,
        utilization=0,
    )
    result = project([device], mine=True)
    row = result["rows"][0]
    assert row["state"] == "queued" and row["mode"] == "exclusive"
    assert row["held_by_you"] is False and row["booked_by_you"] is True
    assert row["queue_rank"] == 8 and row["claimable_by_you"] is False
    assert result["matched_devices"] == 1 and result["projection_complete"]
    output = json.dumps(result)
    assert not any(
        value in output for value in ("another-person", "other-queued", "private-expiry", "private-task", "999")
    )
    assert "holders" not in row and "remaining_seconds" not in row and "expires_at" not in row


def test_actor_platform_is_part_of_identity_and_native_missing_platform_uses_verified_platform():
    device = card(
        5,
        lock_status="shared",
        holders=[{**ACTOR, "platform": "feishu"}, ACTOR.copy()],
        booking_list=[{"external_user_id": ACTOR["external_user_id"], "position": 0}],
        claimable_by_you=True,
    )
    row = project([device], mine=True)["rows"][0]
    assert row["held_by_you"] is True and row["booked_by_you"] is True
    assert row["queue_rank"] == 1 and row["claimable_by_you"] is True and row["mode"] == "shared"
    device["holders"] = [{**ACTOR, "platform": "feishu"}]
    device["booking_list"] = []
    assert project([device], mine=True)["rows"] == []
    assert project([device])["rows"][0]["held_by_you"] is False


@pytest.mark.parametrize("position", [None, -1, True, "0", 0.0])
def test_queue_rank_never_infers_position_from_array_index(position):
    row = project([card(9, booking_list=[{**ACTOR, "position": position}], claimable_by_you=True)])["rows"][0]
    assert row["booked_by_you"] is True and row["queue_rank"] is None and row["claimable_by_you"] is True


@pytest.mark.parametrize("claimable", [None, 0, 1, "true", [], {}])
def test_claimability_requires_an_explicit_bool_on_an_owned_booking(claimable):
    row = project([card(9, booking_list=[{**ACTOR, "position": 0}], claimable_by_you=claimable)])["rows"][0]
    assert row["claimable_by_you"] is None
    row = project([card(10, claimable_by_you=True)])["rows"][0]
    assert row["claimable_by_you"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-holders",
        "missing-booking",
        "bad-holders",
        "bad-booking",
        "bad-owner",
        "empty-owner",
        "bad-platform",
        "duplicate-holder",
        "duplicate-booking",
        "idle-with-holder",
        "exclusive-empty",
        "exclusive-two",
        "missing-status",
    ],
)
def test_missing_malformed_or_duplicate_evidence_retains_unknown_mine_rows(mutation):
    device = card(3)
    if mutation.startswith("missing-"):
        device.pop(
            {"missing-holders": "holders", "missing-booking": "booking_list", "missing-status": "lock_status"}[mutation]
        )
    elif mutation == "bad-holders":
        device["holders"] = {}
    elif mutation == "bad-booking":
        device["booking_list"] = None
    elif mutation == "bad-owner":
        device["booking_list"] = [None]
    elif mutation == "empty-owner":
        device["booking_list"] = [{"external_user_id": ""}]
    elif mutation == "bad-platform":
        device["booking_list"] = [{**ACTOR, "platform": None}]
    elif mutation == "duplicate-holder":
        device.update(lock_status="shared", holders=[ACTOR.copy(), {"external_user_id": ACTOR["external_user_id"]}])
    elif mutation == "duplicate-booking":
        device["booking_list"] = [ACTOR.copy(), ACTOR.copy()]
    elif mutation == "idle-with-holder":
        device["holders"] = [ACTOR.copy()]
    elif mutation == "exclusive-empty":
        device["lock_status"] = "exclusive"
    else:
        device.update(lock_status="exclusive", holders=[ACTOR.copy(), {"external_user_id": "other"}])
    result = project([device], mine=True)
    row = result["rows"][0]
    assert row["state"] == "unknown" and row["ownership_match_unknown"] is True
    assert (
        row["held_by_you"]
        is row["booked_by_you"]
        is row["claimable_by_you"]
        is row["queue_rank"]
        is row["mode"]
        is None
    )
    assert result["unknown_ownership_devices"] == 1 and result["matched_devices"] is None
    assert result["projection_complete"] is False


@pytest.mark.parametrize("condition", ["stale", "stopped", "no-freshness"])
def test_stale_or_stopped_state_does_not_imply_unknown_configured_matching(condition):
    snapshot, context = inventory([card(0)])
    if condition == "stale":
        snapshot["snapshot_fresh_for_read"] = False
    elif condition == "stopped":
        snapshot["health"] = "stopped"
    else:
        snapshot.pop("snapshot_fresh_for_read")
    result = helper.device_summary_rows(snapshot, context, node="node-1")
    assert result["matched_devices"] == result["total_devices"] == 1
    assert result["rows"][0]["snapshot_fresh"] is False and result["rows"][0]["state"] == "unknown"
    assert result["projection_complete"] is False
    own = helper.device_summary_rows(snapshot, context, node="node-1", mine=True)
    assert own["matched_devices"] is None and own["rows"][0]["ownership_match_unknown"]


def test_legacy_read_freshness_does_not_need_write_freshness_and_mine_drops_only_known_nonowners():
    snapshot, context = inventory([card(0), card(5, booking_list=[{**ACTOR, "position": 0}])])
    snapshot["snapshot_fresh_for_write"] = False
    result = helper.device_summary_rows(snapshot, context, node="node-1", mine=True)
    assert result["projection_complete"] and result["matched_devices"] == 1
    assert [row["id"] for row in result["rows"]] == [5]
    assert result["rows"][0]["state"] == "queued" and result["rows"][0]["snapshot_fresh"]


@pytest.mark.parametrize(
    "identifier", [None, True, False, -1, 1.0, {}, [], "", "x" * 257, "line\nbreak", "\ud800", "\u200d"]
)
def test_invalid_ids_are_omitted_and_never_become_positions(identifier):
    result = project([card(identifier), card(0)])
    assert [row["id"] for row in result["rows"]] == [0]
    assert result["unidentified_devices"] == 1 and result["total_devices"] == 2
    assert result["matched_devices"] is None and result["projection_complete"] is False


def test_secret_shaped_and_private_ids_are_rejected_before_possible_cursor_encoding():
    secrets = [
        "lbr_" + "k" * 43,
        "lbp_" + "p" * 30,
        "lbk_" + "a" * 30,
        "lbc1." + "a" * 22 + "." + "b" * 43,
        "private-identity",
    ]
    result = project(
        [card(value) for value in [*secrets, "prefix" + secrets[0], "safe"]], private_values=(secrets[-1],)
    )
    assert [row["id"] for row in result["rows"]] == ["safe"]
    assert result["unidentified_devices"] == 6 and result["matched_devices"] is None
    encoded_ids = base64.urlsafe_b64encode(json.dumps([row["id"] for row in result["rows"]]).encode())
    assert json.loads(base64.urlsafe_b64decode(encoded_ids)) == ["safe"]
    assert not any(secret in json.dumps(result) for secret in secrets)


@pytest.mark.parametrize("kind", ["NODE", "QUEUE"])
def test_device_zero_on_a_whole_node_group_never_establishes_card_allocation(kind):
    snapshot, context = inventory([card(0)], kind=kind)
    with pytest.raises(helper.ClientError, match="^resource_uses_whole_nodes$"):
        helper.device_summary_rows(snapshot, context, node="node-1")


def test_exact_node_selection_rejects_missing_and_ambiguous_nodes_before_filtering():
    snapshot, context = inventory([card(0)])
    with pytest.raises(helper.ClientError, match="^resource_reference_node_unavailable$"):
        helper.device_summary_rows(snapshot, context, node="NODE-1")
    snapshot["resources"].append({"id": "node-1", "devices": [card(1, dev_model="H20")]})
    with pytest.raises(helper.ClientError, match="^resource_reference_node_ambiguous$"):
        helper.device_summary_rows(snapshot, context, node="node-1", model="P800")


def test_numeric_sort_preserves_leading_zero_and_opaque_ids_without_collapsing_them():
    identifiers = ["opaque", "00", 10, "2", 0, "node-1", "01", "100"]
    result = project([card(identifier) for identifier in identifiers])
    assert [row["id"] for row in result["rows"]] == [0, "2", 10, "100", "00", "01", "node-1", "opaque"]
    assert result["matched_devices"] == 8
    assert helper.device_summary_sort_key(2) == helper.device_summary_sort_key("2")
    assert helper.device_summary_sort_key("00") != helper.device_summary_sort_key(0)


def test_large_integer_ids_use_the_same_cursor_bounds_as_string_ids():
    result = project([card(10**256), card(10**255), card(1)])
    assert result["unidentified_devices"] == 1
    assert [row["id"] for row in result["rows"]] == [1, 10**255]
    with pytest.raises(helper.ClientError, match="^invalid_device_identifier$"):
        helper.device_summary_sort_key(10**256)


def test_empty_configured_card_inventory_is_exact_but_staleness_remains_separate():
    snapshot, context = inventory([])
    result = helper.device_summary_rows(snapshot, context, node="node-1")
    assert result["rows"] == [] and result["total_devices"] == result["matched_devices"] == 0
    assert result["projection_complete"]
    snapshot["snapshot_fresh_for_read"] = False
    result = helper.device_summary_rows(snapshot, context, node="node-1")
    assert result["rows"] == [] and result["matched_devices"] == 0 and not result["projection_complete"]


def test_unknown_observation_on_a_definitely_excluded_model_does_not_change_matching_count():
    device = card(0, dev_model="H20", booking_list=None)
    result = project([device], model="P800", mine=True)
    assert result["rows"] == [] and result["matched_devices"] == 0
    assert result["unknown_ownership_devices"] == 1 and result["projection_complete"] is False


@pytest.mark.parametrize("kind", ["DEVICE", "QUEUE_DEVICE"])
def test_utf8_ids_keep_their_original_spelling_and_never_use_numeric_value_for_opaque_ids(kind):
    snapshot, context = inventory([card("机架-2"), card("😀" * 256), card("02"), card(2)], kind=kind)
    result = helper.device_summary_rows(snapshot, context, node="node-1")
    assert [row["id"] for row in result["rows"]] == [2, "02", "机架-2", "😀" * 256]
    assert result["matched_devices"] == 4 and result["projection_complete"]


def test_private_model_text_is_not_projected_and_counts_as_unknown_for_filtering():
    result = project([card(1, dev_model="private-model")], model="P800", private_values=("private-model",))
    assert result["rows"] == [] and result["unknown_model_devices"] == 1 and result["matched_devices"] is None
    assert result["projection_complete"] is False


@pytest.mark.parametrize("cards", [None, {}, "devices"])
def test_malformed_device_arrays_reject_instead_of_inventing_a_card(cards):
    snapshot, context = inventory([])
    snapshot["resources"][0]["devices"] = cards
    with pytest.raises(helper.ClientError, match="^invalid_device_resource_summary$"):
        helper.device_summary_rows(snapshot, context, node="node-1")
