"""One-key selective reads retain authority, live cursors and bounded display parity."""

# ruff: noqa: F811 -- imported pytest fixture
import base64
import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from lockbot.backend.app.agent_skill.scripts import lockbot_cli as sdk
from lockbot.backend.app.resource_api import resource_query as query
from lockbot.backend.app.resource_api import resources
from lockbot.backend.app.resource_api.traffic import credential_budget
from starlette.datastructures import QueryParams

from backend.test_resource_api_resources import ACTOR, _hold, api  # noqa: F401

PATH = "/api/agent/resources/query"


@pytest.fixture(autouse=True)
def budgets():
    credential_budget.reset()
    yield
    credential_budget.reset()


def get(client, api, **params):
    return client.get(PATH, headers=api["headers"], params=params)


def payload(kind="QUEUE_DEVICE", count=3):
    return {
        "context": {
            "server": {"instance_id": "query-instance"},
            "bot": {"id": 7, "uuid": "query-bot", "type": kind},
            "actor": {"platform": "infoflow", "external_user_id": ACTOR},
        },
        "actor": {"platform": "infoflow", "external_user_id": ACTOR},
        "bot_type": kind,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "aliases_observed_at": datetime.now(timezone.utc).isoformat(),
        "age_seconds": 0.0,
        "health": "running",
        "resources": [
            {
                "id": "node-a",
                "alias": "19",
                "devices": [
                    {"id": number, "dev_model": "P800", "lock_status": "idle", "holders": [], "booking_list": []}
                    for number in range(count)
                ],
            }
        ],
    }


def pure_page(snapshot, params, cursor=None, *, generation=3, actor=None, bot=None):
    selected, supplied = query.parse_query(QueryParams(params))
    result = query.query_response(
        snapshot,
        selected,
        cursor or supplied,
        actor=actor or SimpleNamespace(binding_public_id="public-binding", credential_generation=2),
        bot_row=bot or SimpleNamespace(uuid="query-bot", identity_revision=1),
        generation=generation,
    )
    assert len(result.body) <= query.MAX_RESPONSE_BYTES
    return json.loads(result.body)


def test_manifest_advertises_only_the_supported_bounded_contract(client, api):
    manifest = client.get("/api/agent-skill").json()
    assert "resource_query_v1" in manifest["capabilities"]
    assert manifest["resource_query"] == query.capability_descriptor()
    assert manifest["resource_query"]["filters"] == ["node_id", "model", "mine"]
    assert "metrics" not in manifest["resource_query"]["node_fields"]


def test_public_openapi_lists_exact_supported_query_and_personal_key_security(client):
    response = client.get("/api/public-openapi.json?scope=agent")
    assert response.status_code == 200
    operation = response.json()["paths"][PATH]["get"]
    assert operation["security"] == [{"AgentKey": []}]
    parameters = {row["name"]: row for row in operation["parameters"]}
    assert set(parameters) == {"view", "node_id", "model", "mine", "fields", "limit", "cursor"}
    assert parameters["limit"]["schema"]["maximum"] == 256
    assert parameters["cursor"]["schema"]["maxLength"] == 2048
    assert "atomic" in parameters["cursor"]["description"]
    assert "partial" in operation["description"]


@pytest.mark.parametrize("kind", ["auth", "disabled", "validation", "limited", "stopped"])
def test_selective_query_errors_cannot_be_http_cached(client, api, monkeypatch, kind):
    headers, params = api["headers"], {}
    if kind == "auth":
        headers = {}
    elif kind == "disabled":
        client.put(f"/api/bots/{api['bot_id']}/resource-auth", headers=api["auth_header"], json={"enabled": False})
    elif kind == "validation":
        params = {"actor": "someone-else"}
    elif kind == "limited":
        monkeypatch.setattr(resources.credential_budget, "check", lambda *_args: (False, 17))
    else:
        api["manager"].stop_bot(api["bot_id"])
    response = client.get(PATH, headers=headers, params=params)
    assert (
        response.status_code == {"auth": 401, "disabled": 403, "validation": 400, "limited": 429, "stopped": 503}[kind]
    )
    assert response.headers["cache-control"] == "no-store"


def test_query_is_activity_read_and_does_not_log_mutation(client, api, db_session):
    from lockbot.backend.app.resource_api.activity import AgentActivity

    response = get(client, api, fields="node")
    assert response.status_code == 200
    db_session.expire_all()
    records = db_session.query(AgentActivity).all()
    assert len(records) == 1
    assert records[0].read_requests == 1 and records[0].write_requests == 0


