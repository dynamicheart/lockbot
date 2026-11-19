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
import skill_cli_release_eval as cli_release
import skill_consultation_eval as consultation
import skill_continuity_eval as continuity
import skill_operations_eval as operations
import skill_resource_cli_eval as resource_cli
from skill_eval_artifact import load

CASES = {
    "cli": cli.SCENARIOS,
    "cli-release": tuple(cli_release.SCENARIOS),
    "onboarding": tuple(onboarding.SCENARIOS),
    "continuity": tuple(continuity.PROMPTS),
    "operations": tuple(operations.PROMPTS),
    "consultation": tuple(consultation.PROMPTS),
    "resource-cli": tuple(resource_cli.PROMPTS),
}
SECOND_PHASE = {
    "cli": {*cli.ENROLLMENT_SCENARIOS, *cli.intent.CONVERSATIONAL_PROMPTS, "panel"},
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
    (destination / "lockbot-agent.zip").write_bytes(artifact["archive"])
    return {
        "revision": manifest["version"],
        "sha256": manifest["installation"]["sha256"],
        "package_version": manifest.get("package_version"),
        "instance_id": manifest.get("instance_id", onboarding.INSTANCE),
        "origin": stamp["deployment_origin"],
        "skill_sha256": manifest["skill_sha256"],
    }


def prepare(root, suite, scenario, artifacts, *, cli_fixture_version=19, onboarding_fixture_version=7):
    current = artifacts / "current"
    source = current / "SKILL.md"
    if suite == "cli-release":
        cli_release.prepare(
            root,
            current,
            scenario,
            installed_artifact=artifacts / "installed" if scenario == "update-inquiry" else None,
        )
        return lambda root, final: cli_release.score(root, final)
    if suite == "resource-cli":
        resource_cli.prepare(root, current, scenario)
        return resource_cli.score
    if suite == "cli":
        cli.prepare(
            root,
            artifacts / "panel" if scenario == "panel" else current,
            scenario,
            fixture_version=cli_fixture_version,
        )
        return cli.score
    if suite == "onboarding":
        onboarding.prepare(root, None, scenario, artifacts=current, fixture_version=onboarding_fixture_version)
        if cli_fixture_version >= 33:
            path = root / "case.json"
            onboarding.save_json(path, {**json.loads(path.read_text()), "cli_argv_protocol": 1})
        return onboarding.score
    if suite == "continuity":
        artifact = artifacts / "panel" if scenario == "panel" else current
        continuity.prepare(
            root,
            artifact / "SKILL.md",
            scenario,
            artifact,
            fixture_version=continuity.host_v11.VERSION if scenario in continuity.host_v11.SCENARIOS else None,
        )
        if cli_fixture_version >= 33 and scenario in continuity.host_v11.SCENARIOS:
            path = root / "case.json"
            onboarding.save_json(path, {**json.loads(path.read_text()), "cli_argv_protocol": 1})
        return continuity.score
    if suite == "consultation":
        consultation.prepare(root, current, scenario)
        return consultation.score
    installed = artifacts / "installed" if scenario == "update" else None
    operations.prepare(
        root,
        source,
        scenario,
        current,
        installed,
        **({"fixture_version": 10} if scenario in {"queue", "update"} else {}),
    )
    return operations.score


def evaluate_phase(root, metadata, command, grader, limits):
    result = onboarding.evaluate(root, command, metadata["model"], grader, **limits)
    case = json.loads((root / "case.json").read_text())
    result.update(metadata, fixture_version=case["fixture_version"], skill_sha256=case["skill_sha256"])
    if case.get("fixture_family") == "onboarding" and case["fixture_version"] in {7, 8}:
        result.update(fixture_family="onboarding", http_policy_version=case.get("http_policy_version", 1))
    if continuity.host_v11.enabled(case):
        result.update(
            http_policy_version=case.get("http_policy_version", 1),
            fixture_family="continuity",
            artifact_kind=case["artifact_kind"],
            artifact_revision=case["artifact_revision"],
            origin=case["origin"],
        )
    if case.get("fixture_family") == "cli-release":
        result.update(fixture_family="cli-release", fixture_version=case["release_fixture_version"])
    onboarding.save_json(root / "result.json", result)
    return result


def fixture_error(path, metadata):
    message = path / "fixture-error.log"
    message.write_text(traceback.format_exc())
    message.chmod(0o600)
    case_path = path / "case.json"
    try:
        case = json.loads(case_path.read_text())
        keys = ("fixture_family", "fixture_version", "artifact_kind", "artifact_revision", "origin", "skill_sha256")
        if continuity.host_v11.enabled(case) and all(key in case for key in keys):
            metadata = {**metadata, **{key: case[key] for key in keys}}
    except (OSError, ValueError, TypeError):
        pass
    return {**metadata, "status": "fixture_error"}


def run_case(
    output, suite, scenario, number, runner, limits, revision, *, cli_fixture_version=19, onboarding_fixture_version=7
):
    client, model, command = runner
    case = output / suite / scenario / f"{number:03d}"
    case.mkdir(mode=0o700, parents=True)
    metadata = {"suite": suite, "scenario": scenario, "client": client, "model": model, "artifact_revision": revision}
    if suite == "cli":
        metadata["cli_fixture_version"] = cli_fixture_version
    if suite == "onboarding":
        metadata["onboarding_fixture_version"] = onboarding_fixture_version
    if suite == "continuity" and scenario in continuity.host_v11.SCENARIOS:
        metadata.update(
            fixture_family="continuity",
            fixture_version=continuity.host_v11.VERSION,
            artifact_kind="panel" if scenario == "panel" else "current",
        )
    first = case / "phase-1"
    first_metadata = {
        **metadata,
        "phase": 1,
        "evidence": first.relative_to(output).as_posix(),
        "simulated_user_actions": [],
    }
    if suite == "cli-release":
        first_metadata["simulated_user_actions"] = list(cli_release.SCENARIOS[scenario]["synthetic_actions"])
    try:
        grader = prepare(
            first,
            suite,
            scenario,
            output / "artifacts",
            cli_fixture_version=cli_fixture_version,
            onboarding_fixture_version=onboarding_fixture_version,
        )
        if suite == "continuity" and scenario in continuity.host_v11.SCENARIOS:
            case_metadata = json.loads((first / "case.json").read_text())
            metadata.update(
                origin=case_metadata["origin"],
                artifact_revision=case_metadata["artifact_revision"],
                skill_sha256=case_metadata["skill_sha256"],
            )
            first_metadata.update(metadata)
        result = evaluate_phase(first, first_metadata, command, grader, limits)
    except Exception:
        first.mkdir(mode=0o700, exist_ok=True)
        result = fixture_error(first, first_metadata)
    rows = [result]
    if suite == "cli" and scenario == "reset-personal-key":
        previous = first
        for phase in range(2, 5):
            metadata_phase = {**metadata, "phase": phase, "simulated_user_actions": []}
            if not rows[-1].get("observable_pass"):
                rows.append({**metadata_phase, "status": f"blocked_by_phase_{phase - 1}"})
                continue
            destination = case / f"phase-{phase}"
            metadata_phase["evidence"] = destination.relative_to(output).as_posix()
            try:
                cli.resume(previous, destination)
                if phase == 2:
                    metadata_phase["simulated_user_actions"].append("reset_impact_acknowledgment")
                elif phase == 3:
                    metadata_phase["simulated_user_actions"].append("chat_binding_confirmation")
                rows.append(evaluate_phase(destination, metadata_phase, command, grader, limits))
            except Exception:
                destination.mkdir(mode=0o700, exist_ok=True)
                rows.append(fixture_error(destination, metadata_phase))
            previous = destination
        return rows
    if scenario not in SECOND_PHASE.get(suite, ()):
        return rows
    fresh_read = suite == "cli" and scenario in cli.CHALLENGE_PROMPTS
    second_metadata = {**metadata, "phase": 2, "simulated_user_actions": []}
    third_metadata = {**metadata, "phase": 3, "simulated_user_actions": []}
    if not result.get("observable_pass"):
        rows.append({**second_metadata, "status": "blocked_by_phase_1"})
        if fresh_read:
            rows.append({**third_metadata, "status": "blocked_by_phase_1"})
        return rows
    second = case / "phase-2"
    second_metadata["evidence"] = second.relative_to(output).as_posix()
    try:
        if scenario == "panel":
            continuity.human_confirm(first)
            second_metadata["simulated_user_actions"].append("browser_consent")
        (cli if suite == "cli" else continuity).resume(first, second)
        if suite == "cli" and scenario in cli.ENROLLMENT_SCENARIOS:
            second_metadata["simulated_user_actions"].append("chat_binding_confirmation")
        rows.append(evaluate_phase(second, second_metadata, command, grader, limits))
    except Exception:
        second.mkdir(mode=0o700, exist_ok=True)
        rows.append(fixture_error(second, second_metadata))
    if not fresh_read:
        return rows
    if not rows[-1].get("observable_pass"):
        return [*rows, {**third_metadata, "status": "blocked_by_phase_2"}]
    third = case / "phase-3"
    third_metadata["evidence"] = third.relative_to(output).as_posix()
    try:
        cli.resume(second, third)
        rows.append(evaluate_phase(third, third_metadata, command, grader, limits))
    except Exception:
        third.mkdir(mode=0o700, exist_ok=True)
        rows.append(fixture_error(third, third_metadata))
    return rows


def preflight_runner(output, number, runner, timeout):
    client, model, command = runner
    root = output / "preflight" / f"{number:03d}"
    root.mkdir(mode=0o700, parents=True)
    (root / "prompt.txt").write_text("Reply with READY. No tools are needed.\n")
    (root / "system.txt").write_text("This is an isolated model connectivity check. Use no tools.\n")
    onboarding.save_json(root / "mcp.json", {"mcpServers": {}})
    result = onboarding.evaluate(
        root,
        command,
        model,
        lambda _, text: {"responded": bool(text.strip())},
        timeout=timeout,
        max_turns=2,
        output_tokens=128,
    )
    if result["status"] == "completed":
        result["status"] = "ready" if result.pop("responded") else "empty_response"
    result.update(client=client, evidence=root.relative_to(output).as_posix())
    onboarding.save_json(root / "result.json", result)
    return result


def run_matrix(
    output,
    cases,
    runners,
    artifacts,
    workers,
    limits,
    preflight_timeout,
    *,
    cli_fixture_version=19,
    onboarding_fixture_version=7,
):
    plan = list(product(cases, enumerate(runners, start=1)))
    metadata = {
        "artifacts": artifacts,
        "limits": limits,
        "preflight_timeout": preflight_timeout,
        "workers": workers,
        "cli_fixture_version": cli_fixture_version,
        "onboarding_fixture_version": onboarding_fixture_version,
        "simulation_only": True,
        "cases": [f"{suite}:{scenario}" for suite, scenario in cases],
        "runners": [{"client": client, "model": model} for client, model, _ in runners],
    }
    onboarding.save_json(output / "run.json", metadata)
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool, (output / "progress.jsonl").open("w") as progress:
        probes = list(pool.map(lambda item: preflight_runner(output, *item, preflight_timeout), enumerate(runners, 1)))
        onboarding.save_json(output / "preflight.json", probes)
        pending = {}
        for index, ((suite, scenario), (number, runner)) in enumerate(plan):
            artifact = "panel" if scenario == "panel" and suite in {"cli", "continuity"} else "current"
            probe = probes[number - 1]
            if probe["status"] != "ready":
                row = {
                    "suite": suite,
                    "scenario": scenario,
                    "client": probe["client"],
                    "model": probe["model"],
                    "artifact_revision": artifacts[artifact]["revision"],
                    "phase": 1,
                    "status": "blocked_by_model_preflight",
                    "preflight_status": probe["status"],
                    "preflight_evidence": probe["evidence"],
                }
                if suite == "cli":
                    row["cli_fixture_version"] = cli_fixture_version
                if suite == "onboarding":
                    row["onboarding_fixture_version"] = onboarding_fixture_version
                if suite == "continuity" and scenario in continuity.host_v11.SCENARIOS:
                    row.update(
                        fixture_family="continuity",
                        fixture_version=continuity.host_v11.VERSION,
                        artifact_kind=artifact,
                        origin=artifacts[artifact]["origin"],
                        skill_sha256=artifacts[artifact]["skill_sha256"],
                    )
                results[index] = [row]
                progress.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue
            future = pool.submit(
                run_case,
                output,
                suite,
                scenario,
                number,
                runner,
                limits,
                artifacts[artifact]["revision"],
                cli_fixture_version=cli_fixture_version,
                onboarding_fixture_version=onboarding_fixture_version,
            )
            pending[future] = index
        progress.flush()
        for future in as_completed(pending):
            rows = future.result()
            results[pending[future]] = rows
            for row in rows:
                progress.write(json.dumps(row, ensure_ascii=False) + "\n")
            progress.flush()
    ordered = [row for index in range(len(plan)) for row in results[index]]
    onboarding.save_json(output / "matrix.json", {**metadata, "preflight": probes, "results": ordered})
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
    run.add_argument(
        "--cli-fixture-version",
        type=int,
        choices=(19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35),
        default=19,
    )
    run.add_argument("--onboarding-fixture-version", type=int, choices=(7, 8), default=7)
    run.add_argument(
        "--preflight-timeout", type=positive_int, default=45, help="Seconds for each model connectivity probe"
    )
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
    panel = any(scenario == "panel" and suite in {"cli", "continuity"} for suite, scenario in cases)
    update = ("operations", "update") in cases
    release_update = ("cli-release", "update-inquiry") in cases
    if panel and args.panel_artifacts is None:
        run.error("cli:panel and continuity:panel require --panel-artifacts")
    if update and args.installed_artifacts is None:
        run.error("operations:update requires --installed-artifacts")
    if release_update and args.installed_artifacts is None:
        run.error("cli-release:update-inquiry requires --installed-artifacts")
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
        if update or release_update:
            artifacts["installed"] = freeze(args.installed_artifacts, output / "artifacts/installed", onboarding.ORIGIN)
            if artifacts["installed"]["revision"] == artifacts["current"]["revision"]:
                raise ValueError("An update case requires different installed and current revisions")
            if artifacts["installed"]["instance_id"] != artifacts["current"]["instance_id"]:
                raise ValueError("operations:update requires installed and current artifacts from the same instance")
    except (OSError, ValueError) as error:
        run.error(str(error))

    limits = {"timeout": args.timeout, "max_turns": args.max_turns, "output_tokens": args.output_tokens}
    passed = run_matrix(
        output,
        cases,
        runners,
        artifacts,
        args.workers,
        limits,
        args.preflight_timeout,
        cli_fixture_version=args.cli_fixture_version,
        onboarding_fixture_version=args.onboarding_fixture_version,
    )
    print(output / "matrix.json")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
