"""Internal slices for sequential mixed reads; no mutation or production services."""

# ruff: noqa: F811 -- imported pytest fixture
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401


def read(aggregate, query=None, **kwargs):
    return helper.resource_list(
        aggregate.catalog,
        query=helper.resource_list_query(fields=["node"]) if query is None else query,
        root=aggregate.script.parents[1],
        **kwargs,
    )


def nodes(count):
    return [
        {"id": f"node-{number:02}", "lock_status": "idle", "holders": [], "booking_list": []}
        for number in range(count)
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"max_calls": True}, {"max_calls": 0}, {"max_calls": 13},
        {"byte_limit": 1023}, {"byte_limit": 8193}, {"byte_limit": True},
        {"page_limit": 0}, {"page_limit": 21}, {"page_limit": True},
        {"include_validity": 1}, {"deadline": float("nan")}, {"deadline": True},
    ],
)
def test_invalid_internal_budgets_precede_catalog_and_key_reads(monkeypatch, options):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("no catalogue or Key may be opened")

    monkeypatch.setattr(helper, "listed_connections", forbidden)
    with pytest.raises(helper.ClientError, match="resource_list_budget_invalid") as caught:
        helper.resource_list(query=helper.resource_list_query(), clock=lambda: 0, **options)
    assert caught.value.http_requests == 0


def test_deadline_is_shared_clipped_and_expired_before_io(monkeypatch):
    reader = helper._ResourceListReader(root=None, opener=None, clock=lambda: 10, legacy_read=False,
                                        max_calls=3, deadline=1000)
    assert reader.deadline == 70 and reader.max_calls == 3
    with pytest.raises(helper.ClientError, match="request_budget_exhausted") as caught:
        helper.resource_list(query=helper.resource_list_query(), clock=lambda: 10, deadline=10)
    assert caught.value.http_requests == 0


def test_discovery_only_slice_never_opens_key_or_advances_scope(aggregate):
    state = aggregate.server(1)
    path = aggregate.record(state, "scope")
    (path.parent / "key").unlink()
    page = json.loads(read(aggregate, max_calls=1))["result"]
    assert page["http_requests"] == 1 and page["rows"] == [] and page["next_cursor"]
    assert page["has_more"] and page["observed_scopes"] == 0
    assert [event[0] for event in state.events] == ["/api/agent-skill"]


