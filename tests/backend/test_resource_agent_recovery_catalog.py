"""Recovery reuse and per-invocation duplicate coalescing never rotate a Key."""

# ruff: noqa: F811 -- imported pytest fixtures
import importlib.util
import json
import time
from pathlib import Path

import pytest
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_agent_helper import helper, write_private
from backend.test_resource_agent_helper_api import Bridge, installed_bundle
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_api_resources import ACTOR, PLATFORM, api  # noqa: F401


def test_recovering_two_bindings_three_times_reuses_catalog_and_preserves_seed_and_labels(
    client, api, db_session, tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCKBOT_HOME", str(tmp_path / "private-home"))
    created = client.post(
        "/api/bots",
        headers=api["auth_header"],
        json={
            "name": "Second group",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "test model"},
        },
    )
    bot_id = created.json()["id"]
    assert client.put(
        f"/api/bots/{bot_id}/resource-auth", headers=api["auth_header"], json={"enabled": True}
    ).is_success
    pending = client.post("/api/resource-auth/bindings", json={}).json()
    service.handle_chat_binding(
        platform=PLATFORM, external_user_id=ACTOR, bot_id=bot_id, action="bind", argument=pending["bind_code"]
    )
    api["manager"].start_bot(
        bot_id,
        {
            "BOT_TYPE": "NODE",
            "BOT_NAME": "Second group",
            "BOT_ID": bot_id,
            "DATA_DIR": str(tmp_path / "second-bot"),
            "CLUSTER_CONFIGS": {"n1": "test model"},
        },
    )
    root = installed_bundle(tmp_path / "bundle")
    options = {"root": root, "opener": Bridge(client)}
    seed_key = write_private(tmp_path / "seed.key", api["headers"]["Authorization"].split(" ", 1)[1])
    initialized = helper.Client(approved_origin="http://testserver", **options).initialize(
        seed_key, None, agent_label="seed-client"
    )
    seed = Path(initialized["connection"])
    seed_bytes, key_bytes = seed.read_bytes(), seed_key.read_bytes()
    recovered_path = None
    custom_bytes = None
    for index in range(3):
        session = helper.Client(seed, **options)
        result = session.recover(None)
        assert session.calls == 3  # anonymous discovery, context, one recovery request
        recovered = next(item for item in result["connections"] if item["bot"]["id"] == bot_id)
        assert recovered["status"] == ("saved" if index == 0 else "existing")
        if index == 0:
            recovered_path = Path(recovered["connection"])
            saved = json.loads(recovered_path.read_text())
            saved["agent_label"] = "user-chosen-label"
            helper.write_private(recovered_path, json.dumps(saved).encode(), replace=True)
            custom_bytes = recovered_path.read_bytes()
        assert Path(recovered["connection"]) == recovered_path
        assert len(helper.listed_connections()["connections"]) == 2
        aggregate_result = helper.resources_all(root=root, opener=Bridge(client), mine=True)["result"]
        assert aggregate_result["complete"] and len(aggregate_result["groups"]) == 2
        assert aggregate_result["http_requests"] == 3
        assert seed.read_bytes() == seed_bytes and seed_key.read_bytes() == key_bytes
        assert recovered_path.read_bytes() == custom_bytes
        assert recovered_path.stat().st_mode & 0o077 == 0
        assert (recovered_path.parent / "key").read_text() == pending["credential"]
    old_key_bytes = (recovered_path.parent / "key").read_bytes()
    row = db_session.query(ResourceBinding).filter_by(public_id=pending["binding_id"]).one()
    rotated = service.rotate_confirmed_credential(
        db_session,
        row=row,
        instance_id=json.loads(seed.read_text())["instance_id"],
        canonical_name="Second group",
        deadline=time.time() + 60,
    )
    result = helper.Client(seed, **options).recover(None)
    updated = next(item for item in result["connections"] if item["bot"]["id"] == bot_id)
    new_path = Path(updated["connection"])
    assert updated["status"] == "saved" and new_path != recovered_path
    assert (new_path.parent / "key").read_text() == rotated
    assert recovered_path.read_bytes() == custom_bytes and (recovered_path.parent / "key").read_bytes() == old_key_bytes
    assert seed.read_bytes() == seed_bytes and seed_key.read_bytes() == key_bytes
    assert len(helper.listed_connections()["connections"]) == 3
    report = helper.resources_all(root=root, opener=Bridge(client), mine=True)["result"]
    assert not report["complete"] and len(report["groups"]) == 2 and len(report["unread"]) == 1
    assert report["unread"][0]["connection"]["connection"] == str(recovered_path)


@pytest.mark.parametrize("mine", [False, True])
def test_actual_packaged_aggregate_coalesces_exact_duplicates_without_extra_requests(aggregate, mine):
    first, second = aggregate.server(1), aggregate.server(2)
    for index in range(6):
        aggregate.record(first, f"a-{index}")
        aggregate.record(second, f"b-{index}")
    output = aggregate.run("resources-all", *(["--mine"] if mine else []))["result"]
    assert output["complete"] and len(output["groups"]) == 2
    assert output["catalog_connections"] == 12 and output["coalesced_catalog_connections"] == 10
    assert output["http_requests"] == 4
    assert all(len(server.events) == 2 for server in (first, second))
    assert all(len(group["equivalent_connections"]) == 6 for group in output["groups"])
    if mine:
        assert sum(len(group["snapshot"]["holdings"]) for group in output["groups"]) == 2


