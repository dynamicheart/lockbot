#!/usr/bin/env python3
"""Run LockBot Skill cases concurrently with caller-supplied model adapters."""

import argparse
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
from pathlib import Path

import skill_agent_eval as onboarding
import skill_cli_eval as cli
import skill_continuity_eval as continuity
import skill_operations_eval as operations
from skill_eval_artifact import load

CASES = {
    "cli": cli.SCENARIOS,
    "onboarding": tuple(onboarding.SCENARIOS),
    "continuity": tuple(continuity.PROMPTS),
    "operations": tuple(operations.PROMPTS),
}
SECOND_PHASE = {
    "cli": {"approved-enrollment", "panel"},
    "continuity": {"recover", "panel"},
}


def freeze(source, destination, expected_origin):
    artifact = load(source)
    stamp = json.loads(artifact["contents"]["installation.json"])
    if stamp.get("deployment_origin") != expected_origin:
        raise ValueError(f"{destination.name} must be rendered for {expected_origin}")
    destination.mkdir(mode=0o700, parents=True)
    manifest = artifact["manifest"]
    onboarding.save_json(destination / "manifest.json", manifest)
    (destination / "SKILL.md").write_bytes(artifact["contents"]["SKILL.md"])
    (destination / "lockbot-resource-agent.zip").write_bytes(artifact["archive"])
    return {
        "revision": manifest["version"],
        "sha256": manifest["installation"]["sha256"],
        "package_version": manifest.get("package_version"),
        "instance_id": manifest.get("instance_id", onboarding.INSTANCE),
    }


def prepare(root, suite, scenario, artifacts):
    current = artifacts / "current"
    source = current / "SKILL.md"
    if suite == "cli":
        cli.prepare(root, artifacts / "panel" if scenario == "panel" else current, scenario)
        return cli.score
    if suite == "onboarding":
        onboarding.prepare(root, None, scenario, artifacts=current)
        return onboarding.score
    if suite == "continuity":
        continuity.prepare(root, source, scenario, current)
        return continuity.score
    installed = artifacts / "installed" if scenario == "update" else None
    operations.prepare(root, source, scenario, current, installed)
    return operations.score


def evaluate_phase(root, metadata, command, grader, limits):
    result = onboarding.evaluate(root, command, metadata["model"], grader, **limits)
    case = json.loads((root / "case.json").read_text())
    result.update(metadata, fixture_version=case["fixture_version"], skill_sha256=case["skill_sha256"])
    onboarding.save_json(root / "result.json", result)
    return result


def fixture_error(path, metadata):
    message = path / "fixture-error.log"
    message.write_text(traceback.format_exc())
    message.chmod(0o600)
    return {**metadata, "status": "fixture_error"}


def run_case(output, suite, scenario, number, runner, limits, revision):
    client, model, command = runner
    case = output / suite / scenario / f"{number:03d}"
    case.mkdir(mode=0o700, parents=True)
    metadata = {"suite": suite, "scenario": scenario, "client": client, "model": model, "artifact_revision": revision}
    first = case / "phase-1"
    first_metadata = {**metadata, "phase": 1, "evidence": first.relative_to(output).as_posix()}
    try:
        grader = prepare(first, suite, scenario, output / "artifacts")
        result = evaluate_phase(first, first_metadata, command, grader, limits)
    except Exception:
        first.mkdir(mode=0o700, exist_ok=True)
        result = fixture_error(first, first_metadata)
    rows = [result]
    if scenario not in SECOND_PHASE.get(suite, ()):
        return rows
    second_metadata = {**metadata, "phase": 2}
    if not result.get("observable_pass"):
        return [*rows, {**second_metadata, "status": "blocked_by_phase_1"}]
    second = case / "phase-2"
    second_metadata["evidence"] = second.relative_to(output).as_posix()
    try:
        if scenario == "panel":
            continuity.human_confirm(first)
        (cli if suite == "cli" else continuity).resume(first, second)
        rows.append(evaluate_phase(second, second_metadata, command, grader, limits))
    except Exception:
        second.mkdir(mode=0o700, exist_ok=True)
        rows.append(fixture_error(second, second_metadata))
    return rows


