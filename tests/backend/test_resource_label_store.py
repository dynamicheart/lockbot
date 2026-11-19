"""Private label metadata only; no catalogue, resource service or credential reads."""

import json
import os
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import SCRIPT, helper

NATIVE = "s-" + "a" * 64 + "/node07"
OTHER = "s-" + "b" * 64 + "/node07"


def write_private(path, value):
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    return helper.ResourceLabelStore(tmp_path / "resource-labels.json")


def test_missing_read_is_side_effect_free_and_does_not_construct_clients(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("labels must not inspect runtime sources or Keys")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "listed_connections", forbidden)
    monkeypatch.setattr(helper, "private_bytes", forbidden)
    parent = tmp_path / "missing" / "private"
    value = helper.ResourceLabelStore(parent / "labels.json").read()
    assert value == {"schema_version": 1, "resources": {}} and not parent.exists()
    existing = tmp_path / "not-private"
    existing.mkdir(mode=0o755)
    assert helper.ResourceLabelStore(existing / "missing.json").read() == value
    assert list(existing.iterdir()) == []


def test_default_location_uses_saved_user_metadata_directory_only(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "connection_catalog", lambda scope: tmp_path / "connections")
    value = helper.ResourceLabelStore()
    assert value.path == tmp_path / "resource-labels.json"
    assert value.read() == {"schema_version": 1, "resources": {}}
    assert list(tmp_path.iterdir()) == []


def test_display_spelling_dedup_removal_and_clear_are_scoped(store):
    assert store.update(NATIVE, add="Ｐ８００") == {"ref": NATIVE, "labels": ["Ｐ８００"], "changed": True}
    original = store.path.read_bytes()
    assert store.update(NATIVE, add="p800")["changed"] is False
    assert store.path.read_bytes() == original
    store.update(NATIVE, add="我的 测试")
    store.update(OTHER, add="separate")
    assert store.update(NATIVE, remove="P800")["labels"] == ["我的 测试"]
    assert store.update(NATIVE, clear=True) == {"ref": NATIVE, "labels": [], "changed": True}
    assert store.update(NATIVE, clear=True)["changed"] is False
    assert store.read() == {"schema_version": 1, "resources": {OTHER: {"labels": ["separate"]}}}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "label",
    ["", " padded", "padded ", "bad\ntext", True, "é" * 41, "ΐ" * 20, "lbr_" + "secret" * 7, "ＬＢＰ＿" + "Ａ" * 30],
)
def test_invalid_labels_precede_any_directory_creation(tmp_path, label):
    target = tmp_path / "absent" / "labels.json"
    with pytest.raises(helper.ClientError, match="resource_label_invalid"):
        helper.ResourceLabelStore(target).update(NATIVE, add=label)
    assert not target.parent.exists()


def test_native_and_external_refs_are_canonical_full_pure_locators(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("reference validation must remain offline")

    monkeypatch.setattr(helper, "listed_connections", forbidden)
    monkeypatch.setattr(helper, "Client", forbidden)
    external = helper.mixed_reference("c" * 64, "x-example/node%2007")
    assert helper.resource_label_reference(NATIVE) == NATIVE
    assert helper.resource_label_reference(external) == external
    assert helper.resource_label_text("Straße") == "strasse"
    assert helper.resource_label_text("Ｐ８００") == "p800"


@pytest.mark.parametrize(
    "reference",
    [
        "s-" + "a" * 12 + "/node07",
        "s-" + "A" * 64 + "/node07",
        "x-example/node07",
        "s-" + "a" * 64 + "/%6Eode07",
        "s-" + "a" * 64 + "/lbr_" + "secret" * 6,
        "s-" + "a" * 64 + "/%EF%BD%8C%EF%BD%82%EF%BD%92%EF%BC%BF" + "1" * 30,
    ],
)
def test_invalid_or_secret_decoded_reference_never_opens_store(tmp_path, reference):
    target = tmp_path / "absent" / "labels.json"
    with pytest.raises(helper.ClientError, match="resource_label_reference_invalid"):
        helper.ResourceLabelStore(target).update(reference, add="tag")
    assert not target.parent.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":1,"schema_version":1,"resources":{}}',
        {"schema_version": True, "resources": {}},
        {"schema_version": 1, "resources": {}, "snapshot": {}},
        {"schema_version": 1, "resources": {NATIVE: {"labels": ["p800", "Ｐ８００"]}}},
        {"schema_version": 1, "resources": {NATIVE: {"labels": ["ok"], "token": "not-allowed"}}},
        {"schema_version": 1, "resources": {"x-provider/node": {"labels": ["ok"]}}},
    ],
)
def test_strict_schema_refuses_duplicate_keys_runtime_fields_and_bad_refs(store, payload):
    write_private(store.path, payload)
    original = store.path.read_bytes()
    with pytest.raises(helper.ClientError, match="resource_label_store_invalid"):
        store.read()
    with pytest.raises(helper.ClientError, match="resource_label_store_invalid"):
        store.update(NATIVE, clear=True)
    assert store.path.read_bytes() == original