def test_ingress_refusal_of_query_is_not_http_cacheable():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lockbot.backend.app.resource_api.cache_control import ResourceSnapshotCacheMiddleware
    from lockbot.backend.app.resource_api.ingress import AgentIngressMiddleware, IngressBudget

    app = FastAPI()
    app.add_middleware(AgentIngressMiddleware, budget=IngressBudget(source_limit=1))
    app.add_middleware(ResourceSnapshotCacheMiddleware)

    @app.get(PATH)
    def endpoint():
        return {"read": True}

    with TestClient(app) as client:
        assert client.get(PATH).status_code == 200
        response = client.get(PATH)
        assert response.status_code == 429 and response.headers["cache-control"] == "no-store"
        assert int(response.headers["retry-after"]) > 0


def test_query_is_one_charge_one_admission_and_same_cache(client, api, monkeypatch):
    charges, admissions, snapshots = [], [], []
    original_admit = api["manager"].admit
    original_snapshot = api["bot"].state_snapshot

    def check(binding, kind):
        charges.append((binding, kind))
        return True, 0

    @contextmanager
    def admitted(*args, **kwargs):
        admissions.append((args, kwargs))
        with original_admit(*args, **kwargs) as lease:
            yield lease

    def snapshot():
        snapshots.append(True)
        return original_snapshot()

    monkeypatch.setattr(resources.credential_budget, "check", check)
    monkeypatch.setattr(api["manager"], "admit", admitted)
    monkeypatch.setattr(api["bot"], "state_snapshot", snapshot)
    first = get(client, api, fields="node", limit=1)
    second = get(client, api, fields="node", limit=1)
    assert first.status_code == second.status_code == 200
    assert len(charges) == len(admissions) == 2 and len(snapshots) == 1
    assert all(kind == "read" for _, kind in charges)
    assert all(kwargs == {"for_read": True} for _, kwargs in admissions)
    assert first.headers["cache-control"] == "no-store"
    assert "Authorization" in first.headers["vary"]
    assert set(first.json()["rows"][0]) == {"node", "unit", "snapshot_fresh"}
    assert "resources" not in first.json() and "resource_snapshot" not in first.json()


@pytest.mark.parametrize(
    "params",
    [
        [("actor", "bob")],
        [("bot_id", "2")],
        [("unknown", "1")],
        [("limit", "1"), ("limit", "2")],
        [("fields", "node,node")],
        [("fields", "node,metrics")],
        [("view", "devices")],
        [("mine", "1")],
        [("limit", "257")],
        [("limit", "00")],
        [("search", "P800")],
        [("node_hint", "19")],
    ],
)
def test_bad_or_unsupported_query_never_enters_admission(client, api, monkeypatch, params):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid query must precede inventory/budget work")

    monkeypatch.setattr(api["manager"], "admit", forbidden)
    monkeypatch.setattr(resources.credential_budget, "check", forbidden)
    result = client.get(PATH, headers=api["headers"], params=params)
    assert result.status_code == 400 and result.json()["detail"]["code"] == "resource_query_invalid"


@pytest.mark.parametrize("headers", [None, "management", "other"])
def test_query_authentication_does_not_add_cross_binding_authority(client, api, headers):
    selected = (
        {}
        if headers is None
        else api["auth_header"]
        if headers == "management"
        else {"Authorization": "Bearer lbr_invalid-not-a-credential"}
    )
    response = client.get(PATH, headers=selected)
    assert response.status_code == 401


def test_revocation_at_admission_is_reverified(client, api, monkeypatch):
    observed = []

    def refuse(*_args):
        observed.append(api["manager"]._inflight[api["bot_id"]])
        return False

    monkeypatch.setattr(resources, "reverify_actor", refuse)
    result = get(client, api)
    assert result.status_code == 401 and observed == [1]


def test_rate_limit_does_not_enter_admission(client, api, monkeypatch):
    monkeypatch.setattr(resources.credential_budget, "check", lambda *_args: (False, 17))
    monkeypatch.setattr(api["manager"], "admit", lambda *_args, **_kwargs: pytest.fail("must not admit"))
    result = get(client, api)
    assert result.status_code == 429 and result.headers["retry-after"] == "17"


