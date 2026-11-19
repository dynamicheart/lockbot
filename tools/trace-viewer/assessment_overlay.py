"""Attach evidence-bound model assessments without editing trials or comment identities.

An assessment describes automated observations and model judgment separately. It is
not a human approval and its hashes establish correspondence, not review truth.
"""

import argparse
import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from synthetic_transcript import MAX_TRACE_BYTES, fixture_scenario, read_local

SCHEMA = 1
HASH = re.compile(r"[a-f0-9]{64}")
RESOURCE_TOPICS = [
    "Check answer correctness, coverage caveats and unknown utilization against the actual tool output.",
    "Observable checks do not establish semantic correctness or live service acceptance.",
]
CONSULTATION_DOCUMENTS = {
    "what-is-this": "SKILL.md",
    "getting-started": "SKILL.md",
    "binding-help": "references/connections.md",
    "booking-vs-lock": "references/resource-operations.md",
    "default-duration": "references/resource-operations.md",
    "zero-utilization": "references/workbench-and-metrics.md",
    "share-login-link": "references/workbench-and-metrics.md",
}


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def _json(path, root):
    return json.loads(read_local(path, root, MAX_TRACE_BYTES))


def _known_topics(row, case):
    if any(key in row or key in case for key in ("human_review_gate", "human_gate")):
        return None  # Explicit/unknown policies are never waived by a checklist adapter.
    if row.get("suite") == "resource-cli" and case.get("fixture_family") == "resource-cli":
        version = case.get("resource_cli_version")
        expected = RESOURCE_TOPICS if type(version) is int and version in {1, 2} else None
    elif row.get("suite") == "consultation" and case.get("consultation") is True:
        document = CONSULTATION_DOCUMENTS.get(row.get("scenario"))
        expected = (
            [
                "Observable pass checks evidence and non-mutation only; it does not grade explanation correctness.",
                f"Compare the answer with observed sources: {document}, "
                "main Skill, manifest or live rules as applicable.",
                "Reading a substantive page is evidence of consultation, "
                "not proof that the answer used the relevant rule.",
                "Check that advice does not claim completed installation, "
                "binding, resource allocation or link issuance.",
            ]
            if document and type(case.get("fixture_version")) is int and case["fixture_version"] in {1, 2, 3}
            else None
        )
    else:
        expected = None
    return expected if expected is not None and row.get("manual_review_required") == expected else None