def test_resource_label_and_byte_limits_preserve_previous_data(store):
    for number in range(8):
        store.update(NATIVE, add=f"tag-{number}")
    original = store.path.read_bytes()
    assert store.update(NATIVE, add="TAG-7")["changed"] is False
    with pytest.raises(helper.ClientError, match="resource_label_limit_exceeded"):
        store.update(NATIVE, add="ninth")
    assert store.path.read_bytes() == original
    resources = {"s-" + "a" * 64 + f"/node-{i}": {"labels": ["tag"]} for i in range(1024)}
    write_private(store.path, {"schema_version": 1, "resources": resources})
    original = store.path.read_bytes()
    with pytest.raises(helper.ClientError, match="resource_label_resource_limit_exceeded"):
        store.update(OTHER, add="extra")
    assert store.path.read_bytes() == original
    write_private(store.path, b"x" * (1024 * 1024 + 1))
    with pytest.raises(helper.ClientError, match="resource_label_store_too_large"):
        store.read()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/owner/permission regression")
def test_symlink_hardlink_and_nonprivate_metadata_are_not_read_or_replaced(store, tmp_path):
    store.update(NATIVE, add="original")
    original = store.path.read_bytes()
    store.path.chmod(0o644)
    with pytest.raises(helper.ClientError):
        store.read()
    store.path.chmod(0o600)
    alias = tmp_path / "linked.json"
    os.link(store.path, alias)
    with pytest.raises(helper.ClientError, match="resource_label_private_file_required"):
        store.update(NATIVE, add="no")
    alias.unlink()
    store.path.rename(alias)
    store.path.symlink_to(alias)
    with pytest.raises(helper.ClientError):
        store.read()
    with pytest.raises(helper.ClientError):
        store.update(NATIVE, add="no")
    assert alias.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent permission regression")
def test_nonprivate_parent_is_not_changed_and_wrong_owner_is_rejected(store, monkeypatch):
    store.update(NATIVE, add="original")
    store.path.parent.chmod(0o755)
    with pytest.raises(helper.ClientError, match="resource_label_private_directory_required"):
        store.read()
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o755
    store.path.parent.chmod(0o700)
    monkeypatch.setattr(helper.os, "geteuid", lambda: store.path.stat().st_uid + 1)
    with pytest.raises(helper.ClientError, match="resource_label_private_directory_required"):
        store.read()


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_staging_failures_leave_original_and_remove_temporary_files(store, monkeypatch, stage):
    store.update(NATIVE, add="original")
    original = store.path.read_bytes()
    inode = store.path.stat().st_ino

    def fail(*_args, **_kwargs):
        raise OSError("synthetic failure before commit")

    monkeypatch.setattr(helper.os, stage, fail)
    with pytest.raises(helper.ClientError, match="resource_label_store_unavailable"):
        store.update(NATIVE, add="new")
    assert store.path.read_bytes() == original and store.path.stat().st_ino == inode
    assert {path.name for path in store.path.parent.iterdir()} == {store.path.name, store.lock_path.name}


