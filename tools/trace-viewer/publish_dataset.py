#!/usr/bin/env python3
"""Prepare a private immutable terminal-run dataset locally; never upload it."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from assessment_overlay import add_assessments, attach_review
from merge_snapshots import LEGACY_IDS, add_matrix, add_semantic_reviews, atomic_write, load, validate
from synthetic_transcript import MAX_TRACE_BYTES, native_artifact_manifest, read_local, tool_steps

SHA256 = re.compile(r"[a-f0-9]{64}")
MAX_INDEX_BYTES = 1024 * 1024
MAX_DATASETS = 4096
MAX_ASSESSMENT_BYTES = 1024 * 1024
MAX_ASSESSMENTS_PER_DATASET = 64


def private_directory(path):
    if path.is_symlink():
        raise ValueError("Dataset directories must not be symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError("Dataset directories must be private")


def read_private(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("Dataset files must be private regular bounded files")
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Dataset file exceeds its limit")
    return content


def registry(directory):
    path = directory / "index.json"
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "datasets": []}
    value = json.loads(read_private(path, MAX_INDEX_BYTES))
    ids = value.get("datasets")
    if (
        value.get("schema_version") != 1
        or not isinstance(ids, list)
        or len(ids) > MAX_DATASETS
        or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Invalid immutable dataset registry")
    return value


def verify_dataset(directory, dataset_id):
    if not SHA256.fullmatch(dataset_id):
        raise ValueError("Invalid dataset identity")
    path = directory / dataset_id
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("Unsafe immutable dataset directory")
    metadata = json.loads(read_private(path / "metadata.json", 64 * 1024))
    if metadata.get("schema_version") != 1 or metadata.get("dataset_id") != dataset_id:
        raise ValueError("Dataset metadata identity mismatch")
    for raw, name in ((True, "raw"), (False, "latest")):
        content = read_private(path / f"{name}.json", 32 * 1024 * 1024 if raw else 1024 * 1024)
        digest = hashlib.sha256(content).hexdigest()
        if (
            digest != metadata.get(f"{name}_sha256")
            or len(content) != metadata.get(f"{name}_size_bytes")
            or (raw and digest != dataset_id)
        ):
            raise ValueError("Immutable dataset bytes changed")
        validate(json.loads(content), raw)
    return metadata


def assessment_registry(directory):
    path = directory / "index.json"
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "datasets": {}}
    value = json.loads(read_private(path, MAX_INDEX_BYTES))
    datasets = value.get("datasets")
    if (
        value.get("schema_version") != 1
        or not isinstance(datasets, dict)
        or len(datasets) > MAX_DATASETS
        or any(
            not SHA256.fullmatch(key)
            or not isinstance(ids, list)
            or len(ids) > MAX_ASSESSMENTS_PER_DATASET
            or len(ids) != len(set(ids))
            or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in ids)
            for key, ids in datasets.items()
        )
    ):
        raise ValueError("Invalid assessment registry")
    return value


def previous_assessments(output, dataset_id):
    directory = output / "assessments"
    if not directory.exists() and not directory.is_symlink():
        return {}
    private_directory(directory)
    records = assessment_registry(directory)["datasets"].get(dataset_id, [])
    merged = {}
    for identity in records:
        content = read_private(directory / (identity + ".json"), MAX_ASSESSMENT_BYTES)
        value = json.loads(content)
        if (
            hashlib.sha256(content).hexdigest() != identity
            or value.get("schema_version") != 1
            or value.get("dataset_id") != dataset_id
            or not isinstance(value.get("assessments"), dict)
        ):
            raise ValueError("Assessment overlay integrity failed")
        for key, entry in value["assessments"].items():
            old = merged.get(key)
            if old and (
                old["binding"] != entry.get("binding")
                or not {row["review_id"] for row in old["reviews"]}.issubset(
                    {row["review_id"] for row in entry.get("reviews", [])}
                )
            ):
                raise ValueError("Assessment history was removed or changed")
            merged[key] = entry
    return merged


def publish_assessments(output, dataset_id, assessments):
    if not assessments:
        return None
    directory = output / "assessments"
    private_directory(directory)
    record = {"schema_version": 1, "dataset_id": dataset_id, "assessments": assessments}
    content = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(content) > MAX_ASSESSMENT_BYTES:
        raise ValueError("Assessment overlay exceeds its limit")
    identity = hashlib.sha256(content).hexdigest()
    target = directory / (identity + ".json")
    if target.exists() or target.is_symlink():
        if read_private(target, MAX_ASSESSMENT_BYTES) != content:
            raise ValueError("Assessment overlay changed")
    else:
        atomic_write(target, content)
    index = assessment_registry(directory)
    ids = index["datasets"].setdefault(dataset_id, [])
    if identity not in ids:
        if len(ids) >= MAX_ASSESSMENTS_PER_DATASET:
            raise ValueError("Assessment registry is full; no prior review was removed")
        ids.append(identity)
        encoded = json.dumps(index, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_INDEX_BYTES:
            raise ValueError("Assessment registry exceeds its limit")
        atomic_write(directory / "index.json", encoded)
    return {"id": identity, "records": len(assessments)}


def audit_source_io(source, raw):
    """Reject phase omissions instead of inheriting the viewer's best-effort projection."""
    matrix = load(source / "matrix.json")
    counts = {"prompts": 0, "finals": 0, "fixture_tools": 0}
    consumed = set()
    for row in sorted(matrix["results"], key=lambda value: value.get("phase", 0)):
        evidence = row.get("evidence")
        if not evidence:
            continue  # A never-executed phase must not gain invented conversation.
        directory = source / evidence
        if directory.resolve() != directory or not directory.is_relative_to(source):
            raise ValueError("Unsafe source phase evidence path")
        prompt_path, response_path = directory / "prompt.txt", directory / "response.json"
        if not prompt_path.is_file():
            if response_path.exists() or (directory / "trace.jsonl").exists() or row.get("status") == "completed":
                raise ValueError("An executed source phase has no verifiable source prompt")
            continue
        prompt = read_local(prompt_path, source, MAX_TRACE_BYTES)
        final = None
        if response_path.is_file():
            final = json.loads(read_local(response_path, source, MAX_TRACE_BYTES))["text"]
        suite, scenario = row["suite"], row["scenario"]
        key = LEGACY_IDS.get(scenario, scenario) if suite == "cli" else f"{suite}-{scenario}"
        pair = {"client": row["client"], "model": row["model"]}
        number = next(
            index
            for index, runner in enumerate(matrix["runners"])
            if all(runner.get(key) == value for key, value in pair.items())
        )
        sessions = raw["cases"][key][number]["sessions"]
        calls = None
        if (directory / "trace.jsonl").is_file():
            calls = tool_steps(read_local(directory / "trace.jsonl", source, MAX_TRACE_BYTES), row["phase"], raw=True)
        for index, session in enumerate(sessions):
            identity = (key, number, index)
            if identity in consumed or {"role": "user", "text": prompt} not in session:
                continue
            if final is not None and {"role": "assistant", "text": final} not in session:
                continue
            if calls and calls["steps"] and calls not in session:
                continue
            consumed.add(identity)
            counts["prompts"] += 1
            counts["finals"] += int(final is not None)
            counts["fixture_tools"] += calls["total"] if calls else 0
            break
        else:
            raise ValueError(
                "Raw projection omitted or changed actual phase prompt, final or fixture tool I/O: " + evidence
            )
    return counts


