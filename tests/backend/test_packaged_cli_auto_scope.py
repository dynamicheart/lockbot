"""The served SDK resolves saved scopes and proves dispatch through actual API traffic."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.

import contextlib
import hashlib
import importlib.util
import io
import json
from collections import Counter
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api.traffic import credential_budget
from lockbot.backend.app.resource_auth import service

from backend.test_packaged_cli_traffic import BackendTransport
from backend.test_resource_api_resources import ACTOR, PLATFORM, _hold, api  # noqa: F401

ORIGIN = "http://testserver"


@pytest.fixture
def sdk(client, api, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_URL", ORIGIN)
    monkeypatch.setenv("LOCKBOT_HOME", str(tmp_path / "auto-home"))
    credential_budget.reset()
    manifest = client.get("/api/agent-skill").json()
    archive = client.get(manifest["installation"]["download_url"])
    assert hashlib.sha256(archive.content).hexdigest() == manifest["installation"]["sha256"]
    with ZipFile(io.BytesIO(archive.content)) as package:
        package.extractall(tmp_path / "installed")
    installation = tmp_path / "installed" / "lockbot-agent"
    spec = importlib.util.spec_from_file_location("packaged_auto_scope", installation / "scripts/lockbot_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transport = BackendTransport(client, manifest["package_version"])
    original = transport.open
    transport.transform = None
    transport.before = None
    transport.reads = Counter()
    transport.commands = []

    def opened(request, timeout=None):
        if transport.before:
            transport.before(request)
        response = original(request, timeout)
        path = urlsplit(request.full_url).path
        if request.get_method() == "POST" and path == "/api/agent/commands":
            transport.commands.append(json.loads(request.data)["command"])
        if transport.transform and path in {"/api/agent/resources", "/api/agent-skill"}:
            body = json.loads(response.getvalue())
            if path == "/api/agent/resources":
                transport.reads[body["bot_id"]] += 1
            transport.transform(path, body)
            changed = io.BytesIO(json.dumps(body).encode())
            changed.headers = response.headers
            return changed
        return response

    transport.open = opened
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: transport)
    catalog = tmp_path / "auto-home/connections"
    catalog.mkdir(mode=0o700, parents=True)
    connections = []
    keys = []
    bots = [api["bot"]]
    bot_ids = [api["bot_id"]]

    def run(*arguments, code=0):
        start = len(transport.events)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            actual = module.main(list(arguments))
        assert actual == code, err.getvalue()
        for key in keys:
            assert key not in out.getvalue() + err.getvalue()
        return json.loads(out.getvalue() if actual == 0 else err.getvalue()), transport.events[start:]

    def save(headers):
        directory = catalog / ("scope-" + str(len(connections)))
        directory.mkdir(mode=0o700)
        credential = headers["Authorization"].removeprefix("Bearer ")
        keys.append(credential)
        key = directory / "key"
        key.write_text(credential)
        key.chmod(0o600)
        connection = directory / "connection.json"
        run("init", "--origin", ORIGIN, "--key-file", str(key), "--destination", str(connection))
        connections.append(connection)

    save(api["headers"])

    def register(cluster, kind="NODE"):
        created = (
            client.post(
                "/api/bots",
                headers=api["auth_header"],
                json={
                    "name": "scope-" + str(len(bot_ids)),
                    "bot_type": kind,
                    "webhook_url": "https://example.invalid/hook",
                    "cluster_configs": cluster,
                },
            )
            .raise_for_status()
            .json()
        )
        identifier = created["id"]
        client.put(
            f"/api/bots/{identifier}/resource-auth", headers=api["auth_header"], json={"enabled": True}
        ).raise_for_status()
        issued = client.post("/api/resource-auth/bindings", json={"label": "automatic scope"}).json()
        service.handle_chat_binding(
            platform=PLATFORM, external_user_id=ACTOR, bot_id=identifier, action="bind", argument=issued["bind_code"]
        )
        api["manager"].start_bot(
            identifier,
            {
                "BOT_TYPE": kind,
                "BOT_ID": identifier,
                "BOT_NAME": created["name"],
                "DATA_DIR": str(tmp_path / "bots"),
                "CLUSTER_CONFIGS": cluster,
                "ALLOW_USER_ALIAS": True,
            },
        )
        bot_ids.append(identifier)
        bots.append(api["manager"].get_instance(identifier).bot)
        save({"Authorization": "Bearer " + issued["credential"]})
        return bots[-1]

    register({"other": "other-machine"})
    yield SimpleNamespace(
        module=module,
        run=run,
        transport=transport,
        connections=connections,
        keys=keys,
        bots=bots,
        bot_ids=bot_ids,
        register=register,
        catalog=catalog,
        installation=installation,
        client=client,
        api=api,
    )
    credential_budget.reset()


def command(sdk, hint="n1", operation="lock", *extra, code=0):
    return sdk.run("command", "--operation", operation, "--node", hint, *extra, code=code)


def no_post(events):
    assert events and all(row["method"] == "GET" for row in events)


@pytest.mark.parametrize("hint", ["n1", "19"])
def test_unique_id_or_alias_uses_one_invocation_and_one_post(sdk, hint):
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "19"})
    before = {str(p): p.read_bytes() for p in sdk.catalog.rglob("*") if p.is_file()}
    output, events = command(sdk, hint, "lock", "--duration", "2h")
    assert output["result"]["verified"]
    assert output["result"]["resolution"]["observed_scopes"] == 2
    assert output["result"]["resolution"]["matches"][0]["node"] == "n1"
    assert [row["method"] for row in events] == ["GET"] * 5 + ["POST"]
    assert sdk.transport.commands == ["lock n1 2h"]
    assert before == {str(p): p.read_bytes() for p in sdk.catalog.rglob("*") if p.is_file()}


@pytest.mark.parametrize("hint", ["192.0.2.19", "19"])
def test_observed_ipv4_can_resolve_an_owned_node_for_release(sdk, hint):
    _hold(sdk.api, "n1", ACTOR)
    sdk.client.put(
        f"/api/bots/{sdk.bot_ids[0]}/node-access",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": {"host": "192.0.2.19"}}},
    ).raise_for_status()
    output, events = command(sdk, hint, "release")
    assert output["result"]["verified"]
    assert "observed_ipv4" in output["result"]["resolution"]["matches"][0]["match_kinds"][0]
    assert sum(row["method"] == "POST" for row in events) == 1
    assert sdk.transport.commands == ["unlock n1"]


@pytest.mark.parametrize("collision", ["same_alias", "id_vs_alias"])
def test_cross_scope_collisions_do_not_prefer_an_exact_id_or_idle_group(sdk, collision):
    hint = "n1" if collision == "id_vs_alias" else "19"
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": hint})
    sdk.bots[1].config.set_val("NODE_ALIASES", {"other": hint})
    output, events = command(sdk, hint, "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_target_ambiguous"
    assert output["clarification"]["candidate_count"] == 2
    assert output["command_sent"] is False and output["retry_mutation"] is False
    no_post(events)


@pytest.mark.parametrize("hint", ["19", "192.0.2.19"])
def test_missing_address_does_not_infer_a_subnet_or_substring(sdk, hint):
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "119"})
    output, events = command(sdk, hint, "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_target_missing"
    no_post(events)


@pytest.mark.parametrize("model", ["P900", "P800"])
def test_missing_hardware_and_wrong_model_stop_before_post(sdk, model):
    if model == "P800":
        sdk.client.patch(
            f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
            headers=sdk.api["auth_header"],
            json={"version": 1, "nodes": {"n1": [{"model": "P900", "count": 2}]}},
        ).raise_for_status()
    output, events = command(sdk, "n1", "lock", "--model", model, "--duration", "1h", code=2)
    assert output["error"] == "selected_resource_model_unverified_or_insufficient"
    assert output["command_sent"] is False
    no_post(events)


@pytest.mark.parametrize("fault", ["health", "duplicate_node", "bad_holders", "scope_actor", "partial", "stale"])
def test_unread_unhealthy_malformed_or_stale_scope_never_proves_uniqueness(sdk, fault):
    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[1]:
            return
        if fault == "health":
            body["health"] = "unknown"
        elif fault == "duplicate_node":
            body["resources"].append(body["resources"][0])
        elif fault == "bad_holders":
            body["resources"][0]["holders"] = [None]
        elif fault == "scope_actor":
            body["context"]["actor"]["external_user_id"] = "different-actor"
        elif fault == "stale":
            body["age_seconds"] = 6

    sdk.transport.transform = transform
    if fault == "partial":
        sdk.client.put(
            f"/api/bots/{sdk.bot_ids[1]}/resource-auth", headers=sdk.api["auth_header"], json={"enabled": False}
        ).raise_for_status()
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] in {"automatic_scope_incomplete", "automatic_scope_unhealthy", "automatic_inventory_invalid"}
    no_post(events)


def test_complete_unique_scan_reports_real_budget_exhaustion_not_ambiguity(sdk):
    for i in range(7):
        sdk.register({f"extra-{i}": f"extra-machine-{i}"})
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_request_budget_exhausted"
    assert output["clarification"]["request_budget"]["http_limit"] == 12
    assert len(events) == 10
    no_post(events)


@pytest.mark.parametrize("fault", ["alias", "config_origin", "fresh_actor"])
def test_source_selectors_and_scope_are_pinned_through_the_fresh_read(sdk, fault):
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "19"})

    def transform(path, body):
        if path != "/api/agent/resources":
            return
        if fault == "config_origin" and body["bot_id"] == sdk.bot_ids[1]:
            p = sdk.connections[0]
            saved = json.loads(p.read_text())
            saved["approved_origin"] = "http://wrong-origin.invalid"
            p.write_text(json.dumps(saved))
            p.chmod(0o600)
        elif body["bot_id"] == sdk.bot_ids[0] and sdk.transport.reads[body["bot_id"]] == 2:
            if fault == "alias":
                body["resources"][0]["alias"] = "changed"
            if fault == "fresh_actor":
                body["context"]["actor"]["external_user_id"] = "other"

    sdk.transport.transform = transform
    output, events = command(sdk, "19", "lock", "--duration", "1h", code=2)
    assert output["error"] in {
        "automatic_target_changed",
        "automatic_scope_changed",
        "context_actor_mismatch_reinitialize_connection_without_rotating_key",
    }
    no_post(events)
    assert all(row["path"].startswith("/api/") for row in events)


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_node_only_card_request_selects_all_cards_once_and_keeps_default_duration(sdk):
    output, events = command(sdk)
    assert output["result"]["verified"] and output["result"]["duration_source"] == "group-default"
    assert sdk.transport.commands == ["lock n1 dev0,1"]
    assert output["result"]["resolution"]["resolved_devices"] == ["0", "1"]
    assert sum(row["method"] == "POST" for row in events) == 1


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
@pytest.mark.parametrize("fault", ["add", "remove", "model", "busy", "unknown", "duplicate"])
def test_card_set_races_or_conflicts_never_lock_an_available_subset(sdk, fault):
    def transform(path, body):
        if path != "/api/agent/resources" or body["bot_id"] != sdk.bot_ids[0]:
            return
        fresh = sdk.transport.reads[body["bot_id"]] == 2
        cards = body["resources"][0]["devices"]
        if fault in {"busy", "unknown", "duplicate"} or fresh:
            if fault == "add":
                cards.append({**cards[1], "id": 2})
            elif fault == "remove":
                cards.pop()
            elif fault == "model":
                cards[1]["dev_model"] = "changed"
            elif fault == "busy":
                cards[1].update(lock_status="exclusive", holders=[{"external_user_id": "neighbor"}])
            elif fault == "unknown":
                cards[1]["lock_status"] = "unknown"
            elif fault == "duplicate":
                cards[1]["id"] = 0

    sdk.transport.transform = transform
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] in {
        "automatic_device_set_changed",
        "automatic_resource_conflict",
        "automatic_inventory_invalid",
    }
    no_post(events)


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
def test_explicit_card_count_and_group_constraints_are_not_changed(sdk):
    output, events = command(
        sdk, "n1", "slock", "--device", "0", "--device-count", "1", "--group", "resbot-DEVICE", "--duration", "4h"
    )
    assert output["result"]["verified"]
    assert sdk.transport.commands == ["slock n1 dev0 4h"]
    assert sum(row["method"] == "POST" for row in events) == 1


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
@pytest.mark.parametrize("extra", [("--whole-node",), ("--device-count", "1"), ("--device", "9")])
def test_explicit_modes_counts_and_missing_cards_stop_without_conversion(sdk, extra):
    output, events = command(sdk, "n1", "lock", *extra, "--duration", "1h", code=2)
    assert output["error"] in {
        "automatic_resource_mode_mismatch",
        "automatic_device_count_mismatch",
        "automatic_device_missing",
    }
    no_post(events)


def test_mixed_scope_multiple_nodes_are_not_split(sdk):
    output, events = command(sdk, "n1", "lock", "--node", "other", "--duration", "1h", code=2)
    assert output["error"] == "automatic_cross_scope_selection"
    no_post(events)


def test_lost_post_response_never_replays_or_chooses_another_target(sdk):
    original = sdk.transport.open

    def lost(request, timeout=None):
        response = original(request, timeout)
        if request.get_method() == "POST":
            response.close()
            raise HTTPError(request.full_url, 503, "lost response", Message(), io.BytesIO())
        return response

    sdk.transport.open = lost
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "mutation_outcome_unknown_read_resources_do_not_replay"
    assert output["retry_mutation"] is False
    assert sum(row["method"] == "POST" for row in events) == 1
    assert len(sdk.api["bot"].state.bot_state["n1"]["current_users"]) == 1


def test_explicit_scoped_call_keeps_three_request_flow(sdk):
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
    )
    assert output["result"]["verified"] and "resolution" not in output["result"]
    assert [row["method"] for row in events] == ["GET", "GET", "POST"]


def test_explicit_renew_and_release_keep_lease_and_ownership_guards(sdk):
    _hold(sdk.api, "n1", ACTOR)
    output, events = command(sdk, "n1", "renew", "--duration", "1h")
    assert output["result"]["verified"]
    assert sdk.transport.commands == ["lock n1 1h"]
    assert sum(row["method"] == "POST" for row in events) == 1
    output, events = command(sdk, "n1", "release")
    assert output["result"]["verified"] and sdk.transport.commands[-1] == "unlock n1"
    assert sum(row["method"] == "POST" for row in events) == 1


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_claim_keeps_original_booked_duration(sdk):
    from datetime import datetime, timezone

    node = sdk.bots[0].state.bot_state["n1"]
    node["booking_list"].append(
        {
            "user_id": ACTOR,
            "start_time": int(datetime.now(timezone.utc).timestamp()),
            "duration": 60,
            "is_notified": True,
        }
    )
    output, events = command(sdk, "n1", "claim", "--exact-lease-times")
    assert output["result"]["verified"] and sdk.transport.commands == ["lock n1"]
    assert 0 < output["result"]["verified_snapshot"]["resources"][0]["holders"][0]["remaining_seconds"] <= 60
    assert sum(row["method"] == "POST" for row in events) == 1


@pytest.mark.parametrize("api", ["QUEUE"], indirect=True)
def test_book_can_queue_a_busy_node_without_converting_to_lock(sdk):
    _hold(sdk.api, "n1", "neighbor")
    output, events = command(sdk, "n1", "book", "--duration", "2h")
    assert output["result"]["verified"] and sdk.transport.commands == ["book n1 2h"]
    assert sum(row["method"] == "POST" for row in events) == 1


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
@pytest.mark.parametrize("operation", ["renew", "release", "claim"])
def test_node_only_card_lifecycle_operations_report_their_limit(sdk, operation):
    output, events = command(sdk, "n1", operation, code=2)
    assert output["error"] == "unsupported_automatic_operation"
    assert output["command_sent"] is False
    no_post(events)


def test_legacy_services_reserve_post_readback_and_keep_the_same_budget(sdk):
    def legacy(path, body):
        if path == "/api/agent-skill":
            body["capabilities"].remove("coherent_resource_snapshot")

    sdk.transport.transform = legacy
    output, events = command(sdk, "n1", "lock", "--duration", "1h")
    assert output["result"]["verified"]
    assert len(events) == 10 and [row["method"] for row in events].count("POST") == 1
    assert events[-1]["path"] == "/api/agent/resources"


def test_selected_read_can_exhaust_shared_budget_but_never_write(sdk, monkeypatch):
    # Reused two-scope scan uses 3; the selected action needs at least 3 more.
    monkeypatch.setattr(sdk.module, "MAX_CALLS", 5)
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_request_budget_exhausted"
    assert output["clarification"]["request_budget"]["http_limit"] == 5
    assert len(events) == 3
    no_post(events)


def test_pending_enrollment_blocks_even_an_observed_unique_target(sdk):
    pending = sdk.catalog / "unfinished"
    pending.mkdir(mode=0o700)
    # An unread/malformed pending record remains a catalog gap, never an empty group.
    (pending / "enrollment.json").write_text("{}")
    (pending / "enrollment.json").chmod(0o600)
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_scope_incomplete"
    no_post(events)


def test_fresh_selected_read_cannot_use_an_aged_other_scope(sdk, monkeypatch):
    import time

    now = [100.0]
    monkeypatch.setattr(sdk.module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep, time=time.time))

    def age(path, body):
        if (
            path == "/api/agent/resources"
            and body["bot_id"] == sdk.bot_ids[0]
            and sdk.transport.reads[body["bot_id"]] == 2
        ):
            now[0] += 6

    sdk.transport.transform = age
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] in {"automatic_snapshot_stale", "resource_snapshot_stale_or_unverifiable"}
    no_post(events)


def test_explicit_duration_and_resource_caps_are_checked_before_post(sdk):
    def limit(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["effective_max_lock_duration_seconds"] = 60

    sdk.transport.transform = limit
    output, events = command(sdk, "n1", "lock", "--duration", "2h", code=2)
    assert output["error"] == "automatic_duration_limit"
    no_post(events)


def test_429_read_retry_is_bounded_and_never_becomes_a_write(sdk, monkeypatch):
    monkeypatch.setattr(credential_budget, "read_limit", 2)
    original = sdk.transport.open

    def limited(request, timeout=None):
        try:
            return original(request, timeout)
        except HTTPError as error:
            if error.code == 429:
                error.headers.replace_header("Retry-After", "0")
            raise

    sdk.transport.open = limited
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "http_429"
    assert sum(row["status"] == 429 for row in events) == 2
    assert len(events) <= 12
    no_post(events)


def test_post_observation_failure_is_not_reported_as_zero_post(sdk):
    original = sdk.transport.open

    def missing(request, timeout=None):
        response = original(request, timeout)
        if request.get_method() == "POST":
            data = json.loads(response.getvalue())
            data.pop("resource_snapshot")
            changed = io.BytesIO(json.dumps(data).encode())
            changed.headers = response.headers
            return changed
        return response

    sdk.transport.open = missing
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "mutation_not_confirmed_read_resources_do_not_replay"
    assert output["command_sent"] is True
    assert output["command_outcome"] == "unknown"
    assert output["retry_mutation"] is False
    assert output["next_command"] == ["resources-all", "--mine"]
    assert sum(row["method"] == "POST" for row in events) == 1


def test_required_model_count_and_quota_are_preserved(sdk):
    sdk.client.patch(
        f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": [{"model": "P900", "count": 2}]}},
    ).raise_for_status()
    output, events = command(
        sdk, "n1", "lock", "--model", "P900", "--minimum-model-count", "3", "--duration", "1h", code=2
    )
    assert output["error"] == "selected_resource_model_unverified_or_insufficient"
    no_post(events)

    def quota(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"]["max_resources_per_user"] = 0

    sdk.transport.transform = quota
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_resource_limit"
    no_post(events)


@pytest.mark.parametrize("value", [False, "false", "unknown", None])
def test_automatic_access_permission_must_be_true(sdk, value):
    def change(path, body):
        if path == "/api/agent/resources":
            body["context"]["agent_access_enabled"] = value

    sdk.transport.transform = change
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_scope_unhealthy"
    no_post(events)


@pytest.mark.parametrize("field", ["max_resources_per_user", "effective_max_lock_duration_seconds"])
@pytest.mark.parametrize("value", ["1", None, True, -2])
def test_malformed_present_policy_never_means_unlimited(sdk, field, value):
    def change(path, body):
        if path == "/api/agent/resources":
            body["context"]["bot_rules"][field] = value

    sdk.transport.transform = change
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_policy_invalid"
    no_post(events)


def test_last_pre_dispatch_guard_rechecks_time_after_model_validation(sdk, monkeypatch):
    import time

    sdk.client.patch(
        f"/api/bots/{sdk.bot_ids[0]}/node-hardware",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": [{"model": "P900", "count": 2}]}},
    ).raise_for_status()
    now = [100.0]
    monkeypatch.setattr(sdk.module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep, time=time.time))
    matches = sdk.module.Client._matches_model
    calls = [0]

    def slow(*args):
        calls[0] += 1
        result = matches(*args)
        if calls[0] == 2:
            now[0] += 6
        return result

    monkeypatch.setattr(sdk.module.Client, "_matches_model", staticmethod(slow))
    output, events = command(sdk, "n1", "lock", "--model", "P900", "--duration", "1h", code=2)
    assert output["error"] == "automatic_snapshot_stale"
    assert calls[0] == 2
    no_post(events)


@pytest.mark.parametrize("captured", [None, "invalid", "2026-01-01T00:00:00"])
def test_unverifiable_observation_instant_stops_before_post(sdk, captured):
    def change(path, body):
        if path == "/api/agent/resources":
            body["captured_at"] = captured

    sdk.transport.transform = change
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_snapshot_unverifiable"
    no_post(events)


def test_aggregation_return_overhead_is_included_in_snapshot_age(sdk, monkeypatch):
    import time

    now = [100.0]
    monkeypatch.setattr(sdk.module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep, time=time.time))
    original = sdk.module.resources_all

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 6
        return result

    monkeypatch.setattr(sdk.module, "resources_all", delayed)
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_snapshot_stale"
    no_post(events)


def test_alias_change_is_not_hidden_by_a_still_matching_ip(sdk):
    _hold(sdk.api, "n1", ACTOR)
    sdk.bots[0].config.set_val("NODE_ALIASES", {"n1": "19"})
    sdk.client.put(
        f"/api/bots/{sdk.bot_ids[0]}/node-access",
        headers=sdk.api["auth_header"],
        json={"version": 1, "nodes": {"n1": {"host": "192.0.2.19"}}},
    ).raise_for_status()

    def change(path, body):
        if (
            path == "/api/agent/resources"
            and body["bot_id"] == sdk.bot_ids[0]
            and sdk.transport.reads[body["bot_id"]] == 2
        ):
            body["resources"][0]["alias"] = "changed"

    sdk.transport.transform = change
    output, events = command(sdk, "19", "release", code=2)
    assert output["error"] == "automatic_target_changed"
    no_post(events)


@pytest.mark.parametrize("operations", ["unlock", {"lock": True}])
@pytest.mark.parametrize("fresh_only", [False, True])
def test_supported_operations_never_uses_substring_or_dictionary_membership(sdk, operations, fresh_only):
    def change(path, body):
        if path == "/api/agent/resources" and (not fresh_only or sdk.transport.reads[body["bot_id"]] == 2):
            body["supported_operations"] = operations

    sdk.transport.transform = change
    output, events = command(sdk, "n1", "lock", "--duration", "1h", code=2)
    assert output["error"] == "automatic_operations_invalid"
    assert output["command_sent"] is False
    no_post(events)


def test_explicit_empty_connection_never_widens_to_saved_catalog(sdk):
    output, events = sdk.run(
        "--connection", "", "command", "--operation", "lock", "--node", "n1", "--whole-node", "--duration", "1h", code=2
    )
    assert output["error"] == "private_connection_required"
    assert events == []


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("device", ["0", "dev0", "DEV0"])
def test_chat_device_labels_select_the_exact_same_card(sdk, scoped, device):
    prefix = ["--connection", str(sdk.connections[0])] if scoped else []
    output, events = sdk.run(
        *prefix, "command", "--operation", "slock", "--node", "n1", "--device", device, "--duration", "4h"
    )
    assert output["result"]["verified"]
    assert sdk.transport.commands == ["slock n1 dev0 4h"]
    assert sum(e["method"] == "POST" for e in events) == 1


@pytest.mark.parametrize("api", ["DEVICE"], indirect=True)
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("devices", [("dev00",), ("dev-1",), ("dev0", "0")])
def test_malformed_or_duplicate_chat_device_labels_never_write(sdk, scoped, devices):
    prefix = ["--connection", str(sdk.connections[0])] if scoped else []
    flags = [part for device in devices for part in ("--device", device)]
    _, events = sdk.run(*prefix, "command", "--operation", "slock", "--node", "n1", *flags, "--duration", "4h", code=2)
    assert not sdk.transport.commands and all(e["method"] != "POST" for e in events)