def test_arbitrary_preexisting_lock_is_not_adopted_or_truncated(store):
    write_private(store.lock_path, b"unrelated private file")
    original = store.lock_path.read_bytes()
    with pytest.raises(helper.ClientError, match="resource_label_lock_invalid"):
        store.update(NATIVE, add="tag")
    assert not store.path.exists() and store.lock_path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock contention regression")
def test_lock_contention_is_bounded_and_persistent_inode_survives(store, monkeypatch):
    import fcntl

    store.update(NATIVE, add="original")
    inode = store.lock_path.stat().st_ino
    monkeypatch.setattr(helper, "RESOURCE_LABEL_LOCK_TIMEOUT", 0.04)
    with store.lock_path.open("r+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(helper.ClientError, match="resource_label_lock_timeout"):
            store.update(NATIVE, add="blocked")
        assert time.monotonic() - started < 1
    store.update(NATIVE, add="after")
    assert store.lock_path.stat().st_ino == inode
    assert store.read()["resources"][NATIVE]["labels"] == ["original", "after"]


def test_cooperative_subprocess_writers_reread_latest_without_lost_updates(store):
    program = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('label_worker',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m.ResourceLabelStore(sys.argv[2]).update(sys.argv[3],add=sys.argv[4])"
    )

    def add(number):
        reference = NATIVE if number < 6 else OTHER
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", program, str(SCRIPT), str(store.path), reference, f"tag-{number}"],
            capture_output=True,
            text=True,
            timeout=12,
        )
        assert result.returncode == 0, result.stderr

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(add, range(12)))
    value = store.read()["resources"]
    assert set(value[NATIVE]["labels"]) == {f"tag-{number}" for number in range(6)}
    assert set(value[OTHER]["labels"]) == {f"tag-{number}" for number in range(6, 12)}


def test_windows_write_open_extension_preserves_default_access_without_claiming_windows_e2e(monkeypatch):
    windows = object.__new__(helper.WindowsPrivateFiles)
    calls = []
    windows._open_handle = lambda path, **options: calls.append(options) or 123
    windows.msvcrt = SimpleNamespace(open_osfhandle=lambda handle, flags: (handle, flags))
    windows.close = lambda handle: None
    monkeypatch.setattr(helper.os, "O_BINARY", 0, raising=False)
    assert windows.open_fd("synthetic-existing")[1] == os.O_RDONLY
    assert windows.open_fd("synthetic-lock", write=True)[1] == os.O_RDWR
    assert calls == [{"write": False}, {"write": True}]


def test_unknown_user_path_expansion_is_a_controlled_private_error(monkeypatch):
    def unavailable(_path):
        raise RuntimeError("synthetic private path must not escape")

    monkeypatch.setattr(helper.Path, "expanduser", unavailable)
    with pytest.raises(helper.ClientError, match="^resource_label_path_invalid$") as caught:
        helper.ResourceLabelStore("~unknown-user/labels.json")
    assert "private path" not in str(caught.value)


def test_empty_interrupted_lock_initialization_recovers_without_changing_old_metadata(store, monkeypatch):
    value = {"schema_version": 1, "resources": {NATIVE: {"labels": ["original"]}}}
    write_private(store.path, value)
    original = store.path.read_bytes()
    real_write = helper.os.write
    interrupted = [False]

    def stop_before_marker(fd, data):
        if not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt
        return real_write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "write", stop_before_marker)
        with pytest.raises(KeyboardInterrupt):
            store.update(NATIVE, add="interrupted")
    assert store.path.read_bytes() == original and store.lock_path.read_bytes() == b""
    inode = store.lock_path.stat().st_ino
    assert store.update(NATIVE, add="after")["labels"] == ["original", "after"]
    assert store.lock_path.stat().st_ino == inode
    assert store.lock_path.read_bytes() == helper._RESOURCE_LABEL_LOCK_MAGIC