def export_dataset(
    source,
    output,
    *,
    title=None,
    minimal_summary=False,
    assessment_reviews=(),
    release_assessments=False,
    cli_assessments=False,
):
    """Return an immutable registered dataset; root legacy files are never read or written."""
    source = source.resolve(strict=True)
    matrix = source / "matrix.json"
    if not matrix.is_file():
        raise ValueError("Only terminal runs with matrix.json can be exported")
    plan = load(source / "run.json")
    version = plan["artifacts"]["current"]["package_version"]
    title = title or source.name
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("Dataset title must be 1–256 characters")
    # Do not resolve away a caller-supplied symlink before checking the output.
    output = output.absolute()
    private_directory(output)
    output = output.resolve(strict=True)
    directory = output / "datasets"
    private_directory(directory)
    descriptor = os.open(directory / ".publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode) or os.fstat(lock.fileno()).st_mode & 0o077:
            raise ValueError("Unsafe dataset publication lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = registry(directory)
        payloads = {}
        for raw, name in ((True, "raw"), (False, "latest")):
            data = {"schema": 1, "title": title, "version": version, "cases": {}}
            if raw:
                data["visibility"] = "raw"
            add_matrix(
                data,
                source,
                raw,
                native_artifacts=output / "evidence" if raw else None,
                compact_summary=minimal_summary,
                minimal_new_summary=minimal_summary,
            )
            add_semantic_reviews(
                data, source, raw, compact_summary=minimal_summary, minimal_new_summary=minimal_summary
            )
            payloads[name] = validate(data, raw)
        audit = audit_source_io(source, json.loads(payloads["raw"]))
        dataset_id = hashlib.sha256(payloads["raw"]).hexdigest()
        record = json.loads(payloads["raw"])
        # Assessments are outside the immutable dataset bytes: dataset IDs are part
        # of comment identity, even when only a top-level field would have changed.
        assessments = json.loads(payloads["raw"])
        assessments["assessments"] = previous_assessments(output, dataset_id)
        add_assessments(assessments, source, release_assessments=release_assessments, cli_assessments=cli_assessments)
        for review_path, report_path in assessment_reviews:
            review = json.loads(read_private(review_path, 64 * 1024))
            attach_review(assessments, source, review, report_path)
        metadata = {
            "schema_version": 1,
            "dataset_id": dataset_id,
            "raw_sha256": dataset_id,
            "latest_sha256": hashlib.sha256(payloads["latest"]).hexdigest(),
            "raw_size_bytes": len(payloads["raw"]),
            "latest_size_bytes": len(payloads["latest"]),
            "title": title,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": version,
            "source_run": source.name,
            "source_package_version": version,
            "source_matrix_sha256": hashlib.sha256(matrix.read_bytes()).hexdigest(),
            "run_count": sum(len(items) for items in record["cases"].values()),
            "case_count": len(record["cases"]),
        }
        target = directory / dataset_id
        if target.exists() or target.is_symlink():
            previous = verify_dataset(directory, dataset_id)
            if {k: v for k, v in previous.items() if k != "created_at"} != {
                k: v for k, v in metadata.items() if k != "created_at"
            }:
                raise ValueError("Refusing to overwrite an existing immutable dataset")
            metadata = previous
        else:
            temporary = Path(tempfile.mkdtemp(prefix=".dataset-", dir=directory))
            try:
                for name, content in payloads.items():
                    atomic_write(temporary / f"{name}.json", content)
                atomic_write(temporary / "metadata.json", json.dumps(metadata, ensure_ascii=False).encode() + b"\n")
                os.rename(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        verify_dataset(directory, dataset_id)
        evidence = native_artifact_manifest(output / "evidence")
        if dataset_id not in index["datasets"]:
            if len(index["datasets"]) >= MAX_DATASETS:
                raise ValueError("Dataset registry is full; no registered dataset was removed")
            index["datasets"].append(dataset_id)
            content = json.dumps(index, separators=(",", ":")).encode() + b"\n"
            if len(content) > MAX_INDEX_BYTES:
                raise ValueError("Dataset registry exceeds its limit")
            # Publish membership only after the immutable pair and all diagnostics exist.
            atomic_write(directory / "index.json", content)
        assessment = publish_assessments(output, dataset_id, assessments.get("assessments", {}))
        return {
            "dataset": metadata,
            "source_io_audit": audit,
            "evidence": evidence,
            "assessment_overlay": assessment,
            "publication_authorized": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Private terminal matrix directory")
    parser.add_argument("output", type=Path, help="Private local staging root; never the frontend static directory")
    parser.add_argument("--title", help="Human-readable immutable batch label")
    parser.add_argument(
        "--minimal-summary", action="store_true", help="Project summary verdicts; retain complete raw I/O"
    )
    parser.add_argument(
        "--assessment-review",
        action="append",
        nargs=2,
        type=Path,
        default=[],
        metavar=("SIDECAR", "REPORT"),
        help="Attach a bound model review to its existing run",
    )
    parser.add_argument(
        "--release-assessments", action="store_true", help="Opt in known cli-release fixtures to semantic assessment"
    )
    parser.add_argument(
        "--cli-assessments", action="store_true", help="Opt in exact CLI35/36 whole-run semantic assessments"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            export_dataset(
                args.source,
                args.output,
                title=args.title,
                minimal_summary=args.minimal_summary,
                assessment_reviews=args.assessment_review,
                release_assessments=args.release_assessments,
                cli_assessments=args.cli_assessments,
            )
        )
    )


if __name__ == "__main__":
    main()
