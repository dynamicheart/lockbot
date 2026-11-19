"""Explicit CLI35 review policy shared by the private publisher and trace reader."""

import hashlib
import json
import re

from lockbot.trace_evidence_files import NATIVE_PHASE_FILES

PHASE_COUNTS = {
    "approved-enrollment": 2,
    "reset-personal-key": 4,
    "telemetry": 1,
    "intent-shared-tail": 1,
    "intent-ambiguous-tail": 1,
    "intent-my-locks": 1,
    "intent-my-books": 1,
    "intent-reminder-configured": 1,
    "intent-choose": 1,
    "intent-whole": 1,
    "intent-claim": 1,
    "intent-model-p800-unknown-node": 1,
}
ROLE_POLICY = [
    "Review the exact reported owners, resources, cards and roles against the observed state. "
    "Check explanations against the installed Skill; no read-only timestamp proves reminder delivery."
]
TOPICS = [
    "Compare every answer and action with its own observed phase, actor, resource, scope and installed Skill.",
    "Preserve binding/reset acknowledgments, incomplete phases and unknown state; "
    "do not infer live acceptance, reminder delivery or human approval.",
]
FILES = (
    "case.json",
    "subject.md",
    "prompt.txt",
    "response.json",
    "result.json",
    "trace.jsonl",
    "cli-http.jsonl",
    "system.txt",
    "runner.stdout",
    "binding.json",
    "browser.json",
    "mcp.json",
    *NATIVE_PHASE_FILES,
)
SHA = re.compile(r"[a-f0-9]{64}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def case_id(scenario):
    return "case-3" if scenario == "reset-personal-key" else scenario


def known_policy(value, scenario):
    if any(key in value for key in ("human_gate", "human_review_gate")):
        return False
    allowed = ROLE_POLICY if scenario in {"intent-my-locks", "intent-my-books", "intent-reminder-configured"} else []
    return value.get("manual_review_required") in (None, [], allowed)


def checked_header(value, source_run, data):
    """Reject unsupported run policy or incomplete evidence before accepting reviews."""
    target, binding = value.get("target", {}), value.get("binding", {})
    if (
        set(value)
        != {
            "schema_version",
            "target",
            "binding",
            "adapter",
            "execution",
            "behavior",
            "semantic",
            "human_gate",
            "topics",
            "reviews",
            "phase_evidence",
        }
        or not isinstance(target, dict)
        or set(target) != {"suite", "scenario", "client", "model", "phase_scope", "case_id", "source_run"}
        or any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in target.values())
        or target.get("suite") != "cli"
        or target.get("phase_scope") != "all"
        or target.get("source_run") != source_run
        or target.get("scenario") not in PHASE_COUNTS
        or target.get("case_id") != case_id(target.get("scenario"))
        or value.get("adapter") != {"family": "cli", "version": 35}
        or value.get("human_gate") != "not_required"
        or value.get("topics") != TOPICS
        or not any(trial.get("source_run") == source_run for trial in data["cases"].get(target["case_id"], []))
        or not isinstance(binding, dict)
        or set(binding)
        != {
            "plan",
            "matrix",
            "phase_evidence",
            "artifact/manifest.json",
            "artifact/SKILL.md",
            "artifact/lockbot-agent.zip",
        }
        or any(not isinstance(item, str) or not SHA.fullmatch(item) for item in binding.values())
        or not isinstance(value.get("phase_evidence"), list)
        or len(value["phase_evidence"]) != PHASE_COUNTS[target["scenario"]]
        or binding["phase_evidence"] != digest(value["phase_evidence"])
    ):
        raise ValueError("Unsupported CLI run assessment policy")
    for number, phase in enumerate(value["phase_evidence"], 1):
        if (
            not isinstance(phase, dict)
            or set(phase) != {"phase", "status", "observed", "completed", "observable_pass", "hashes", "matrix_row"}
            or type(phase.get("phase")) is not int
            or phase["phase"] != number
            or not isinstance(phase.get("status"), str)
            or not 1 <= len(phase["status"]) <= 80
            or type(phase.get("observed")) is not bool
            or type(phase.get("completed")) is not bool
            or phase.get("observable_pass") not in (True, False, None)
            or (phase.get("observable_pass") is not None and type(phase["observable_pass"]) is not bool)
            or not isinstance(phase.get("hashes"), dict)
            or set(phase["hashes"]) != set(FILES)
            or any(
                item is not None and (not isinstance(item, str) or not SHA.fullmatch(item))
                for item in phase["hashes"].values()
            )
            or (
                phase.get("matrix_row") is not None
                and (not isinstance(phase["matrix_row"], str) or not SHA.fullmatch(phase["matrix_row"]))
            )
            or (phase["observed"] and phase["matrix_row"] is None)
            or (
                phase["observed"]
                != all(
                    phase["hashes"][name] is not None
                    for name in ("case.json", "subject.md", "prompt.txt", "result.json")
                )
            )
            or phase["completed"]
            != (phase["observed"] and phase["status"] == "completed" and phase["hashes"]["response.json"] is not None)
            or (not phase["observed"] and any(item is not None for item in phase["hashes"].values()))
        ):
            raise ValueError("Invalid CLI phase evidence")
    complete = all(phase["completed"] for phase in value["phase_evidence"])
    behavior = (
        "fail"
        if any(phase["observable_pass"] is False for phase in value["phase_evidence"])
        else (
            "pass"
            if complete and all(phase["observable_pass"] is True for phase in value["phase_evidence"])
            else "unknown"
        )
    )
    if value.get("execution") != ("completed" if complete else "incomplete") or value.get("behavior") != behavior:
        raise ValueError("CLI run execution status changed")
    if not isinstance(value.get("reviews"), list) or len(value["reviews"]) > 16:
        raise ValueError("Invalid CLI reviews")


def checked_coverage(review, assessment):
    phases = review.get("reviewed_phases")
    observed = [phase["phase"] for phase in assessment["phase_evidence"] if phase["observed"]]
    if (
        not isinstance(phases, list)
        or not phases
        or any(type(item) is not int for item in phases)
        or phases != sorted(set(phases))
        or any(item not in observed for item in phases)
        or (
            review["status"] == "pass"
            and (
                assessment["execution"] != "completed"
                or phases != list(range(1, len(assessment["phase_evidence"]) + 1))
            )
        )
    ):
        raise ValueError("Model review does not cover completed CLI phases")
