"""Immutable per-run exports preserve the legacy board and publish membership last."""

import importlib.util
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from test_trace_merge import matrix

TOOLS = Path(__file__).resolve().parents[2] / "tools/trace-viewer"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("trace_dataset", TOOLS / "publish_dataset.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def export(tmp_path, **kwargs):
    source, _ = matrix(tmp_path, terminal=True)
    return source, publisher.export_dataset(source, tmp_path / "board", **kwargs)


@pytest.mark.parametrize("scenario", ["intent-context-reminder-configured", "intent-context-reminder-unavailable"])
def test_context_reminder_cases_export_actual_dialogue_without_catalog_omissions(tmp_path, scenario):
    source, _ = matrix(tmp_path, terminal=True, scenario=scenario)
    report = publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)
    dataset = tmp_path / "board/datasets" / report["dataset"]["dataset_id"]
    raw = json.loads((dataset / "raw.json").read_text())
    assert list(raw["cases"]) == [scenario]
    assert raw["cases"][scenario][0]["sessions"][0][0] == {"role": "user", "text": "Actual user request"}
    assert report["source_io_audit"]["prompts"] == 1
    assert report["source_io_audit"]["finals"] == 1


def test_terminal_export_is_lossless_and_does_not_read_or_write_legacy_board(tmp_path):
    board = tmp_path / "board"
    board.mkdir(mode=0o700)
    legacy = {name: ("Existing " + name).encode() for name in ("raw.json", "latest.json", "review-comments.sqlite3")}
    for name, content in legacy.items():
        (board / name).write_bytes(content)
    _, report = export(tmp_path, minimal_summary=True)
    metadata = report["dataset"]
    dataset = board / "datasets" / metadata["dataset_id"]
    raw_bytes = (dataset / "raw.json").read_bytes()
    raw = json.loads(raw_bytes)
    latest = json.loads((dataset / "latest.json").read_bytes())
    assert sha256(raw_bytes).hexdigest() == metadata["dataset_id"]
    assert metadata["raw_size_bytes"] == len(raw_bytes)
    assert metadata["case_count"] == 1  # No unselected catalog placeholders.
    trial = raw["cases"]["link-only"][0]
    session = trial["sessions"][0]
    assert {"role": "user", "text": "Actual user request"} in session
    assert {"role": "assistant", "text": "Actual native reply"} in session
    evidence = next(event for event in session if event["role"] == "tools")["evidence"][0]
    assert evidence["input"] == {"path": "/fixture/SKILL.md"}
    assert evidence["output"] == {"exit_code": 2, "error": "Synthetic failure"}
    assert latest["cases"]["link-only"][0]["sessions"] == []
    assert json.loads((board / "datasets/index.json").read_text())["datasets"] == [metadata["dataset_id"]]
    for name, content in legacy.items():
        assert (board / name).read_bytes() == content
    assert not report["publication_authorized"]
    for path in dataset.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert dataset.stat().st_mode & 0o777 == 0o700


def test_idempotent_export_preserves_files_and_old_registry_entries(tmp_path):
    source, first = export(tmp_path)
    root = tmp_path / "board/datasets"
    first_id = first["dataset"]["dataset_id"]
    before = {p.name: p.read_bytes() for p in (root / first_id).iterdir()}
    assert publisher.export_dataset(source, tmp_path / "board") == first
    assert before == {p.name: p.read_bytes() for p in (root / first_id).iterdir()}
    other, _ = matrix(tmp_path, version="c" * 16, terminal=True)
    second = publisher.export_dataset(other, tmp_path / "board")
    assert second["dataset"]["dataset_id"] != first_id
    assert json.loads((root / "index.json").read_text())["datasets"] == [first_id, second["dataset"]["dataset_id"]]
    assert before == {p.name: p.read_bytes() for p in (root / first_id).iterdir()}


def test_failed_validation_does_not_publish_membership(tmp_path, monkeypatch):
    source, _ = matrix(tmp_path, terminal=True)
    original = publisher.verify_dataset

    def interrupted(*args):
        original(*args)
        assert not (tmp_path / "board/datasets/index.json").exists()
        raise ValueError("Synthetic integrity failure before registry publication")

    monkeypatch.setattr(publisher, "verify_dataset", interrupted)
    with pytest.raises(ValueError, match="integrity failure"):
        publisher.export_dataset(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


def test_mutated_existing_dataset_is_never_repaired_or_overwritten(tmp_path):
    source, report = export(tmp_path)
    dataset = tmp_path / "board/datasets" / report["dataset"]["dataset_id"]
    target = dataset / "raw.json"
    target.write_bytes(b"Tampered raw")
    before = (tmp_path / "board/datasets/index.json").read_bytes()
    with pytest.raises(ValueError, match="bytes changed"):
        publisher.export_dataset(source, tmp_path / "board")
    assert target.read_bytes() == b"Tampered raw"
    assert (tmp_path / "board/datasets/index.json").read_bytes() == before


def test_different_summary_cannot_overwrite_same_raw_identity(tmp_path):
    source, _ = export(tmp_path)
    with pytest.raises(ValueError, match="overwrite"):
        publisher.export_dataset(source, tmp_path / "board", minimal_summary=True)


@pytest.mark.parametrize("name", ["datasets", "datasets/index.json", "datasets/.publish.lock"])
def test_rejects_symlink_publication_paths(tmp_path, name):
    source, _ = matrix(tmp_path, terminal=True)
    board = tmp_path / "board"
    board.mkdir(mode=0o700)
    target = board / name
    target.parent.mkdir(mode=0o700, exist_ok=True)
    private = tmp_path / "untouched"
    private.mkdir(mode=0o700)
    target.symlink_to(private)
    with pytest.raises((ValueError, OSError)):
        publisher.export_dataset(source, board)
    assert not list(private.iterdir())


def test_limits_are_not_raised_and_oversized_export_stays_unregistered(tmp_path):
    source, _ = matrix(tmp_path, terminal=True)
    reply = source / "cli/link-only/001/phase-1/response.json"
    reply.write_text(json.dumps({"text": "x" * (33 * 1024 * 1024)}))
    with pytest.raises(ValueError):
        publisher.export_dataset(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


def test_private_registry_is_bounded_and_does_not_accept_duplicate_ids(tmp_path):
    directory = tmp_path / "datasets"
    directory.mkdir(mode=0o700)
    path = directory / "index.json"
    path.write_text(json.dumps({"schema_version": 1, "datasets": ["a" * 64, "a" * 64]}))
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="registry"):
        publisher.registry(directory)
