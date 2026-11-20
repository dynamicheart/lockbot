"""Native aliases and continuation inputs must invalidate stale whole-run reviews."""

import ast
import copy
import json
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.trace_assessment import checked_assessment
from lockbot.trace_cli_assessment import FILES
from lockbot.trace_evidence_files import NATIVE_CONTEXT_FILES, NATIVE_PHASE_FILES, NATIVE_RENDER_FILES
from test_trace_cli_run_assessment import data_and_entry, overlay, publisher, review, saved_review, source_run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/trace-viewer"))
import synthetic_transcript as transcript  # noqa: E402

OMITTED = ("codex-native.jsonl", "codex-native.stderr", "runner.stderr", "native-session.json", "conversation.json")
WIRE = (
    json.dumps(
        {
            "direction": "receive",
            "message": {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "Native fixture answer."}},
            },
        }
    ).encode()
    + b"\n"
)


def payload(name):
    return WIRE if name.endswith(".jsonl") else b'{"fixture":"native-only"}\n'


def immutable_files(board, identity):
    return {
        name: (board / "datasets" / identity / name).read_bytes()
        for name in ("raw.json", "latest.json", "metadata.json")
    }


@pytest.mark.parametrize("name", NATIVE_PHASE_FILES)
def test_late_native_or_continuation_file_rejects_old_review_in_every_import_path(tmp_path, name):
    source, target = source_run(tmp_path)
    record, report = review(tmp_path, source, target)
    board = tmp_path / "board"
    initial = publisher.export_dataset(source, board, minimal_summary=True, cli_assessments=True)
    identity = initial["dataset"]["dataset_id"]
    originals = immutable_files(board, identity)
    before_index = (board / "datasets/index.json").read_bytes()
    data, _ = data_and_entry(source, target)
    directory = source / "cli/approved-enrollment/001/phase-2"
    (directory / name).write_bytes(payload(name))
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="binding changed"):
        overlay.attach_review(data, source, record, report)
    assert data == before
    fresh, entry = data_and_entry(source, target)
    assert entry["binding"] != record["binding"]
    entry.update(reviews=[record], semantic="pass")
    with pytest.raises(ValueError, match="provenance"):
        checked_assessment(entry, entry["target"]["source_run"], fresh)
    with pytest.raises(ValueError, match="binding changed|source identity changed"):
        publisher.export_dataset(
            source,
            board,
            minimal_summary=True,
            cli_assessments=True,
            assessment_reviews=[(saved_review(tmp_path, record), report)],
        )
    assert immutable_files(board, identity) == originals
    assert (board / "datasets/index.json").read_bytes() == before_index


@pytest.mark.parametrize("name", OMITTED)
@pytest.mark.parametrize("mutation", ["edit", "remove", "symlink", "oversize", "wrong-phase"])
def test_present_alias_mutation_cannot_reuse_a_passing_review(tmp_path, name, mutation):
    source, target = source_run(tmp_path)
    directory = source / "cli/approved-enrollment/001/phase-2"
    path = directory / name
    path.write_bytes(payload(name))
    record, report = review(tmp_path, source, target)
    data, entry = data_and_entry(source, target)
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    if mutation == "edit":
        path.write_bytes(payload(name) + b" ")
    elif mutation == "remove":
        path.unlink()
    elif mutation == "symlink":
        moved = directory / "original-native-fixture"
        path.rename(moved)
        path.symlink_to(moved)
    elif mutation == "oversize":
        limit = transcript.MAX_NATIVE_EVIDENCE_BYTES if name in NATIVE_RENDER_FILES else transcript.MAX_TRACE_BYTES
        with path.open("r+b") as stream:
            stream.truncate(limit + 1)
    else:
        path.rename(directory.parent / "phase-1" / name)
    before = copy.deepcopy(data)
    with pytest.raises(ValueError):
        overlay.attach_review(data, source, record, report)
    assert data == before
    with pytest.raises(ValueError):
        publisher.export_dataset(
            source,
            tmp_path / "changed-board",
            minimal_summary=True,
            cli_assessments=True,
            assessment_reviews=[(saved_review(tmp_path, record), report)],
        )
    if mutation in {"edit", "remove", "wrong-phase"}:
        fresh, current = data_and_entry(source, target)
        current.update(reviews=[record], semantic="pass")
        with pytest.raises(ValueError):
            checked_assessment(current, current["target"]["source_run"], fresh)