def run_matrix(output, cases, runners, artifacts, workers, limits):
    plan = list(product(cases, enumerate(runners, start=1)))
    metadata = {
        "artifacts": artifacts,
        "limits": limits,
        "workers": workers,
        "simulation_only": True,
        "cases": [f"{suite}:{scenario}" for suite, scenario in cases],
        "runners": [{"client": client, "model": model} for client, model, _ in runners],
    }
    onboarding.save_json(output / "run.json", metadata)
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool, (output / "progress.jsonl").open("w") as progress:
        pending = {}
        for index, ((suite, scenario), (number, runner)) in enumerate(plan):
            artifact = "panel" if (suite, scenario) == ("cli", "panel") else "current"
            future = pool.submit(
                run_case, output, suite, scenario, number, runner, limits, artifacts[artifact]["revision"]
            )
            pending[future] = index
        for future in as_completed(pending):
            rows = future.result()
            results[pending[future]] = rows
            for row in rows:
                progress.write(json.dumps(row, ensure_ascii=False) + "\n")
            progress.flush()
    ordered = [row for index in range(len(plan)) for row in results[index]]
    onboarding.save_json(output / "matrix.json", {**metadata, "results": ordered})
    return all(row.get("observable_pass") for row in ordered)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List supported suite:scenario identifiers")
    run = commands.add_parser("run", help="Run selected scenarios for every configured client/model pair")
    run.add_argument("--runner", action="append", nargs=3, metavar=("CLIENT", "MODEL", "ARGV_JSON"), required=True)
    run.add_argument("--suite", action="append", choices=CASES, default=[])
    case_ids = [f"{suite}:{scenario}" for suite, scenarios in CASES.items() for scenario in scenarios]
    run.add_argument("--case", action="append", choices=case_ids, metavar="SUITE:SCENARIO", default=[])
    run.add_argument("--artifacts", type=Path, required=True)
    run.add_argument("--panel-artifacts", type=Path, help="Same Skill rendered for the HTTPS personal panel fixture")
    run.add_argument("--installed-artifacts", type=Path, help="Older Skill ZIP for operations:update")
    run.add_argument("--output", type=Path, required=True, help="New private evidence directory outside the repository")
    run.add_argument("--workers", type=positive_int, default=4)
    run.add_argument("--timeout", type=positive_int, default=180, help="Seconds per model session")
    run.add_argument("--max-turns", type=positive_int, default=24)
    run.add_argument("--output-tokens", type=positive_int, default=4096)
    args = parser.parse_args()
    if args.action == "list":
        print(*case_ids, sep="\n")
        return 0

    cases = [
        (suite, scenario)
        for suite, scenarios in CASES.items()
        for scenario in scenarios
        if (not args.suite and not args.case) or suite in args.suite or f"{suite}:{scenario}" in args.case
    ]
    panel = ("cli", "panel") in cases
    update = ("operations", "update") in cases
    if panel and args.panel_artifacts is None:
        run.error("cli:panel requires --panel-artifacts")
    if update and args.installed_artifacts is None:
        run.error("operations:update requires --installed-artifacts")
    pairs = {(client, model) for client, model, _ in args.runner}
    if len(pairs) != len(args.runner) or any(not client.strip() or not model.strip() for client, model in pairs):
        run.error("Provide each nonempty client/model pair only once")
    output = args.output.resolve()
    try:
        runners = [(client, model, onboarding.read_runner(Path(path))) for client, model, path in args.runner]
        output.mkdir(mode=0o700, parents=True)
        artifacts = {"current": freeze(args.artifacts, output / "artifacts/current", onboarding.ORIGIN)}
        if panel:
            artifacts["panel"] = freeze(args.panel_artifacts, output / "artifacts/panel", cli.PANEL_ORIGIN)
            if not artifacts["current"]["package_version"] or (
                artifacts["panel"]["package_version"] != artifacts["current"]["package_version"]
            ):
                raise ValueError("--panel-artifacts must use the same portable package version as --artifacts")
        if update:
            artifacts["installed"] = freeze(args.installed_artifacts, output / "artifacts/installed", onboarding.ORIGIN)
            if artifacts["installed"]["revision"] == artifacts["current"]["revision"]:
                raise ValueError("operations:update requires different installed and current revisions")
            if artifacts["installed"]["instance_id"] != artifacts["current"]["instance_id"]:
                raise ValueError("operations:update requires installed and current artifacts from the same instance")
    except (OSError, ValueError) as error:
        run.error(str(error))

    limits = {"timeout": args.timeout, "max_turns": args.max_turns, "output_tokens": args.output_tokens}
    passed = run_matrix(output, cases, runners, artifacts, args.workers, limits)
    print(output / "matrix.json")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