def test_internal_retry_cannot_spend_beyond_slice_and_keeps_position(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.status, state.retry_after = 429, 0
    page = json.loads(read(aggregate, max_calls=2))["result"]
    assert page["http_requests"] == len(state.events) == 2
    assert page["rows"] == [] and page["has_more"] and not page["traversal_exhausted"]
    state.status = 200
    resumed = json.loads(read(aggregate, cursor=page["next_cursor"], max_calls=2))["result"]
    assert len(resumed["rows"]) == 1 and resumed["traversal_exhausted"]
    assert resumed["http_requests"] == 2


def test_output_page_limit_does_not_change_bound_query_or_next_command(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = nodes(5)
    query = helper.resource_list_query(fields=["node"], limit=20)
    first = json.loads(read(aggregate, query, page_limit=2))["result"]
    assert [row["node"] for row in first["rows"]] == ["node-00", "node-01"]
    assert first["next_command"][first["next_command"].index("--limit") + 1] == "20"
    second = json.loads(read(aggregate, query, page_limit=1, cursor=first["next_cursor"]))["result"]
    assert [row["node"] for row in second["rows"]] == ["node-02"]
    third = json.loads(read(aggregate, query, page_limit=3, cursor=second["next_cursor"]))["result"]
    assert [row["node"] for row in third["rows"]] == ["node-03", "node-04"]
    assert third["next_cursor"] is None
    changed = helper.resource_list_query(fields=["node"], limit=2)
    with pytest.raises(helper.ClientError, match="resource_list_cursor_query_changed") as caught:
        read(aggregate, changed, cursor=first["next_cursor"])
    assert caught.value.http_requests == 0


def test_lower_byte_cap_retains_cursor_tail_without_changing_query(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{**row, "id": "机器" * 15 + row["id"]} for row in nodes(8)]
    query, cursor, refs = helper.resource_list_query(fields=["node"], limit=20), None, []
    for _ in range(16):
        encoded = read(aggregate, query, cursor=cursor, byte_limit=2048)
        assert len(encoded.encode()) + 1 <= 2048
        page = json.loads(encoded)["result"]
        refs.extend(row["ref"] for row in page["rows"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    else:
        pytest.fail("lower byte budget did not make progress")
    assert len(refs) == len(set(refs)) == 8


@pytest.mark.parametrize("mine", [False, True])
def test_validity_is_opt_in_and_capped_by_known_holder_remaining(aggregate, mine):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"], "remaining_seconds": 1.5,
                     "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}],
    }]
    query = helper.resource_list_query(fields=["node"], mine=mine)
    default = json.loads(read(aggregate, query, clock=lambda: 0))["result"]
    assert "valid_for_seconds" not in default
    bounded = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert 0 < bounded["valid_for_seconds"] <= 1.5
    assert state.key not in json.dumps(bounded)
    assert "expires_at" not in json.dumps(bounded)


def test_unknown_holder_timing_and_idle_share_observation_freshness_ceiling(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = nodes(1)
    fresh = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert fresh["valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE
    state.extra["resources"] = [{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"]}],
    }]
    unknown = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert unknown["valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE
    assert unknown["coverage_valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE


def test_client_error_carries_actual_spent_requests(aggregate, monkeypatch):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    original, count = helper._ResourceListReader.project, [0]

    def invalid(*args, **kwargs):
        count[0] += 1
        if count[0] > 1:
            raise helper.ClientError("synthetic_display_failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(helper._ResourceListReader, "project", invalid)
    with pytest.raises(helper.ClientError, match="synthetic_display_failure") as caught:
        read(aggregate)
    assert caught.value.http_requests == len(state.events) == 2


def test_empty_own_page_has_live_coverage_bound_without_returned_row_bound(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = nodes(1)
    query = helper.resource_list_query(fields=["node"], mine=True)
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["rows"] == [] and page["scope_complete"]
    assert page["valid_for_seconds"] == 0
    assert page["coverage_valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE


def test_filtered_absence_coverage_includes_unreturned_known_lease(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{
        "id": "not-requested", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"], "remaining_seconds": 0.75,
                     "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}],
    }]
    query = helper.resource_list_query(nodes=["absent"], fields=["node"], mine=True)
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["rows"] == [] and page["scope_complete"]
    assert 0 < page["coverage_valid_for_seconds"] <= 0.75
    assert page["valid_for_seconds"] == 0


def test_row_validity_and_all_candidates_coverage_are_independent(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [*nodes(1), {
        "id": "not-requested", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"], "remaining_seconds": "invalid"}],
    }]
    query = helper.resource_list_query(nodes=["node-00"], fields=["node"])
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE
    assert page["coverage_valid_for_seconds"] == 0


def test_absolute_expiry_is_an_independent_smaller_validity_bound(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    captured = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    state.extra.update(captured_at=captured.isoformat(), resources=[{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"], "remaining_seconds": 60,
                     "expires_at": (captured + timedelta(seconds=2.25)).isoformat()}],
    }])
    page = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == 2.25


@pytest.mark.parametrize("elapsed,expected", [(1.25, 0.25), (2.0, 0.0)])
def test_later_read_reages_earlier_returned_lease_bound(aggregate, monkeypatch, elapsed, expected):
    first, second = aggregate.server(1), aggregate.server(2)
    aggregate.record(first, "first")
    aggregate.record(second, "second")
    first.extra["resources"] = [{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": first.actor["external_user_id"], "remaining_seconds": 1.5}],
    }]
    second.extra["resources"] = nodes(1)
    original, now = helper._ResourceListReader.read, [0.0]

    def later(self, *args, **kwargs):
        if self.clients:
            now[0] = elapsed
        return original(self, *args, **kwargs)

    monkeypatch.setattr(helper._ResourceListReader, "read", later)
    page = json.loads(read(aggregate, include_validity=True, clock=lambda: now[0]))["result"]
    assert len(page["rows"]) == 2
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == expected


def test_unknown_model_cannot_certify_complete_filtered_absence_timing(aggregate):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = nodes(1)
    query = helper.resource_list_query(model="P800", fields=["node"])
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["rows"] == [] and page["unknown_hardware_nodes"] == 1
    assert page["coverage_valid_for_seconds"] == 0


@pytest.mark.parametrize("mine", [False, True])
def test_fresh_booking_counts_retain_observation_window_without_claim_timer(aggregate, mine):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{
        "id": "node-00", "lock_status": "idle", "holders": [],
        "booking_list": [{"external_user_id": state.actor["external_user_id"], "position": 0}],
    }]
    fields = ["node", "booking_units"] if mine else ["node", "states"]
    query = helper.resource_list_query(fields=fields, mine=mine)
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE
    if mine:
        assert page["rows"][0]["booking_units"] == 1
    else:
        assert page["rows"][0]["states"]["queued"] == 1
    assert "claimable_by_you" not in page["rows"][0]


@pytest.mark.parametrize("mine", [False, True])
def test_null_holder_expiry_retains_only_fresh_observed_count(aggregate, mine):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"],
                     "remaining_seconds": None, "expires_at": None}],
    }]
    query = helper.resource_list_query(fields=["node", "holding_units"] if mine else ["node", "states"], mine=mine)
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE
    if mine:
        assert page["rows"][0]["holding_units"] == 1
    else:
        assert page["rows"][0]["states"]["busy"] == 1


@pytest.mark.parametrize("timer", [{"remaining_seconds": "not-seconds"}, {"expires_at": "not-a-date"},
                                   {"remaining_seconds": 0}])
def test_malformed_or_known_expired_holder_timing_still_has_zero_window(aggregate, timer):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    state.extra["resources"] = [{
        "id": "node-00", "lock_status": "exclusive", "booking_list": [],
        "holders": [{"external_user_id": state.actor["external_user_id"], **timer}],
    }]
    page = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == 0


@pytest.mark.parametrize("kind", ["claim_window_end", "own_lock_expiry"])
def test_known_exact_schedule_caps_booking_or_hold_evidence_window(aggregate, kind):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    captured = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    booking = kind == "claim_window_end"
    state.extra.update(
        captured_at=captured.isoformat(), next_event_kind=kind,
        next_event_at=(captured + timedelta(seconds=1.25)).isoformat(),
        resources=[{
            "id": "node-00", "lock_status": "idle" if booking else "exclusive",
            "holders": [] if booking else [{"external_user_id": state.actor["external_user_id"]}],
            "booking_list": [{"external_user_id": state.actor["external_user_id"], "position": 0}] if booking else [],
        }],
    )
    query = helper.resource_list_query(fields=["node", "holding_units", "booking_units"], mine=True)
    page = json.loads(read(aggregate, query, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == 1.25
    assert page["rows"][0]["booking_units" if booking else "holding_units"] == 1


@pytest.mark.parametrize("kind", ["own_early_warning", "queue_estimate"])
def test_warning_and_queue_estimate_do_not_shorten_observation_window(aggregate, kind):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    captured = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    state.extra.update(captured_at=captured.isoformat(), resources=nodes(1), next_event_kind=kind,
                       next_event_at=(captured + timedelta(seconds=0.1)).isoformat())
    page = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == helper.MAX_RESOURCE_SNAPSHOT_AGE


@pytest.mark.parametrize("seconds,expected", [(None, 5.0), (-1, 0.0)])
def test_exact_schedule_without_deadline_differs_from_known_elapsed_deadline(aggregate, seconds, expected):
    state = aggregate.server(1)
    aggregate.record(state, "scope")
    captured = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    event_at = (captured + timedelta(seconds=seconds)).isoformat() if seconds is not None else None
    state.extra.update(captured_at=captured.isoformat(), resources=nodes(1),
                       next_event_kind="claim_window_end", next_event_at=event_at)
    page = json.loads(read(aggregate, include_validity=True, clock=lambda: 0))["result"]
    assert page["valid_for_seconds"] == page["coverage_valid_for_seconds"] == expected
