"""AC-6 / AC-16: the resource snapshot and its client-scheduling fields."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.resource_api.resources import snapshot_cache
from lockbot.backend.app.resource_auth import service

PLATFORM = "infoflow"
ACTOR = "alice"
OTHER = "bob"


@pytest.fixture(autouse=True)
def _clear_cache():
    snapshot_cache.clear()
    yield
    snapshot_cache.clear()


@pytest.fixture
def api(client, auth_header, db_session, monkeypatch, session_factory, tmp_path, request):
    # Imported fixtures do not inherit this module's autouse cache fixture.
    snapshot_cache.clear()
    bot_type = getattr(request, "param", "NODE")
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)

    cluster = {"n1": ["gpu", "gpu"]} if bot_type in {"DEVICE", "QUEUE_DEVICE"} else {"n1": "m1", "n2": "m2"}
    created = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": f"resbot-{bot_type}",
            "bot_type": bot_type,
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": cluster,
        },
    )
    assert created.status_code == 201, created.text
    bot_id = created.json()["id"]
    client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})

    def _issue(actor):
        issued = client.post("/api/resource-auth/bindings", json={"label": actor}).json()
        service.handle_chat_binding(
            platform=PLATFORM, external_user_id=actor, bot_id=bot_id, action="bind", argument=issued["bind_code"]
        )
        return issued["credential"]

    credential = _issue(ACTOR)
    other_credential = _issue(OTHER)
    db_session.expire_all()

    manager = BotManager(max_inflight=5, drain_timeout=2.0)
    manager.start_bot(
        bot_id,
        {
            "BOT_TYPE": bot_type,
            "BOT_NAME": f"resbot-{bot_type}",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "bots"),
            "CLUSTER_CONFIGS": cluster,
            "ALLOW_USER_ALIAS": True,
        },
    )

    with (
        patch("lockbot.backend.app.resource_api.resources.bot_manager", manager),
        patch("lockbot.backend.app.resource_api.commands.bot_manager", manager),
    ):
        yield {
            "bot_id": bot_id,
            "bot": manager.get_instance(bot_id).bot,
            "manager": manager,
            "headers": {"Authorization": f"Bearer {credential}"},
            "other_headers": {"Authorization": f"Bearer {other_credential}"},
            "auth_header": auth_header,
        }
    manager.shutdown_all()


def _get(client, api, headers=None):
    return client.get(f"/api/bots/{api['bot_id']}/resources", headers=headers or api["headers"])


def _hold(api, node_key, user_id, duration=3600, started=None, status="exclusive"):
    node = api["bot"].state.bot_state[node_key]
    node["status"] = status
    node["current_users"].append(
        {
            "user_id": user_id,
            "start_time": int(started or datetime.now(timezone.utc).timestamp()),
            "duration": duration,
            "is_notified": False,
        }
    )


class TestShape:
    def test_top_level_fields(self, client, api):
        body = _get(client, api).json()
        assert body["bot_id"] == api["bot_id"]
        assert body["bot_type"] == "NODE"
        assert body["platform"] == "infoflow"
        assert body["health"] in ("running", "stopped", "error")
        assert body["captured_at"]
        assert body["actor"] == {"platform": PLATFORM, "external_user_id": ACTOR}
        assert isinstance(body["resources"], list)

    def test_supported_operations_are_the_agent_reachable_ones(self, client, api):
        ops = _get(client, api).json()["supported_operations"]
        assert set(ops) <= {"lock", "slock", "unlock", "query", "book", "alias"}
        assert "kickout" not in ops
        assert "take" not in ops

    def test_resource_ids_are_what_commands_accept(self, client, api):
        ids = [r["id"] for r in _get(client, api).json()["resources"]]
        assert set(ids) == {"n1", "n2"}
        resp = client.post(
            f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": f"lock {ids[0]} 1h"}
        )
        assert resp.status_code == 200, resp.text

    def test_lock_status_semantics_are_documented(self, client, api):
        notes = _get(client, api).json()["notes"]
        assert "lock_status" in notes
        assert "effective availability" in notes["lock_status"]

    def test_no_attribution_is_claimed(self, client, api):
        assert "does not attribute" in _get(client, api).json()["notes"]["attribution"]

    def test_alias_non_standard_properties_are_documented(self, client, api):
        """AC-5.1: all four, plus the fact that alias is absent from this snapshot."""
        note = _get(client, api).json()["notes"]["alias"]
        assert "bot-GLOBAL" in note
        assert "NOT covered by the bot state lock" in note
        assert "15 characters" in note
        assert "best-effort" in note
        assert "aliases_disabled" in note

    def test_the_snapshot_does_not_cover_alias(self, client, api):
        """AC-6 negative: an alias lives in configuration, not in lock state."""
        resp = client.post(
            f"/api/bots/{api['bot_id']}/commands",
            headers=api["headers"],
            json={"command": "alias n1 10.0.0.9"},
        )
        assert resp.status_code == 200, resp.text
        snapshot_cache.clear()
        raw = _get(client, api).text
        assert "10.0.0.9" not in raw
        assert "alias" not in {k for r in _get(client, api).json()["resources"] for k in r}

    def test_absence_of_push_delivery_is_stated(self, client, api):
        assert "no push delivery" in _get(client, api).json()["notes"]["delivery"]

    def test_private_bookkeeping_keys_never_reach_the_wire(self, client, api):
        raw = _get(client, api).text
        assert "_caller_booking_started_at" not in raw


class TestHolderTiming:
    def test_absolute_and_relative_timing_agree(self, client, api):
        _hold(api, "n1", ACTOR, duration=3600)
        body = _get(client, api).json()
        resource = next(r for r in body["resources"] if r["id"] == "n1")
        holder = resource["holders"][0]

        captured_at = datetime.fromisoformat(body["captured_at"])
        expires_at = datetime.fromisoformat(holder["expires_at"])
        derived = (expires_at - captured_at).total_seconds()
        assert abs(derived - holder["remaining_seconds"]) <= 1

    def test_expires_at_is_absolute_utc(self, client, api):
        _hold(api, "n1", ACTOR)
        holder = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")["holders"][0]
        moment = datetime.fromisoformat(holder["expires_at"])
        assert moment.tzinfo is not None
        assert moment.utcoffset().total_seconds() == 0

    def test_holder_identity_is_platform_neutral(self, client, api):
        _hold(api, "n1", ACTOR)
        holder = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")["holders"][0]
        assert holder["platform"] == PLATFORM
        assert holder["external_user_id"] == ACTOR
        assert "user_id" not in holder

    def test_an_elapsed_holder_still_appears_with_zero_remaining(self, client, api):
        """lock_status is stored state, not effective availability."""
        _hold(api, "n1", ACTOR, duration=1, started=datetime.now(timezone.utc).timestamp() - 600)
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert resource["lock_status"] == "exclusive"
        assert resource["holders"][0]["remaining_seconds"] == 0


class TestNextEvent:
    def test_null_when_the_caller_holds_nothing_and_has_no_booking(self, client, api):
        body = _get(client, api).json()
        assert body["next_event_at"] is None
        assert body["next_event_kind"] is None

    def test_own_lock_expiry_is_reported(self, client, api):
        _hold(api, "n1", ACTOR, duration=3600)
        body = _get(client, api).json()
        assert body["next_event_at"] is not None
        assert body["next_event_kind"] in ("own_lock_expiry", "own_early_warning")

    def test_another_holders_timing_is_not_leaked_to_a_bystander(self, client, api):
        """AC-6: a caller with no stake must not learn someone else's deadline."""
        _hold(api, "n1", OTHER, duration=3600)
        body = _get(client, api, headers=api["headers"]).json()
        assert body["next_event_at"] is None, "a bystander must not be given another holder's expiry"

    def test_each_caller_sees_only_its_own_schedule(self, client, api):
        _hold(api, "n1", OTHER, duration=3600)
        mine = _get(client, api, headers=api["headers"]).json()
        theirs = _get(client, api, headers=api["other_headers"]).json()
        assert mine["next_event_at"] is None
        assert theirs["next_event_at"] is not None


