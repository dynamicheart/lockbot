"""Bind the explicitly supported CLI35 whole-run policy to immutable local evidence."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from lockbot.trace_cli_assessment import (  # noqa: E402
    FILES,
    PHASE_COUNTS,
    TOPICS,
    case_id,
    checked_header,
    known_policy,
)


def run_source(source, target):
    # Delayed import keeps the existing phase adapter independent of CLI opt-in.
    from assessment_overlay import _bind_artifact, _json, digest
    from lockbot.trace_evidence_files import NATIVE_RENDER_FILES
    from synthetic_transcript import MAX_TRACE_BYTES, read_local, read_native_bytes

    source = source.resolve(strict=True)
    if not isinstance(target, dict) or target.get("suite") != "cli" or target.get("phase_scope") != "all":
        raise ValueError("CLI assessment must identify an entire original run")
    scenario = target.get("scenario")
    if scenario not in PHASE_COUNTS:
        raise ValueError("Unsupported CLI assessment scenario")
    plan, matrix = _json(source / "run.json", source), _json(source / "matrix.json", source)
    if (
        plan.get("simulation_only") is not True
        or plan.get("cli_fixture_version") != 35
        or type(plan.get("cli_fixture_version")) is not int
        or matrix.get("cli_fixture_version") != 35
        or type(matrix.get("cli_fixture_version")) is not int
        or matrix.get("simulation_only") is not True
        or matrix.get("artifacts") != plan.get("artifacts")
        or matrix.get("runners") != plan.get("runners")
        or "cli:" + scenario not in plan.get("cases", [])
        or not all(known_policy(value, scenario) for value in (plan, matrix))
    ):
        raise ValueError("Unsupported CLI35 frozen plan or policy")
    pair = {key: target.get(key) for key in ("client", "model")}
    if plan["runners"].count(pair) != 1:
        raise ValueError("CLI runner identity is ambiguous")
    number = plan["runners"].index(pair)
    rows = [
        row
        for row in matrix["results"]
        if row.get("suite") == "cli"
        and row.get("scenario") == scenario
        and all(row.get(key) == value for key, value in pair.items())
    ]
    expected_phases = list(range(1, PHASE_COUNTS[scenario] + 1))
    if len({row.get("phase") for row in rows}) != len(rows) or any(
        type(row.get("phase")) is not int or row["phase"] not in expected_phases for row in rows
    ):
        raise ValueError("CLI matrix phase identity changed")
    binding = {
        "plan": digest(read_local(source / "run.json", source, MAX_TRACE_BYTES).encode()),
        "matrix": digest(read_local(source / "matrix.json", source, MAX_TRACE_BYTES).encode()),
    }
    _bind_artifact(source, source / "artifacts/current", binding, "artifact/")
    manifest = _json(source / "artifacts/current/manifest.json", source)
    artifact = plan["artifacts"]["current"]
    if (
        manifest.get("version") != artifact.get("revision")
        or manifest.get("package_version") != artifact.get("package_version")
        or manifest.get("skill_sha256") != binding["artifact/SKILL.md"]
        or manifest.get("installation", {}).get("sha256") != binding["artifact/lockbot-agent.zip"]
        or artifact.get("sha256") != binding["artifact/lockbot-agent.zip"]
    ):
        raise ValueError("CLI assessment artifact changed")
    evidence = []
    for phase in expected_phases:
        row = next((row for row in rows if row["phase"] == phase), None)
        relative = Path("cli", scenario, f"{number + 1:03}", f"phase-{phase}")
        directory = source / relative
        observed = row is not None and row.get("evidence") is not None
        if row is not None and (
            row.get("fixture_family") not in (None, "cli")
            or not known_policy(row, scenario)
            or row.get("cli_fixture_version") != 35
            or type(row.get("cli_fixture_version")) is not int
        ):
            raise ValueError("Unsupported CLI phase policy")
        if observed and row["evidence"] != relative.as_posix():
            raise ValueError("CLI phase evidence path changed")
        hashes = {}
        for name in FILES:
            path = directory / name
            if not path.exists() and not path.is_symlink():
                hashes[name] = None
            elif name in NATIVE_RENDER_FILES:
                hashes[name] = digest(read_native_bytes(path, source))
            else:
                hashes[name] = digest(read_local(path, source, MAX_TRACE_BYTES).encode())
        status = row.get("status", "missing") if row else "missing"
        if observed:
            case, result = _json(directory / "case.json", source), _json(directory / "result.json", source)
            if (
                row.get("fixture_version") != 35
                or type(row.get("fixture_version")) is not int
                or case.get("fixture_version") != 35
                or type(case.get("fixture_version")) is not int
                or case.get("scenario") != scenario
                or type(case.get("phase")) is not int
                or case.get("phase") != phase
                or case.get("fixture_family") not in (None, "cli")
                or not known_policy(case, scenario)
                or result != row
                or row.get("artifact_revision") != artifact["revision"]
                or row.get("skill_sha256") != binding["artifact/SKILL.md"]
                or case.get("skill_sha256") != binding["artifact/SKILL.md"]
                or case.get("subject_sha256") != hashes["subject.md"]
            ):
                raise ValueError("CLI phase identity or policy changed")
        elif any(item is not None for item in hashes.values()):
            raise ValueError("Unexecuted CLI phase has unbound files")
        completed = observed and status == "completed" and hashes["response.json"] is not None
        if completed and not _json(directory / "response.json", source).get("text", "").strip():
            raise ValueError("Completed CLI phase has no answer")
        evidence.append(
            {
                "phase": phase,
                "status": status,
                "observed": observed,
                "completed": completed,
                "observable_pass": row.get("observable_pass") if row else None,
                "matrix_row": digest(row) if row else None,
                "hashes": hashes,
            }
        )
    if not any(phase["observed"] for phase in evidence):
        raise ValueError("CLI run has no observed phase")
    binding["phase_evidence"] = digest(evidence)
    exact = {
        **pair,
        "suite": "cli",
        "scenario": scenario,
        "phase_scope": "all",
        "case_id": case_id(scenario),
        "source_run": f"matrix:{digest(matrix)}:{case_id(scenario)}:{number}",
    }
    complete = all(phase["completed"] for phase in evidence)
    behavior = (
        "fail"
        if any(phase["observable_pass"] is False for phase in evidence)
        else ("pass" if complete and all(phase["observable_pass"] is True for phase in evidence) else "unknown")
    )
    entry = {
        "schema_version": 2,
        "target": exact,
        "binding": binding,
        "adapter": {"family": "cli", "version": 35},
        "phase_evidence": evidence,
        "execution": "completed" if complete else "incomplete",
        "behavior": behavior,
        "semantic": "not_reviewed",
        "human_gate": "not_required",
        "topics": TOPICS,
        "reviews": [],
    }
    checked_header(entry, exact["source_run"], {"cases": {exact["case_id"]: [{"source_run": exact["source_run"]}]}})
    return entry


def additions(source):
    from assessment_overlay import _json

    matrix = _json(source / "matrix.json", source)
    entries = {}
    for row in matrix.get("results", []):
        if row.get("suite") != "cli" or row.get("scenario") not in PHASE_COUNTS or row.get("phase") != 1:
            continue
        # Unknown versions and explicit gates remain entirely under their original policy.
        if row.get("cli_fixture_version") != 35 or not row.get("evidence"):
            continue
        entry = run_source(source, {**row, "phase_scope": "all"})
        entries[entry["target"]["source_run"]] = entry
    return entries