@pytest.mark.parametrize(
    "client_files",
    [
        (
            "native-ducc.jsonl",
            "native-ducc.stderr",
            "native-invocation.json",
            "native-session.json",
            "conversation.json",
        ),
        (
            "codex-native.jsonl",
            "codex-native.stderr",
            "codex-native-argv.json",
            "native-session.json",
            "conversation.json",
            "fixture-native.jsonl",
            "fixture-native.stderr",
            "fixture-native-argv.json",
        ),
    ],
)
def test_known_client_file_sets_remain_valid_and_bound_without_changing_source(tmp_path, client_files):
    source, target = source_run(tmp_path)
    for phase in (1, 2):
        directory = source / f"cli/approved-enrollment/001/phase-{phase}"
        for name in client_files:
            (directory / name).write_bytes(payload(name) + (b" " * phase))
    record, report = review(tmp_path, source, target)
    data, entry = data_and_entry(source, target)
    for phase in entry["phase_evidence"]:
        assert all(phase["hashes"][name] is not None for name in client_files)
    assert overlay.attach_review(data, source, record, report)
    assert checked_assessment(entry, entry["target"]["source_run"], data) == entry
    assert overlay.attach_review(data, source, record, report) is False


def test_native_file_from_another_original_runner_is_not_a_review_of_this_run(tmp_path):
    source, target = source_run(tmp_path)
    path = source / "cli/approved-enrollment/001/phase-2/native-session.json"
    path.write_bytes(payload(path.name))
    record, report = review(tmp_path, source, target)
    # A different runner/session input is still different evidence even when the
    # aggregate matrix and human-visible final answer have not changed.
    path.write_text('{"fixture":"another-runner","session_id":"different-session"}')
    data, entry = data_and_entry(source, target)
    entry.update(reviews=[record], semantic="pass")
    with pytest.raises(ValueError):
        checked_assessment(entry, entry["target"]["source_run"], data)
    with pytest.raises(ValueError):
        overlay.attach_review(data, source, record, report)


def test_renderer_and_current_runner_sources_are_covered_by_one_native_file_policy():
    assert transcript.NATIVE_RENDER_FILES is NATIVE_RENDER_FILES
    assert set(NATIVE_RENDER_FILES + NATIVE_CONTEXT_FILES) <= set(FILES)
    assert len(FILES) == len(set(FILES))
    training = ast.parse((ROOT / "scripts/dev/skill_training13_runner.py").read_text())
    # JsonProcess writes protocol, stderr, and invocation files from its explicit
    # name argument. A newly supported process prefix must extend the policy.
    processes = [
        node.args[2].value
        for node in ast.walk(training)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "JsonProcess"
        and len(node.args) >= 3
        and isinstance(node.args[2], ast.Constant)
    ]
    assert processes
    assert len(processes) == sum(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "JsonProcess"
        for node in ast.walk(training)
    )
    for process in processes:
        assert {process + suffix for suffix in (".jsonl", ".stderr", "-argv.json")} <= set(FILES)
    for filename in ("skill_ducc_runner.py", "skill_training13_runner.py", "skill_agent_eval.py"):
        tree = ast.parse((ROOT / "scripts/dev" / filename).read_text())
        # These are actual phase-root/predecessor file accesses in current hosts,
        # including conversation/session inputs, not a second handwritten alias list.
        paths = {
            node.right.value
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id in {"root", "first", "previous"}
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
            and "/" not in node.right.value
            and node.right.value.endswith((".json", ".jsonl", ".txt", ".stderr", ".stdout"))
        }
        assert paths <= set(FILES), (filename, paths - set(FILES))


@pytest.mark.parametrize("name", OMITTED)
def test_empty_late_file_is_distinct_from_bound_absence(tmp_path, name):
    source, target = source_run(tmp_path)
    record, report = review(tmp_path, source, target)
    data, _ = data_and_entry(source, target)
    (source / "cli/approved-enrollment/001/phase-2" / name).touch()
    with pytest.raises(ValueError, match="binding changed"):
        overlay.attach_review(data, source, record, report)


def test_unexecuted_phase_cannot_acquire_unbound_native_inputs(tmp_path):
    source, target = source_run(tmp_path, blocked=True)
    directory = source / "cli/approved-enrollment/001/phase-2"
    directory.mkdir()
    (directory / "codex-native.jsonl").write_bytes(WIRE)
    with pytest.raises(ValueError, match="Unexecuted"):
        data_and_entry(source, target)
