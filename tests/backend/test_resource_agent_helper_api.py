"""Run the distributed helper against real resource API/core fixtures, not invented bot IDs."""

# ruff: noqa: F811
import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api.skill import skill_artifacts
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_agent_helper import helper, write_private
from backend.test_resource_api_resources import ACTOR, PLATFORM, api  # noqa: F401


class Bridge:
    def __init__(self, client):
        self.client = client

    def open(self, request, timeout):
        result = self.client.request(
            request.method, request.full_url, headers=dict(request.header_items()), content=request.data
        )
        if result.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, result.status_code, "refused", result.headers, io.BytesIO(result.content)
            )
        payload = io.BytesIO(result.content)
        payload.headers = result.headers
        return payload


def installed_bundle(tmp_path):
    _, bundle, meta = skill_artifacts()
    with zipfile.ZipFile(io.BytesIO(bundle)) as zipped:
        zipped.extractall(tmp_path)
    return tmp_path / meta["name"]


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_helper_real_api_init_acquire_renew_release(client, api, tmp_path):
    root = installed_bundle(tmp_path)
    key = write_private(tmp_path / "existing.key", api["headers"]["Authorization"].split(" ", 1)[1])
    original = key.read_bytes()
    connection = tmp_path / "connection.json"
    init = helper.Client(approved_origin="http://testserver", root=root, opener=Bridge(client))
    scope = init.initialize(key, connection)
    assert scope["bot"]["id"] == api["bot_id"]
    assert key.read_bytes() == original
    cards = ["0"] if scope["bot"]["type"] in {"DEVICE", "QUEUE_DEVICE"} else []
    for operation, duration in [("lock", "2m"), ("renew", "3m"), ("release", None)]:
        session = helper.Client(connection, root=root, opener=Bridge(client))
        result = session.command(operation, ["n1"], cards, duration, {"task_category": "unit_testing"})
        assert result["verified"] and not result["job_runner"]
    if scope["bot"]["type"] in {"QUEUE", "QUEUE_DEVICE"}:
        for operation, duration in [("book", "2m"), ("release", None)]:
            session = helper.Client(connection, root=root, opener=Bridge(client))
            assert session.command(operation, ["n1"], cards, duration)["verified"]
    saved = json.loads(connection.read_text())
    assert saved["bot_uuid"] == scope["bot"]["uuid"]
    assert saved["actor"] == scope["actor"]


@pytest.mark.parametrize("scope", ["explicit", "user", "project"])
def test_helper_key_recovery_creates_key_after_verified_chat_confirmation(client, api, tmp_path, monkeypatch, scope):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = installed_bundle(tmp_path)
    options = {"root": root, "opener": Bridge(client)}
    session = helper.Client(approved_origin="http://testserver", **options)
    directory = tmp_path / "enrollment" if scope == "explicit" else None
    options_for_scope = {"scope": "project"} if scope == "project" else {}
    pending = session.enroll(directory, purpose="recover", **options_for_scope)
    assert pending["target_group"] is None
    assert pending["bind_command"].startswith("bind REC-")
    path, state = helper.enrollment_state(pending["enrollment_dir"])
    session = helper.Client(approved_origin=state["approved_origin"], **options)
    assert session.check_enrollment(path, state, finish=True)["status"] == "pending"
    assert not (path / "key").exists()
    service.handle_chat_binding(
        platform=PLATFORM,
        external_user_id="new-agent-user",
        bot_id=api["bot_id"],
        action="bind",
        argument=pending["bind_command"].split(" ", 1)[1],
    )
    session = helper.Client(approved_origin=state["approved_origin"], **options)
    confirmed = session.check_enrollment(path, state, finish=True)
    assert confirmed["connection_created"]
    assert confirmed["outcome"] == "created"
    assert confirmed["bot"]["id"] == api["bot_id"]
    if scope != "explicit":
        assert helper.listed_connections()["connections"][0]["connection"] == confirmed["connection"]
    session = helper.Client(confirmed["connection"], **options)
    assert session.context()["actor"]["external_user_id"] == "new-agent-user"


