"""Existing private metadata must block credential reuse before authentication."""

# ruff: noqa: F811

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.test_resource_agent_helper import case, helper, write_private  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKBOT_HOME", str(tmp_path / "profile"))
    monkeypatch.chdir(tmp_path)


def saved_binding(case, *, service=None, instance="instance-a", name="old", key=None):
    directory = helper.new_catalog_entry("user", name)
    helper.make_private_directory(directory)
    key = key or case.tmp / "resource.key"
    record = {
        **case.connection,
        "approved_origin": service or case.base,
        "instance_id": instance,
        "key_file": os.path.relpath(key, directory),
    }
    return write_private(directory / "connection.json", record)


def bootstrap(case):
    return helper.Client(approved_origin=case.base, root=case.root)


def assert_no_auth(case):
    assert all(not any(name.lower() == "authorization" for name in headers) for _, _, headers, _ in case.state.requests)


@pytest.mark.parametrize("alias", ["relative", "absolute", "hardlink"])
def test_old_key_origin_blocks_auth_and_preserves_files(case, alias):
    selected = case.tmp / "resource.key"
    connection = saved_binding(case, service="https://old.example.invalid")
    if alias == "absolute":
        record = json.loads(connection.read_text())
        record["key_file"] = str(selected)
        write_private(connection, record)
    elif alias == "hardlink":
        alias_path = case.tmp / "alias.key"
        os.link(selected, alias_path)
        selected = alias_path
    old = {path: path.read_bytes() for path in (selected, connection)}
    with pytest.raises(helper.ClientError, match="^key_origin_mismatch$"):
        bootstrap(case).initialize(selected, case.tmp / "new.json")
    assert case.state.requests == []
    assert not (case.tmp / "new.json").exists()
    assert all(path.read_bytes() == content for path, content in old.items())


@pytest.mark.parametrize("existing", [False, True])
def test_new_seed_and_same_origin_reuse_still_initialize(case, existing):
    if existing:
        saved_binding(case)
    result = bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert result["connection_created"]
    assert [path for path, _, _, _ in case.state.requests] == ["/api/agent-skill", "/api/agent/context"]
    assert "Authorization" in case.state.requests[-1][2]


def test_same_origin_replaced_instance_is_rejected_before_auth(case):
    old = saved_binding(case, instance="previous-instance")
    before = old.read_bytes()
    with pytest.raises(helper.ClientError, match="^key_instance_mismatch$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert [row[0] for row in case.state.requests] == ["/api/agent-skill"]
    assert_no_auth(case)
    assert old.read_bytes() == before


def test_matching_binding_does_not_hide_conflicting_binding(case):
    saved_binding(case)
    saved_binding(case, service="https://other.example.invalid", name="second")
    with pytest.raises(helper.ClientError, match="^key_origin_mismatch$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert_no_auth(case)


@pytest.mark.parametrize("catalog", ["user", "legacy", "project"])
def test_all_supported_catalog_locations_keep_key_origin(case, monkeypatch, catalog):
    monkeypatch.delenv("LOCKBOT_HOME")
    monkeypatch.setattr(Path, "home", lambda: case.tmp / "home")
    root = {
        "user": helper.connection_catalog("user"),
        "legacy": Path.home() / ".config/lockbot/connections",
        "project": helper.connection_catalog("project"),
    }[catalog]
    helper.make_private_directory(root, parents=True)
    write_private(
        root / "connection.json",
        {
            **case.connection,
            "approved_origin": "https://old.example.invalid",
            "key_file": str(case.tmp / "resource.key"),
        },
    )
    with pytest.raises(helper.ClientError, match="^key_origin_mismatch$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert_no_auth(case)


def test_requested_project_catalog_is_checked_with_isolated_user_profile(case):
    root = helper.connection_catalog("project")
    helper.make_private_directory(root, parents=True)
    write_private(
        root / "connection.json",
        {
            **case.connection,
            "approved_origin": "https://old.example.invalid",
            "key_file": str(case.tmp / "resource.key"),
        },
    )
    with pytest.raises(helper.ClientError, match="^key_origin_mismatch$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json", scope="project")
    assert_no_auth(case)


@pytest.mark.parametrize("failure", ["malformed", "record_budget", "entry_budget", "byte_budget", "deadline"])
def test_incomplete_catalog_cannot_authenticate_even_new_seed(case, monkeypatch, failure):
    record = saved_binding(case, key=case.tmp / "unrelated.key")
    if failure == "malformed":
        write_private(record, "{")
    original = helper.CatalogScanBudget
    options = {"record_budget": {"records": 0}, "entry_budget": {"entries": 0}, "byte_budget": {"size": 0}}
    if failure != "malformed":
        monkeypatch.setattr(
            helper,
            "CatalogScanBudget",
            lambda deadline, clock: original(
                clock() - 1 if failure == "deadline" else deadline, clock, **options.get(failure, {})
            ),
        )
    with pytest.raises(helper.ClientError, match="^key_origin_catalog_incomplete$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert case.state.requests == []


def test_pending_enrollment_key_keeps_its_origin(case):
    directory = helper.new_catalog_entry("user", "pending")
    helper.make_private_directory(directory)
    os.link(case.tmp / "resource.key", directory / "key")
    write_private(
        directory / "enrollment.json",
        {
            "approved_origin": "https://old.example.invalid",
            "instance_id": "instance-a",
            "status": "requesting",
            "target_group": None,
        },
    )
    with pytest.raises(helper.ClientError, match="^key_origin_mismatch$"):
        bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert_no_auth(case)


def test_scans_metadata_once_without_reading_other_credentials(case, monkeypatch):
    other = write_private(case.tmp / "other.key", "do-not-read")
    records = [saved_binding(case, name=str(i), key=other) for i in range(8)]
    original = helper.private_bytes
    reads = []

    def read(path):
        reads.append(Path(path))
        assert Path(path) != other
        return original(path)

    monkeypatch.setattr(helper, "private_bytes", read)
    bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")
    assert all(reads.count(path) == 1 for path in records)
    assert reads.count(case.tmp / "resource.key") == 1


def test_packaged_cli_refuses_cross_origin_key_without_any_authorization(case):
    old = saved_binding(case, service="https://old.example.invalid")
    key = case.tmp / "resource.key"
    before = {path: path.read_bytes() for path in (old, key)}
    completed = subprocess.run(
        [
            sys.executable,
            str(case.root / "scripts/lockbot_cli.py"),
            "init",
            "--origin",
            case.base,
            "--key-file",
            str(key),
            "--destination",
            str(case.tmp / "new.json"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode != 0
    assert "key_origin_mismatch" in completed.stdout + completed.stderr
    assert key.read_text().strip() not in completed.stdout + completed.stderr
    assert_no_auth(case)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_unindexed_custom_metadata_is_not_arbitrarily_searched(case):
    write_private(
        case.tmp / "connections.json",
        {
            "connections": [{"origin": "https://old.example.invalid", "credential_ref": "resource.key"}],
        },
    )
    assert bootstrap(case).initialize(case.tmp / "resource.key", case.tmp / "new.json")["connection_created"]


def test_independent_key_copy_is_not_claimed_to_have_known_provenance(case):
    saved_binding(case, service="https://old.example.invalid")
    copied = write_private(case.tmp / "copy.key", (case.tmp / "resource.key").read_text())
    assert bootstrap(case).initialize(copied, case.tmp / "new.json")["connection_created"]