@pytest.mark.parametrize("marker", [b"l", b"lock", helper._RESOURCE_LABEL_LOCK_MAGIC[:-1]])
def test_nonempty_partial_lock_markers_remain_unchanged(store, marker):
    write_private(store.lock_path, marker)
    with pytest.raises(helper.ClientError, match="resource_label_lock_invalid"):
        store.update(NATIVE, add="no")
    assert store.lock_path.read_bytes() == marker and not store.path.exists()


def test_short_marker_writes_finish_and_zero_write_fails_without_truncation(store, monkeypatch):
    real_write = helper.os.write

    def short(fd, data):
        return real_write(fd, data[:2])

    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "write", short)
        store.update(NATIVE, add="original")
    assert store.lock_path.read_bytes() == helper._RESOURCE_LABEL_LOCK_MAGIC
    other = helper.ResourceLabelStore(store.path.with_name("other.json"))
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "write", lambda *_: 0)
        with pytest.raises(helper.ClientError, match="resource_label_store_unavailable"):
            other.update(NATIVE, add="no")
    assert not other.path.exists() and other.lock_path.read_bytes() == b""
    assert other.update(NATIVE, add="after")["changed"]


def test_failed_temporary_creation_does_not_delete_an_existing_private_file(store, monkeypatch):
    store.update(NATIVE, add="original")
    original = store.path.read_bytes()
    occupied = store.path.parent / ".resource-labels-fixed"
    write_private(occupied, b"unrelated private content")
    monkeypatch.setattr(helper.secrets, "token_hex", lambda *_: "fixed")
    with pytest.raises(helper.ClientError, match="resource_label_store_unavailable"):
        store.update(NATIVE, add="no")
    assert store.path.read_bytes() == original and occupied.read_bytes() == b"unrelated private content"


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir-fd identity check; Windows uses lstat")
def test_lock_path_inode_must_still_match_the_locked_file(store, monkeypatch):
    store.update(NATIVE, add="original")
    original, real_stat = store.path.read_bytes(), helper.os.stat

    def changed(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == store.lock_path.name and "dir_fd" in kwargs:
            return SimpleNamespace(st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino + 1)
        return result

    monkeypatch.setattr(helper.os, "stat", changed)
    with pytest.raises(helper.ClientError, match="resource_label_lock_invalid"):
        store.update(NATIVE, add="no")
    assert store.path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir-fd replacement lifecycle regression")
@pytest.mark.parametrize("fail_replace", [False, True])
def test_temporary_cleanup_never_unlinks_a_replacement_inode(store, monkeypatch, fail_replace):
    store.update(NATIVE, add="original")
    original, real_replace = store.path.read_bytes(), helper.os.replace
    monkeypatch.setattr(helper.secrets, "token_hex", lambda *_: "fixed")
    temporary = store.path.parent / ".resource-labels-fixed"

    def replaced(source, target, **options):
        if not fail_replace:
            real_replace(source, target, **options)
        foreign = store.path.parent / ".foreign-stage"
        write_private(foreign, b"another writer's inode")
        real_replace(foreign, temporary)
        if fail_replace:
            raise OSError("synthetic replacement failure")

    monkeypatch.setattr(helper.os, "replace", replaced)
    if fail_replace:
        with pytest.raises(helper.ClientError, match="resource_label_store_unavailable"):
            store.update(NATIVE, add="new")
        assert store.path.read_bytes() == original
    else:
        assert store.update(NATIVE, add="new")["changed"]
    assert temporary.read_bytes() == b"another writer's inode"


def test_windows_directory_handle_lifetime_is_retained_with_mock_acl_primitives(store, monkeypatch):
    events = []
    storage = SimpleNamespace(
        _open_handle=lambda path, **options: events.append(("open", options)) or 123,
        close=lambda handle: events.append(("close", handle)),
    )
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, "name", "nt")
        patch.setattr(helper, "windows_private_files", lambda: storage)
        with store._directory() as directory:
            during = list(events)
            directory_open = directory is True
    assert directory_open and during == [("open", {"directory": True})]
    assert events == [("open", {"directory": True}), ("close", 123)]
