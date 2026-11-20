"""Controlled legacy DTOs for archived grader unit tests, never model evidence.

The current packaged SDK is exercised separately. These deep copies only model
the old JSON shape for a pure grader call; captured tool/HTTP/case files stay
unchanged. This neither executes an old SDK nor reconstructs an old artifact.
"""

import copy
from urllib.parse import urlsplit

from dev.test_skill_cli_eval import evaluation
from dev.test_skill_cli_v20 import events


def legacy_intent_checks(fixture, final):
    assert 19 <= fixture.case["fixture_version"] <= 33
    paths = [fixture.root / name for name in ("trace.jsonl", "cli-http.jsonl", "case.json")]
    originals = {path: path.read_bytes() for path in paths}
    tools = copy.deepcopy(events(fixture))
    requests = copy.deepcopy(events(fixture, "cli-http.jsonl"))
    for tool in tools:
        snapshot = tool.get("result", {}).get("result", {}).get("verified_snapshot")
        if isinstance(snapshot, dict):
            assert "health" not in snapshot and "bot_service_health" in snapshot
            snapshot["health"] = snapshot.pop("bot_service_health")
    # Match score()'s scope URL normalization on copied transport observations.
    for row in requests:
        if row.get("resource_bot") is not None:
            parsed = urlsplit(row["url"])
            row["url"] = parsed._replace(
                path=evaluation.base.bot_resource_path(parsed.path, row["resource_bot"])
            ).geturl()
    proof = evaluation.verified_sdk_resolutions(fixture, tools, requests)
    checks = evaluation.intent.score(
        fixture.case["scenario"],
        requests,
        tools,
        final,
        phase=fixture.case["phase"],
        fixture_version=fixture.case["fixture_version"],
        sdk_resolutions=proof,
    )
    assert all(path.read_bytes() == content for path, content in originals.items())
    return checks