def test_full_lease_is_released_on_admission_refusal(client, api):
    manager, bot = api["manager"], api["bot_id"]
    with manager.admit(bot), manager.admit(bot), manager.admit(bot), manager.admit(bot), manager.admit(bot):
        response = get(client, api)
        assert response.status_code == 429
    assert manager._inflight.get(bot, 0) == 0


def test_query_does_not_build_private_declarations_addresses_or_new_grants(client, api, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("query must not decorate private data or issue capabilities")

    monkeypatch.setattr(resources, "active_declarations", forbidden)
    monkeypatch.setattr(resources, "saved_node_access", forbidden)
    monkeypatch.setattr("lockbot.backend.app.bots.metrics_access.issue_metrics_access", forbidden)
    monkeypatch.setattr("lockbot.backend.app.resource_api.view_links.view_link", forbidden)
    result = get(client, api, fields="node,hardware")
    assert result.status_code == 200, result.text
    assert all("node_access" not in row and "notes" not in row for row in result.json()["rows"])


def test_old_endpoint_keeps_full_response_and_query_uses_detached_cache(client, api):
    _hold(api, "n1", ACTOR)
    first = client.get("/api/agent/resources", headers=api["headers"]).json()
    state = copy.deepcopy(api["bot"].state.bot_state)
    selected = get(client, api, mine="true", fields="node,holdings,bookings").json()
    second = client.get("/api/agent/resources", headers=api["headers"]).json()
    assert first["resources"] == second["resources"] and api["bot"].state.bot_state == state
    assert set(first) == set(second) and "resources" in first
    assert selected["rows"][0]["holdings"] and not selected["rows"][0]["bookings"]
    assert "external_user_id" not in json.dumps(selected["rows"])


def test_exact_node_is_not_reinterpreted_as_alias():
    page = pure_page(payload(), {"node_id": "19", "fields": "node"})
    assert page["rows"] == [] and page["traversal_exhausted"]
    exact = pure_page(payload(), {"node_id": "node-a", "fields": "node"})
    assert exact["rows"][0]["node"] == "node-a"


def test_device_rows_match_sdk_projection_and_keep_zero_and_gaps():
    snapshot = payload(count=4)
    snapshot["resources"][0]["devices"][1]["id"] = "00"
    snapshot["resources"][0]["devices"][2]["id"] = 7
    page = pure_page(snapshot, {"view": "devices", "node_id": "node-a"})
    observed = copy.deepcopy(snapshot)
    observed["snapshot_fresh_for_read"] = True
    expected = sdk.device_summary_rows(observed, observed["context"], node="node-a")["rows"]
    assert page["rows"] == expected
    assert [row["id"] for row in page["rows"]] == [0, 3, 7, "00"]


@pytest.mark.parametrize("field", ["holders", "booking_list"])
def test_missing_own_roles_remain_unknown_despite_field_selection(field):
    snapshot = payload(count=2)
    snapshot["resources"][0]["devices"][0].pop(field)
    page = pure_page(snapshot, {"view": "devices", "node_id": "node-a", "mine": "true", "fields": "id"})
    assert page["coverage"]["projection_complete"] is False
    assert page["rows"][0] == {"id": 0, "snapshot_fresh": True, "ownership_match_unknown": True}
    assert page["coverage"]["unknown_ownership_devices"] == 1


def test_stale_own_rows_are_locator_only_and_missing_hardware_is_not_no_match():
    snapshot = payload(count=2)
    snapshot["resources"][0]["devices"][0].update(
        lock_status="exclusive", holders=[{"external_user_id": ACTOR, "remaining_seconds": 3600, "expires_at": None}]
    )
    snapshot["age_seconds"] = 6
    page = pure_page(snapshot, {"mine": "true", "fields": "node,holdings,holding_units"})
    assert page["rows"][0] == {
        "node": "node-a",
        "unit": "card",
        "snapshot_fresh": False,
        "observation_incomplete": True,
        "holding_units": None,
    }
    assert page["coverage"]["unknown_ownership_units"] > 0
    snapshot["age_seconds"] = 0
    snapshot["resources"][0]["devices"][1]["dev_model"] = None
    page = pure_page(snapshot, {"model": "H20", "fields": "node"})
    assert not page["rows"] and page["coverage"]["unknown_hardware_nodes"] == 1
    assert not page["coverage"]["projection_complete"]


def test_signed_cursor_binds_query_scope_generation_configuration_and_expiry(monkeypatch):
    snapshot = payload(count=5)
    params = {"view": "devices", "node_id": "node-a", "fields": "id", "limit": "2"}
    first = pure_page(snapshot, params)
    assert [row["id"] for row in first["rows"]] == [0, 1]
    cursor = first["next_cursor"]
    assert cursor == sdk.resource_query_resume_cursor(first["cursor_frame"], "1", first["row_cursors"][-1])
    frame_data = json.loads(query._unb64(first["cursor_frame"][5:]))
    assert len(frame_data) == 2 and frame_data[0] == 1 and type(frame_data[1]) is int
    assert len(first["cursor_frame"]) < 40
    second = pure_page(snapshot, params, cursor)
    assert [row["id"] for row in second["rows"]] == [2, 3]
    last = pure_page(snapshot, params, second["next_cursor"])
    assert [row["id"] for row in last["rows"]] == [4]
    assert last["next_cursor"] is None and len(last["row_cursors"]) == 1
    # An SDK byte-trim can resume from any actually delivered row without loss.
    trimmed_cursor = sdk.resource_query_resume_cursor(first["cursor_frame"], "0", first["row_cursors"][0])
    trimmed = pure_page(snapshot, params, trimmed_cursor)
    assert [row["id"] for row in trimmed["rows"]] == [1, 2]
    # The shared frame is not independently authoritative; its mark binds the
    # exact emitted key. Recombining valid components cannot move the boundary.
    recombined = sdk.resource_query_resume_cursor(first["cursor_frame"], "1", first["row_cursors"][0])
    with pytest.raises(Exception) as caught:
        pure_page(snapshot, params, recombined)
    assert caught.value.status_code == 409
    for options in (
        {"generation": 4},
        {"actor": SimpleNamespace(binding_public_id="other", credential_generation=2)},
        {"actor": SimpleNamespace(binding_public_id="public-binding", credential_generation=3)},
        {"bot": SimpleNamespace(uuid="other-bot", identity_revision=1)},
        {"bot": SimpleNamespace(uuid="query-bot", identity_revision=2)},
    ):
        with pytest.raises(Exception) as caught:
            pure_page(snapshot, params, cursor, **options)
        assert caught.value.status_code == 409
    changed = copy.deepcopy(snapshot)
    changed["resources"][0]["devices"][2]["dev_model"] = "H20"
    with pytest.raises(Exception) as caught:
        pure_page(changed, params, cursor)
    assert caught.value.status_code == 409
    changed_identity = copy.deepcopy(snapshot)
    changed_identity["context"]["server"]["instance_id"] = "other-instance"
    with pytest.raises(Exception) as caught:
        pure_page(changed_identity, params, cursor)
    assert caught.value.status_code == 409
    changed_expiry = "rqf1." + query._b64(query._encoded([1, frame_data[1] + 1]))
    unsigned_change = sdk.resource_query_resume_cursor(changed_expiry, "1", first["row_cursors"][-1])
    with pytest.raises(Exception) as caught:
        pure_page(snapshot, params, unsigned_change)
    assert caught.value.status_code == 409
    with pytest.raises(Exception) as caught:
        pure_page(snapshot, {**params, "limit": "3"}, cursor)
    assert caught.value.status_code == 409
    with pytest.raises(Exception) as caught:
        pure_page(snapshot, params, cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))
    assert caught.value.status_code == 400
    monkeypatch.setattr(query.time, "time", lambda: 10**12)
    with pytest.raises(Exception) as caught:
        pure_page(snapshot, params, cursor)
    assert caught.value.status_code == 409