class TestCache:
    def test_a_fresh_response_reports_no_meaningful_staleness(self, client, api):
        snapshot_cache.clear()
        assert _get(client, api).json()["age_seconds"] < 1.0

    def test_a_second_poll_is_served_from_cache(self, client, api):
        first = _get(client, api).json()
        second = _get(client, api).json()
        assert second["captured_at"] == first["captured_at"], "the burst should collapse into one copy"

    def test_a_cached_response_carries_age_seconds(self, client, api):
        _get(client, api)
        with patch.object(type(api["bot"]), "state_snapshot", autospec=True) as spy:
            body = _get(client, api).json()
            spy.assert_not_called(), "a cache hit must not take the state lock again"
        assert body["age_seconds"] >= 0

    def test_the_cache_does_not_hand_out_shared_mutable_state(self, client, api):
        _get(client, api)
        api["bot"].state.bot_state["n1"]["status"] = "exclusive"
        cached = _get(client, api).json()
        resource = next(r for r in cached["resources"] if r["id"] == "n1")
        assert resource["lock_status"] == "idle", "the cached copy must be detached from live state"

    def test_the_cache_is_keyed_on_generation(self, client, api, tmp_path):
        _get(client, api)
        api["manager"].restart_bot(
            api["bot_id"],
            {
                "BOT_TYPE": "NODE",
                "BOT_NAME": "resbot-NODE",
                "BOT_ID": api["bot_id"],
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": {"n1": "m1", "n2": "m2"},
            },
        )
        fresh = _get(client, api).json()
        assert fresh["age_seconds"] < 1.0, "a new generation must not reuse the old cache entry"


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
class TestQueueFields:
    def test_booking_list_and_own_position_are_reported(self, client, api):
        node = api["bot"].state.bot_state["n1"]
        node["booking_list"].append({"user_id": OTHER, "start_time": 1, "duration": 60, "is_notified": False})
        node["booking_list"].append({"user_id": ACTOR, "start_time": 2, "duration": 60, "is_notified": False})

        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert [b["external_user_id"] for b in resource["booking_list"]] == [OTHER, ACTOR]
        assert resource["caller_queue_position"] == 1
        assert resource["caller_is_queue_head"] is False

    def test_head_of_queue_is_computed_server_side(self, client, api):
        node = api["bot"].state.bot_state["n1"]
        node["booking_list"].append({"user_id": ACTOR, "start_time": 2, "duration": 60, "is_notified": False})
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert resource["caller_queue_position"] == 0
        assert resource["caller_is_queue_head"] is True

    def test_a_caller_does_not_see_another_identitys_position_as_its_own(self, client, api):
        node = api["bot"].state.bot_state["n1"]
        node["booking_list"].append({"user_id": OTHER, "start_time": 1, "duration": 60, "is_notified": False})
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert resource["caller_queue_position"] is None
        assert resource["caller_is_queue_head"] is False

    def test_claim_window_is_exposed_for_queue_bots(self, client, api):
        assert _get(client, api).json()["queue_claim_window_seconds"] == 300

    def test_claim_window_end_is_the_next_event_for_a_notified_head(self, client, api):
        node = api["bot"].state.bot_state["n1"]
        node["status"] = "idle"
        node["booking_list"].append(
            {
                "user_id": ACTOR,
                "start_time": int(datetime.now(timezone.utc).timestamp()),
                "duration": 60,
                "is_notified": True,
            }
        )
        body = _get(client, api).json()
        assert body["next_event_kind"] == "claim_window_end"

    def test_a_non_head_booking_gets_an_estimate_not_an_exact_promise(self, client, api):
        node = api["bot"].state.bot_state["n1"]
        _hold(api, "n1", "carol", duration=3600)
        node["booking_list"].append({"user_id": OTHER, "start_time": 1, "duration": 60, "is_notified": False})
        node["booking_list"].append({"user_id": ACTOR, "start_time": 2, "duration": 60, "is_notified": False})
        body = _get(client, api).json()
        assert body["next_event_kind"] == "queue_estimate"


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
class TestDeviceFields:
    def test_devices_are_listed(self, client, api):
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert [d["id"] for d in resource["devices"]] == [0, 1]
        assert all(d["lock_status"] == "idle" for d in resource["devices"])

    def test_a_device_holder_is_reported(self, client, api):
        dev = api["bot"].state.bot_state["n1"][0]
        dev["status"] = "exclusive"
        dev["current_users"].append(
            {
                "user_id": ACTOR,
                "start_time": int(datetime.now(timezone.utc).timestamp()),
                "duration": 600,
                "is_notified": False,
            }
        )
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert resource["devices"][0]["holders"][0]["external_user_id"] == ACTOR
        assert _get(client, api).json()["next_event_kind"] == "own_lock_expiry"

    def test_no_booking_fields_for_device_bots(self, client, api):
        resource = next(r for r in _get(client, api).json()["resources"] if r["id"] == "n1")
        assert "booking_list" not in resource


