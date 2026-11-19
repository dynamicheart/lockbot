"""Bounded, presentation-only review metadata; original trace IDs remain authoritative."""

import hashlib
import json
import re

SHA = re.compile(r"[a-f0-9]{64}")


def checked_assessment(value, source_run, data):
    """Reject unsupported policy/schema or forged review structure, never infer approval."""
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or set(value)
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
        }
    ):
        raise ValueError("Invalid assessment")
    target, binding, adapter = value.get("target", {}), value.get("binding", {}), value.get("adapter", {})
    if (
        not isinstance(target, dict)
        or set(target) != {"suite", "scenario", "client", "model", "phase", "case_id", "source_run"}
        or any(not isinstance(item, str) or len(item) > 512 for key, item in target.items() if key != "phase")
        or target.get("source_run") != source_run
        or type(target.get("phase")) is not int
        or target["phase"] != 1
        or target.get("suite") not in {"resource-cli", "consultation"}
        or target.get("case_id") != f"{target['suite']}-{target.get('scenario')}"
        or not isinstance(adapter, dict)
        or adapter.get("family") != target["suite"]
        or type(adapter.get("version")) is not int
        or adapter["version"] not in ({1, 2, 3} if target["suite"] == "consultation" else {1, 2})
        or not any(trial.get("source_run") == source_run for trial in data["cases"].get(target["case_id"], []))
        or value.get("execution") != "completed"
        or value.get("behavior") not in {"pass", "fail"}
        or value.get("human_gate") != "not_required"
        or value.get("semantic") not in {"not_reviewed", "pass", "findings", "uncertain"}
        or not isinstance(binding, dict)
        or not 8 <= len(binding) <= 16
        or any(
            not isinstance(key, str)
            or len(key) > 80
            or (item is not None and (not isinstance(item, str) or not SHA.fullmatch(item)))
            for key, item in binding.items()
        )
        or not isinstance(value.get("topics"), list)
        or len(value["topics"]) > 8
        or any(not isinstance(item, str) or len(item) > 600 for item in value["topics"])
        or not isinstance(value.get("reviews"), list)
        or len(value["reviews"]) > 16
    ):
        raise ValueError("Unsupported assessment policy or target")
    reviews = value["reviews"]
    ids = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Invalid model review")
        fields = {key: item for key, item in review.items() if key != "review_id"}
        identity = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        author = review.get("reviewer", {})
        if (
            set(review)
            != {
                "schema_version",
                "target",
                "binding",
                "reviewer",
                "status",
                "findings",
                "report_sha256",
                "bound_at",
                "supersedes",
                "review_id",
            }
            or review.get("review_id") != identity
            or identity in ids
            or type(review.get("schema_version")) is not int
            or review["schema_version"] != 1
            or not isinstance(review.get("bound_at"), str)
            or len(review["bound_at"]) > 80
            or review.get("target") != target
            or review.get("binding") != binding
            or not isinstance(author, dict)
            or set(author) != {"kind", "name", "requested_model"}
            or author["kind"] != "model"
            or any(
                not isinstance(author[key], str) or not 1 <= len(author[key]) <= 160
                for key in ("name", "requested_model")
            )
            or review.get("status") not in {"pass", "findings", "uncertain"}
            or not isinstance(review.get("report_sha256"), str)
            or not SHA.fullmatch(review["report_sha256"])
            or not isinstance(review.get("findings"), list)
            or len(review["findings"]) > 4
            or (review.get("supersedes") is not None and review["supersedes"] not in ids)
        ):
            raise ValueError("Invalid model review provenance")
        for finding in review["findings"]:
            if (
                not isinstance(finding, dict)
                or set(finding) != {"severity", "blocking", "text"}
                or finding["severity"] not in {"P0", "P1", "P2", "P3"}
                or type(finding["blocking"]) is not bool
                or not isinstance(finding["text"], str)
                or not 1 <= len(finding["text"]) <= 600
            ):
                raise ValueError("Invalid model finding")
        if (review["status"] == "pass" and review["findings"]) or (
            review["status"] == "findings" and not review["findings"]
        ):
            raise ValueError("Invalid model verdict")
        ids.add(identity)
    superseded = {review["supersedes"] for review in reviews if review.get("supersedes")}
    statuses = {review["status"] for review in reviews if review["review_id"] not in superseded}
    semantic = "not_reviewed" if not statuses else next(iter(statuses)) if len(statuses) == 1 else "uncertain"
    if value["semantic"] != semantic:
        raise ValueError("Assessment semantic status changed")
    return value