@pytest.mark.parametrize("api", ["QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_helper_claims_own_booking_without_overriding_duration(client, api, tmp_path, monkeypatch):
    root = installed_bundle(tmp_path)
    options = {"root": root, "opener": Bridge(client)}
    key = write_private(tmp_path / "booking.key", api["headers"]["Authorization"].split(" ", 1)[1])
    connection = tmp_path / "booking.json"
    scope = helper.Client(approved_origin="http://testserver", **options).initialize(key, connection)
    cards = ["0"] if scope["bot"]["type"] == "QUEUE_DEVICE" else []
    target = "n1 dev0" if cards else "n1"
    endpoint = f"/api/bots/{api['bot_id']}/commands"
    assert (
        client.post(endpoint, headers=api["other_headers"], json={"command": f"lock {target} 10m"}).status_code == 200
    )
    with pytest.raises(helper.ClientError, match="booking_not_claimable"):
        helper.Client(connection, **options).command("claim", ["n1"], cards, None)
    assert helper.Client(connection, **options).command("book", ["n1"], cards, "2m")["verified"]
    with pytest.raises(helper.ClientError, match="booking_not_claimable"):
        helper.Client(connection, **options).command("claim", ["n1"], cards, None)
    assert client.post(endpoint, headers=api["other_headers"], json={"command": f"unlock {target}"}).status_code == 200
    with pytest.raises(helper.ClientError, match="claim_uses_booked_duration"):
        helper.Client(connection, **options).command("claim", ["n1"], cards, "1m")

    session = helper.Client(connection, **options)

    def unexpected_write(*args):
        pytest.fail("A lock must not send a request for a claimable booking")

    monkeypatch.setattr(session, "_post_command", unexpected_write)
    with pytest.raises(helper.ClientError, match="booking_claimable_use_explicit_claim"):
        session.command("lock", ["n1"], cards, "1m")

    session = helper.Client(connection, **options)
    read = session.resources

    def inconsistent_claim(context):
        snapshot = read(context)
        (item,) = session._selected(snapshot, ["n1"], cards)
        item["claim_command"] = "lock wrong-target"
        return snapshot

    monkeypatch.setattr(session, "resources", inconsistent_claim)
    with pytest.raises(helper.ClientError, match="unexpected_claim_command"):
        session.command("claim", ["n1"], cards, None)

    session = helper.Client(connection, **options)
    result = session.command("claim", ["n1"], cards, None)
    assert result["verified"]
    (resource,) = session._selected(result["verified_snapshot"], ["n1"], cards)
    assert len(resource["holders"]) == 1
    assert resource["holders"][0]["external_user_id"] == ACTOR
    assert 118 <= resource["holders"][0]["remaining_seconds"] <= 120
    assert resource["booking_list"] == []


@pytest.mark.parametrize("scope", ["explicit", "user"])
def test_helper_recovers_existing_groups_from_one_private_key(client, api, db_session, tmp_path, monkeypatch, scope):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def bind(name):
        created = client.post(
            "/api/bots",
            headers=api["auth_header"],
            json={
                "name": name,
                "bot_type": "NODE",
                "webhook_url": "https://example.invalid/hook",
                "cluster_configs": {"n1": "test model"},
            },
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert client.put(
            f"/api/bots/{identifier}/resource-auth", headers=api["auth_header"], json={"enabled": True}
        ).is_success
        issued = client.post("/api/resource-auth/bindings", json={}).json()
        service.handle_chat_binding(
            platform=PLATFORM,
            external_user_id=ACTOR,
            bot_id=identifier,
            action="bind",
            argument=issued["bind_code"],
        )
        return issued

    available = bind("Second group")
    unavailable = bind("Legacy group")
    row = db_session.query(ResourceBinding).filter_by(public_id=unavailable["binding_id"]).one()
    row.credential_encrypted = None
    db_session.commit()
    options = {"root": installed_bundle(tmp_path), "opener": Bridge(client)}
    original = api["headers"]["Authorization"].split(" ", 1)[1]
    key_file = write_private(tmp_path / "seed.key", original)
    seed = tmp_path / "seed.json"
    helper.Client(approved_origin="http://testserver", **options).initialize(key_file, seed)
    original_config = seed.read_bytes()
    destination = tmp_path / "received" if scope == "explicit" else None
    migration = helper.Client(seed, **options).recover(destination)
    entries = {entry["bot"]["name"]: entry for entry in migration["connections"]}
    assert entries["resbot-NODE"]["status"] == "existing"
    assert entries["Legacy group"]["status"] == "unavailable" and "connection" not in entries["Legacy group"]
    assert entries["Second group"]["status"] == "saved"
    received = helper.Client(entries["Second group"]["connection"], **options)
    assert received.context()["bot"]["name"] == "Second group"
    assert received.key == available["credential"]
    assert Path(entries["Second group"]["connection"]).stat().st_mode & 0o077 == 0
    assert not any(key["credential"] in json.dumps(migration) for key in (available, unavailable))
    assert key_file.read_text() == original and seed.read_bytes() == original_config
    if scope == "user":
        record = Path(entries["Second group"]["connection"])
        assert helper.listed_connections()["connections"][0]["connection"] == str(record)
        destination = record.parent.parent
    with pytest.raises(helper.ClientError, match="recovery_directory_exists"):
        helper.Client(seed, **options).recover(destination)