def phase_source(source, target):
    """Validate identity and bind all inputs used to assess this actual terminal phase."""
    source = source.resolve(strict=True)
    if not isinstance(target, dict):
        raise ValueError("Invalid assessment target")
    plan, matrix = _json(source / "run.json", source), _json(source / "matrix.json", source)
    if matrix.get("simulation_only") is not True or matrix.get("artifacts") != plan.get("artifacts"):
        raise ValueError("Assessment requires an actual frozen synthetic matrix")
    fields = ("suite", "scenario", "client", "model", "phase")
    rows = [row for row in matrix["results"] if all(row.get(key) == target.get(key) for key in fields)]
    if len(rows) != 1 or type(target.get("phase")) is not int or target["phase"] != 1:
        raise ValueError("Assessment must identify one supported actual phase")
    row = rows[0]
    pair = {"client": row["client"], "model": row["model"]}
    number = plan["runners"].index(pair)
    expected = Path(row["suite"], row["scenario"], f"{number + 1:03}", "phase-1").as_posix()
    if row.get("evidence") != expected:
        raise ValueError("Assessment evidence path does not match its target")
    directory = source / row["evidence"]
    case = _json(directory / "case.json", source)
    if fixture_scenario(case, row["suite"]) != row["scenario"] or case.get("phase", 1) != 1:
        raise ValueError("Assessment fixture identity changed")
    artifact = plan["artifacts"]["current"]
    frozen = source / "artifacts/current"
    manifest = _json(frozen / "manifest.json", source)
    files = ["case.json", "subject.md", "prompt.txt", "response.json", "result.json"]
    binding = {"matrix_row": digest(row)}
    for name in files + ["trace.jsonl", "cli-http.jsonl"]:
        path = directory / name
        # Absence is bound as well: adding a later trace cannot validate an older review.
        if name in {"trace.jsonl", "cli-http.jsonl"} and not path.exists() and not path.is_symlink():
            binding[name] = None
        else:
            binding[name] = digest(read_local(path, source, MAX_TRACE_BYTES).encode())
    for name in ("manifest.json", "SKILL.md", "lockbot-agent.zip"):
        path = frozen / name
        if name == "lockbot-agent.zip":
            resolved = path.resolve(strict=True)
            if resolved != path or not resolved.is_relative_to(source) or not resolved.is_file():
                raise ValueError("Assessment archive must remain in its frozen directory")
            content = resolved.read_bytes()
            if len(content) > MAX_TRACE_BYTES:
                raise ValueError("Assessment archive exceeds its limit")
        else:
            content = read_local(path, source, MAX_TRACE_BYTES).encode()
        binding["artifact/" + name] = digest(content)
    result = _json(directory / "result.json", source)
    response = _json(directory / "response.json", source)
    if (
        not isinstance(response.get("text"), str)
        or not response["text"].strip()
        or any(result.get(key) != row.get(key) for key in (*fields, "status", "observable_pass", "skill_sha256"))
        or row.get("artifact_revision") != artifact.get("revision")
        or row.get("skill_sha256") != binding["artifact/SKILL.md"]
        or case.get("skill_sha256") != binding["artifact/SKILL.md"]
        or case.get("subject_sha256", case["skill_sha256"]) != binding["subject.md"]
        or manifest.get("version") != artifact.get("revision")
        or manifest.get("skill_sha256") != binding["artifact/SKILL.md"]
        or manifest.get("package_version") != artifact.get("package_version")
        or manifest.get("installation", {}).get("sha256") != binding["artifact/lockbot-agent.zip"]
        or artifact.get("sha256") != binding["artifact/lockbot-agent.zip"]
    ):
        raise ValueError("Assessment phase or artifact changed")
    case_id = f"{row['suite']}-{row['scenario']}"
    source_run = f"matrix:{digest(matrix)}:{case_id}:{number}"
    return (
        row,
        case,
        binding,
        {
            **pair,
            "suite": row["suite"],
            "scenario": row["scenario"],
            "phase": 1,
            "case_id": case_id,
            "source_run": source_run,
        },
    )


def add_assessments(data, source):
    """Interpret only known generic checklists, retaining every original trial byte."""
    source = source.resolve(strict=True)
    matrix = _json(source / "matrix.json", source)
    additions = {}
    for row in matrix.get("results", []):
        if row.get("suite") not in {"resource-cli", "consultation"} or row.get("phase") != 1:
            continue
        case = _json(source / row["evidence"] / "case.json", source)
        topics = _known_topics(row, case)
        if topics is None or row.get("status") != "completed":
            continue
        response_path = source / row["evidence"] / "response.json"
        if not response_path.exists() or not _json(response_path, source).get("text", "").strip():
            continue  # Missing answers retain the original failure/unknown result.
        _, _, binding, target = phase_source(source, row)
        if not any(
            trial.get("source_run") == target["source_run"] for trial in data["cases"].get(target["case_id"], [])
        ):
            raise ValueError("Assessment source trial is absent")
        additions[target["source_run"]] = {
            "schema_version": SCHEMA,
            "target": target,
            "binding": binding,
            "adapter": {"family": row["suite"], "version": case.get("resource_cli_version", case["fixture_version"])},
            "execution": row["status"],
            "behavior": "pass" if row.get("observable_pass") is True else "fail",
            "semantic": "not_reviewed",
            "human_gate": "not_required",
            "topics": topics,
            "reviews": [],
        }
    current = data.get("assessments", {})
    for key, value in additions.items():
        if key in current:
            if {k: v for k, v in current[key].items() if k not in {"reviews", "semantic"}} != {
                k: v for k, v in value.items() if k not in {"reviews", "semantic"}
            }:
                raise ValueError("Assessment source identity changed")
            value.update(reviews=copy.deepcopy(current[key]["reviews"]), semantic=current[key]["semantic"])
    if additions:
        data["assessments"] = {**current, **additions}