def test_lock_changes_do_not_invalidate_hardware_cursor_and_token_has_no_authority_secrets():
    snapshot = payload(count=4)
    params = {"view": "devices", "node_id": "node-a", "fields": "id", "limit": "2"}
    first = pure_page(snapshot, params)
    snapshot["resources"][0]["devices"][3].update(lock_status="exclusive", holders=[{"external_user_id": "peer"}])
    assert [row["id"] for row in pure_page(snapshot, params, first["next_cursor"])["rows"]] == [2, 3]
    body = first["next_cursor"].split(".")[1]
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    assert all(value not in decoded for value in ("public-binding", "alice", "credential", "key_file", "lbr_"))


def test_thousand_card_node_stays_bounded_and_tokens_are_counted():
    snapshot = payload(count=1000)
    page = pure_page(snapshot, {"fields": "node,hardware,matched_device_ids"})
    assert len(page["rows"][0]["matched_device_ids"]) == 20
    assert page["rows"][0]["devices_has_more"]
    assert page["rows"][0]["device_inventory_complete"]
    assert page["rows"][0]["unidentified_devices"] == 0
    assert page["rows"][0]["hardware_has_more"] is False
    assert page["rows"][0]["hardware_incomplete"] is False
    device = pure_page(snapshot, {"view": "devices", "node_id": "node-a", "limit": "256"})
    assert len(json.dumps(device, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()) <= 65536
    assert device["has_more"] and len(device["row_cursors"]) == len(device["rows"])
    assert all(len(token.encode()) <= 2048 for token in device["row_cursors"])
    assert all(len(token) == 43 for token in device["row_cursors"])
    assert 0 < len(device["rows"]) <= 256


def test_filtered_clipped_fields_keep_their_completeness_companions():
    snapshot = payload(count=1000)
    node = snapshot["resources"][0]
    node["alias"] = "public-label-" * 20
    for index, card in enumerate(node["devices"]):
        card["dev_model"] = "model-" + str(index % 50)
        card.update(lock_status="exclusive", holders=[{"external_user_id": ACTOR, "remaining_seconds": 3600}])
        card["booking_list"] = [{"external_user_id": ACTOR, "position": 0}]
    page = pure_page(snapshot, {"fields": "hardware,alias,idle_device_ids"})
    row = page["rows"][0]
    assert len(row["hardware"]) == 20 and row["hardware_has_more"] and not row["hardware_incomplete"]
    assert len(row["alias"]) == 160 and row["alias_truncated"]
    assert row["idle_device_ids"] == [] and not row["idle_devices_has_more"]
    assert row["device_inventory_complete"] and row["unidentified_devices"] == 0
    own = pure_page(snapshot, {"mine": "true", "fields": "holdings,bookings"})["rows"][0]
    assert len(own["holdings"]) == len(own["bookings"]) == 20
    assert own["holdings_has_more"] and own["bookings_has_more"]
    assert own["holding_units"] == own["booking_units"] == 1000


def test_stale_context_fields_always_retained_and_failed_generation_is_readable(client, api):
    manager, bot_id = api["manager"], api["bot_id"]
    manager._scheduler.remove(bot_id)
    manager._make_fatal_error_handler(bot_id)(bot_id)
    result = get(client, api, fields="node")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["health"] == "error" and not body["coverage"]["projection_complete"]
    assert all(row["snapshot_fresh"] is False for row in body["rows"])
    assert {"actor", "bot", "server"} <= set(body["context"])


@pytest.mark.parametrize("view,mine", [("nodes", False), ("nodes", True), ("devices", True)])
def test_final_packing_reages_and_demotes_state_or_own_roles(monkeypatch, view, mine):
    snapshot = payload(count=3)
    captured = datetime.now(timezone.utc)
    snapshot["captured_at"] = captured.isoformat()
    card = snapshot["resources"][0]["devices"][0]
    card.update(lock_status="exclusive", holders=[{"external_user_id": ACTOR, "remaining_seconds": 3600}])
    clock = [captured.timestamp() + 4.9]

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0], tz or timezone.utc)

    original = query._cursor

    def crossed(*args, **kwargs):
        clock[0] = captured.timestamp() + 5.1
        return original(*args, **kwargs)

    monkeypatch.setattr(query, "datetime", ClockDatetime)
    monkeypatch.setattr(query, "_cursor", crossed)
    params = {"view": view, "mine": "true" if mine else "false"}
    if view == "devices":
        params["node_id"] = "node-a"
    page = pure_page(snapshot, params)
    assert page["age_seconds"] == pytest.approx(5.1)
    assert not page["coverage"]["projection_complete"]
    assert all(not row["snapshot_fresh"] for row in page["rows"])
    if view == "devices":
        assert len(page["rows"]) == 3  # newly unknown earlier/non-owned cards are retained
        assert all(row["held_by_you"] is None for row in page["rows"])
    elif mine:
        assert page["rows"][0]["observation_incomplete"]
        assert "holdings" not in page["rows"][0] and page["rows"][0]["holding_units"] is None
    else:
        assert page["rows"][0]["states"]["unknown"] == 3