@pytest.mark.parametrize(
    "change", ["key", "actor", "instance", "malformed-key", "invalid-label", "private-mode", "key-symlink"]
)
def test_conflicting_or_invalid_catalog_records_keep_independent_verification_and_errors(aggregate, change):
    server = aggregate.server(1)
    aggregate.record(server, "a-original")
    other = aggregate.record(server, "b-conflict")
    record = json.loads(other.read_text())
    if change == "key":
        key = other.parent / "key"
        key.write_text("lbr_" + "rotated" * 5)
    elif change == "malformed-key":
        (other.parent / "key").write_text("not-a-valid-key")
    elif change == "actor":
        record["actor"] = {**record["actor"], "external_user_id": "actor-1-other"}
    elif change == "instance":
        record["instance_id"] = "other-instance"
    elif change == "private-mode":
        (other.parent / "key").chmod(0o644)
    elif change == "key-symlink":
        (other.parent / "key").unlink()
        (other.parent / "key").symlink_to(aggregate.catalog / "a-original/key")
    else:
        record["agent_label"] = "invalid\nlabel"
    other.write_text(json.dumps(record))
    result = aggregate.run("resources-all", "--mine")["result"]
    assert not result["complete"] and len(result["groups"]) == 1
    assert result["coalesced_catalog_connections"] == 0 and len(result["unread"]) == 1
    assert result["unread"][0]["connection"]["connection"] == str(other)
    assert sum(path == "/api/agent-skill" for path, *_ in server.events) == (2 if change == "instance" else 1)
    expected = {
        "key": "http_401",
        "actor": "context_actor_mismatch",
        "instance": "instance_mismatch",
        "malformed-key": "invalid_resource_key",
        "invalid-label": "invalid_agent_label",
        "private-mode": "private_file_permissions_or_size",
        "key-symlink": "private_file_unavailable",
    }[change]
    assert result["unread"][0]["error"].startswith(expected)


def test_same_key_identity_and_alias_on_different_origins_do_not_coalesce(aggregate):
    first, second = aggregate.server(1), aggregate.server(1)
    aggregate.record(first, "first-origin")
    aggregate.record(second, "second-origin")
    result = aggregate.run("resources-all", "--mine")["result"]
    assert result["complete"] and result["http_requests"] == 4 and len(result["groups"]) == 2
    assert result["coalesced_catalog_connections"] == 0
    assert {group["identity"]["origin"] for group in result["groups"]} == {first.origin, second.origin}


def test_failed_connections_cannot_be_used_as_duplicate_verification(aggregate):
    server = aggregate.server(1)
    server.status = 403
    aggregate.record(server, "first-error")
    aggregate.record(server, "second-error")
    report = aggregate.run("resources-all")["result"]
    assert not report["complete"] and report["groups"] == []
    assert report["http_requests"] == 4 and report["coalesced_catalog_connections"] == 0
    assert len(report["unread"]) == 2 and all(row["error"] == "http_403" for row in report["unread"])


def load_packaged_module(aggregate):
    spec = importlib.util.spec_from_file_location("recovery_catalog_packaged", aggregate.script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duplicate_observation_is_aged_once_at_aggregate_completion(aggregate, monkeypatch):
    server = aggregate.server(1)
    aggregate.record(server, "a-original")
    duplicate = aggregate.record(server, "b-duplicate")
    module = load_packaged_module(aggregate)
    original = module.private_bytes
    elapsed = [0]

    def read(path):
        if Path(path) == duplicate.parent / "key":
            elapsed[0] = 6
        return original(path)

    monkeypatch.setattr(module, "private_bytes", read)
    report = module.resources_all(aggregate.catalog, root=aggregate.script.parents[1], clock=lambda: elapsed[0])[
        "result"
    ]
    assert report["http_requests"] == 2 and report["coalesced_catalog_connections"] == 1
    assert not report["complete"] and len(report["groups"]) == 1
    snapshot = report["groups"][0]["snapshot"]
    assert snapshot["age_seconds"] == 6 and not snapshot["snapshot_fresh_for_write"]
    assert report["unread"][0]["error"] == "stale_or_unverifiable_snapshot"


def test_duplicate_private_comparisons_share_the_catalog_byte_budget(aggregate, monkeypatch):
    server = aggregate.server(1)
    for index in range(3):
        aggregate.record(server, str(index))
    module = load_packaged_module(aggregate)
    budget = module.CatalogScanBudget
    monkeypatch.setattr(module, "CatalogScanBudget", lambda deadline, clock: budget(deadline, clock, size=3 * 16385))
    report = module.resources_all(aggregate.catalog, root=aggregate.script.parents[1])["result"]
    assert not report["complete"] and report["http_requests"] == 2
    assert report["catalog_scan"]["reasons"] == ["byte_budget"]
    assert report["catalog_scan"]["bytes_reserved"] == 3 * 16385
    assert report["unread"][0]["error"] == "catalog_scan_incomplete"
