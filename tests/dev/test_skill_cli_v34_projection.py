"""New compact DTO grading is explicit; archived health-only grading stays intact."""

import json

import pytest
from test_skill_cli_argv_protocol import cli, prepare, run


@pytest.mark.parametrize(
    "version,full,passes", [(33, False, False), (33, True, True), (34, False, True), (34, True, True)]
)
def test_actual_current_sdk_projection_requires_new_fixture_or_explicit_raw_mode(tmp_path, version, full, passes):
    fixture = prepare(tmp_path, "intent-lock-tail", version=version)
    argv = ["command", "--operation", "lock", "--node", "19", *(["--full"] if full else [])]
    response = run(fixture, *argv)
    assert response["result"]["verified"] is True
    snapshot = response["result"]["verified_snapshot"]
    assert snapshot["health" if full else "bot_service_health"] == "running"
    assert ("health" in snapshot) is full
    assert ("bot_service_health" in snapshot) is not full
    scored = cli.score(fixture.root, "已锁定19，约3小时后到期。")
    assert scored["observable_pass"] is passes, scored
    wire = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert sum(row["method"] == "POST" for row in wire) == 1


def test_v34_never_interpolates_legacy_health_into_a_compact_observation(tmp_path):
    fixture = prepare(tmp_path, "intent-lock-tail", version=34)
    run(fixture, "command", "--operation", "lock", "--node", "19")
    path = fixture.root / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    snapshot = rows[-1]["result"]["result"]["verified_snapshot"]
    snapshot["health"] = snapshot.pop("bot_service_health")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert not cli.score(fixture.root, "已锁定19，约3小时后到期。")["observable_pass"]