@pytest.mark.parametrize("view", ["nodes", "devices"])
@pytest.mark.parametrize("elapsed", [0.2, 1.1])
def test_own_lease_horizon_demotes_during_packing_before_snapshot_stales(monkeypatch, view, elapsed):
    snapshot = payload(count=1)
    captured = datetime.now(timezone.utc)
    snapshot["captured_at"] = captured.isoformat()
    snapshot["resources"][0]["devices"][0].update(
        lock_status="exclusive",
        holders=[{"external_user_id": ACTOR, "remaining_seconds": 1.0}],
    )
    clock = [captured.timestamp()]

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0], tz or timezone.utc)

    original = query._cursor

    def crossed(*args, **kwargs):
        clock[0] = captured.timestamp() + elapsed
        return original(*args, **kwargs)

    monkeypatch.setattr(query, "datetime", ClockDatetime)
    monkeypatch.setattr(query, "_cursor", crossed)
    params = {"view": view, "mine": "true"}
    if view == "devices":
        params["node_id"] = "node-a"
    page = pure_page(snapshot, params)
    assert page["age_seconds"] == pytest.approx(elapsed)
    assert page["valid_for_seconds"] == pytest.approx(max(0.0, 1.0 - elapsed))
    row = page["rows"][0]
    if elapsed < 1:
        assert row["snapshot_fresh"] and page["coverage"]["projection_complete"]
        assert row["held_by_you"] is True if view == "devices" else row["holding_units"] == 1
    else:
        assert not row["snapshot_fresh"] and not page["coverage"]["projection_complete"]
        if view == "devices":
            assert row["held_by_you"] is None and row["state"] == "unknown"
        else:
            assert row["holding_units"] is None and "holdings" not in row