class TestAuthAndLifecycle:
    def test_an_unauthenticated_read_is_rejected(self, client, api):
        assert client.get(f"/api/bots/{api['bot_id']}/resources").status_code == 401

    def test_a_management_jwt_is_rejected(self, client, api):
        assert _get(client, api, headers=api["auth_header"]).status_code == 401

    def test_a_stopped_bot_reports_bot_not_running(self, client, api):
        api["manager"].stop_bot(api["bot_id"])
        resp = _get(client, api)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "bot_not_running"

    def test_reads_do_not_notify_the_group(self, client, api):
        """AC-15: a polling agent must not be able to flood the group."""
        sent_before = len(getattr(api["bot"].adapter, "sent", []))
        _get(client, api)
        assert len(getattr(api["bot"].adapter, "sent", [])) == sent_before


class TestFailedGenerationHealth:
    """M1/AC-6: a fatally-failed generation is retained read-only so /resources reports
    health=error with the last state instead of a transient not-running/retiring error."""

    def test_resources_reports_error_health_for_a_failed_generation(self, client, api):
        bot_id, manager = api["bot_id"], api["manager"]
        _hold(api, "n1", ACTOR)

        # Drive the bot into the failed state the way the scheduler does on fatal.
        manager._scheduler.remove(bot_id)
        manager._make_fatal_error_handler(bot_id)(bot_id)
        assert bot_id in manager._failed

        resp = _get(client, api)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["health"] == "error"
        # The last detached state is still readable (the holder is still shown).
        assert any(r["id"] == "n1" for r in body["resources"])

    def test_a_write_against_a_failed_generation_is_refused(self, client, api):
        bot_id, manager = api["bot_id"], api["manager"]
        manager._scheduler.remove(bot_id)
        manager._make_fatal_error_handler(bot_id)(bot_id)

        resp = client.post(f"/api/bots/{bot_id}/commands", headers=api["headers"], json={"command": "lock n1 1h"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "bot_not_running"