def bind_review(source, target, report, *, reviewer, status, findings=(), supersedes=None):
    """Build an explicit model-review sidecar; no model call or human approval occurs."""
    row, case, binding, exact = phase_source(source, target)
    if _known_topics(row, case) is None or row.get("status") != "completed":
        raise ValueError("This phase has no supported checklist adapter")
    path = report.absolute()
    if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError("Review report must be an explicit regular bounded file")
    record = {
        "schema_version": SCHEMA,
        "target": exact,
        "binding": binding,
        "reviewer": reviewer,
        "status": status,
        "findings": list(findings),
        "report_sha256": digest(path.read_bytes()),
        "bound_at": datetime.now(timezone.utc).isoformat(),
        "supersedes": supersedes,
    }
    validate_review(record)
    record["review_id"] = digest(record)
    return record


def validate_review(record):
    if not isinstance(record, dict):
        raise ValueError("Invalid model review record")
    reviewer = record.get("reviewer", {})
    if (
        type(record.get("schema_version")) is not int
        or record["schema_version"] != SCHEMA
        or not isinstance(reviewer, dict)
        or reviewer.get("kind") != "model"
        or set(reviewer) - {"kind", "name", "requested_model"}
        or any(
            not isinstance(reviewer.get(key), str) or not 1 <= len(reviewer[key]) <= 160
            for key in ("name", "requested_model")
        )
        or record.get("status") not in {"pass", "findings", "uncertain"}
        or not isinstance(record.get("findings"), list)
        or len(record["findings"]) > 4
        or not HASH.fullmatch(record.get("report_sha256", ""))
        or (record.get("supersedes") is not None and not HASH.fullmatch(record["supersedes"]))
    ):
        raise ValueError("Invalid model review record")
    for finding in record["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"severity", "blocking", "text"}
            or finding["severity"] not in {"P0", "P1", "P2", "P3"}
            or type(finding["blocking"]) is not bool
            or not isinstance(finding["text"], str)
            or not 1 <= len(finding["text"]) <= 600
        ):
            raise ValueError("Invalid review finding")
    if (record["status"] == "pass" and record["findings"]) or (
        record["status"] == "findings" and not record["findings"]
    ):
        raise ValueError("Review status does not match findings")


def attach_review(data, source, record, report):
    """Validate before changing the overlay; archived trials remain untouched."""
    validate_review(record)
    declared = {k: v for k, v in record.items() if k != "review_id"}
    if record.get("review_id") != digest(declared):
        raise ValueError("Review record digest changed")
    report = report.absolute()
    if (
        report.is_symlink()
        or report.resolve(strict=True) != report
        or not report.is_file()
        or report.stat().st_size > 128 * 1024
        or digest(report.read_bytes()) != record["report_sha256"]
    ):
        raise ValueError("Review report changed")
    _, _, binding, target = phase_source(source, record["target"])
    if record["target"] != target or record["binding"] != binding:
        raise ValueError("Review evidence binding changed")
    existing = data.get("assessments", {}).get(target["source_run"])
    if existing is None or existing["binding"] != binding:
        raise ValueError("Review has no matching derived assessment")
    reviews = copy.deepcopy(existing["reviews"])
    if any(item["review_id"] == record["review_id"] for item in reviews):
        return False
    if len(reviews) >= 16:
        raise ValueError("Review history is full; no prior review was removed")
    if record.get("supersedes") and record["supersedes"] not in {item["review_id"] for item in reviews}:
        raise ValueError("Review correction must reference an existing review")
    reviews.append(copy.deepcopy(record))
    replaced = {item["supersedes"] for item in reviews if item.get("supersedes")}
    active = [item for item in reviews if item["review_id"] not in replaced]
    statuses = {item["status"] for item in active}
    semantic = next(iter(statuses)) if len(statuses) == 1 else "uncertain"
    existing.update(reviews=reviews, semantic=semantic)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True, help="JSON suite/scenario/client/model/phase identity")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--requested-model", required=True)
    parser.add_argument("--status", choices=("pass", "findings", "uncertain"), required=True)
    parser.add_argument("--findings", type=Path, help="Explicit JSON findings; never inferred from prose")
    parser.add_argument("--supersedes", help="Existing review ID corrected by this append-only record")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = bind_review(
        args.source,
        json.loads(args.target.read_text()),
        args.report,
        reviewer={"kind": "model", "name": args.reviewer, "requested_model": args.requested_model},
        status=args.status,
        findings=json.loads(args.findings.read_text()) if args.findings else [],
        supersedes=args.supersedes,
    )
    os.umask(0o077)
    with args.output.open("x") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