def test_supplied_protocol_update_headers_survive_json_response():
    snapshot = payload()
    selected, _ = query.parse_query(QueryParams({"fields": "node"}))
    response = query.query_response(
        snapshot,
        selected,
        None,
        actor=SimpleNamespace(binding_public_id="public-binding", credential_generation=2),
        bot_row=SimpleNamespace(uuid="query-bot", identity_revision=1),
        generation=3,
        headers={"X-Lockbot-API-Version": "1", "X-Lockbot-Skill-Update": "available", "Vary": "Origin"},
    )
    assert response.headers["X-Lockbot-API-Version"] == "1"
    assert response.headers["X-Lockbot-Skill-Update"] == "available"
    assert set(response.headers["Vary"].split(", ")) == {"Authorization", "Origin"}


@pytest.mark.parametrize("field", ["model", "node_id", "fields"])
@pytest.mark.parametrize("secret", ["lbr_" + "x" * 43, "lbk_" + "y" * 43, "lbc1." + "a" * 22 + "." + "b" * 43])
def test_secret_shaped_query_values_are_not_echoed_or_encoded(client, api, monkeypatch, field, secret):
    monkeypatch.setattr(api["manager"], "admit", lambda *_args, **_kwargs: pytest.fail("must not admit"))
    response = get(client, api, **{field: secret})
    assert response.status_code == 400
    assert secret not in response.text and secret not in str(response.headers)


@pytest.mark.parametrize("api", ["QUEUE_DEVICE"], indirect=True)
def test_real_queue_card_own_holdings_and_bookings_stay_separate(client, api):
    first, second = api["bot"].state.bot_state["n1"]
    first.update(
        status="exclusive",
        current_users=[
            {
                "user_id": ACTOR,
                "start_time": int(datetime.now(timezone.utc).timestamp()),
                "duration": 3600,
                "is_notified": False,
            }
        ],
    )
    second["booking_list"].append(
        {
            "user_id": ACTOR,
            "start_time": int(datetime.now(timezone.utc).timestamp()),
            "duration": 7200,
            "is_notified": True,
        }
    )
    response = get(client, api, mine="true", fields="node,holding_units,booking_units,holdings,bookings")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["holding_units"] == row["booking_units"] == 1
    assert row["holdings"][0]["device"] == 0 and row["holdings"][0]["mode"] == "exclusive"
    assert row["bookings"][0] == {"device": 1, "model": "gpu", "queue_rank": 1, "claimable_by_you": True}
    assert "external_user_id" not in json.dumps(row)
