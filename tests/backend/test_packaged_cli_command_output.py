"""Verify command projection on the served SDK and actual synthetic backend routes."""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.

import copy
import json

import pytest

from backend.test_packaged_cli_auto_scope import command, sdk  # noqa: F401
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "DEVICE"], indirect=True)
@pytest.mark.parametrize("full,exact", [(False, False), (True, False), (False, True), (True, True)])
def test_projection_keeps_one_complete_verified_state_without_extra_requests(sdk, monkeypatch, full, exact):
    original = sdk.module.command_output
    captured = {}

    def project(result, **options):
        before = copy.deepcopy(result)
        output = original(result, **options)
        assert result == before, "Output projection must not mutate the verified SDK result"
        captured.update(before=before, output=output)
        return output

    monkeypatch.setattr(sdk.module, "command_output", project)
    before_files = {str(p): p.read_bytes() for p in sdk.catalog.rglob("*") if p.is_file()}
    flags = [*(["--full"] if full else []), *(["--exact-lease-times"] if exact else [])]
    output, events = command(sdk, "n1", "lock", *flags)
    result, before = output["result"], captured["before"]
    assert [row["method"] for row in events] == ["GET"] * 5 + ["POST"]
    assert before_files == {str(p): p.read_bytes() for p in sdk.catalog.rglob("*") if p.is_file()}
    assert result["resolution"] == before["resolution"]
    for key in ("verified", "verification", "command_outcome", "duration_source", "update_required", "job_runner"):
        assert result.get(key) == before.get(key)
    actual, raw = result["verified_snapshot"], before["verified_snapshot"]
    for key in raw.keys() - {"context", "health"}:
        assert actual[key] == raw[key]
    for key in ("actor", "bot", "bot_rules"):
        assert actual["context"][key] == raw["context"][key]
    embedded = result["command_result"]["resource_snapshot"]
    if full:
        assert result == before
        assert embedded == actual
    else:
        assert actual["bot_service_health"] == raw["health"] and "health" not in actual
        assert embedded["reference"] == "verified_snapshot" and "resources" not in embedded
        assert embedded["captured_at"] == actual["captured_at"]
        assert embedded["bot_id"] == actual["bot_id"] and embedded["actor"] == actual["actor"]
        assert embedded["context"]["bot"]["uuid"] == actual["context"]["bot"]["uuid"]
        assert embedded["context"]["server"]["instance_id"] == actual["context"]["server"]["instance_id"]
        assert len(json.dumps(result)) < len(json.dumps(before)) * 0.6
    units = actual["resources"][0].get("devices") or [actual["resources"][0]]
    holding = units[0]["holders"][0]
    assert ("expires_at" in holding) is exact
    assert ("remaining_time" in holding) is not exact


def test_nonidentical_observations_and_unverified_results_are_not_deduplicated(sdk):
    result = {
        "verified": True,
        "verified_snapshot": {"captured_at": "new", "resources": []},
        "command_result": {"resource_snapshot": {"captured_at": "older", "resources": [{"id": "n1"}]}},
    }
    before = copy.deepcopy(result)
    assert sdk.module.command_output(result) == before
    result["verified"] = False
    assert sdk.module.command_output(result) == result


@pytest.mark.parametrize("verified", [True, False, None])
def test_compact_health_names_bot_lifecycle_without_rewriting_observations(sdk, verified):
    result = {
        "verified": verified,
        "verified_snapshot": {"health": "running", "resources": [{"id": "n1", "health": "node-metric"}]},
        "command_result": {"resource_snapshot": {"health": "error", "captured_at": "older"}},
    }
    before = copy.deepcopy(result)
    compact = sdk.module.command_output(result)
    assert compact["verified"] is verified
    assert compact["verified_snapshot"] == {
        "bot_service_health": "running",
        "resources": [{"id": "n1", "health": "node-metric"}],
    }
    assert compact["command_result"]["resource_snapshot"] == {"bot_service_health": "error", "captured_at": "older"}
    assert result == before
    assert sdk.module.command_output(result, full=True) is result


@pytest.mark.parametrize("result", [{}, {"verified": True}, {"verified_snapshot": None}, {"command_result": []}])
def test_sparse_command_projection_does_not_invent_health_or_snapshots(sdk, result):
    before = copy.deepcopy(result)
    assert sdk.module.command_output(result) == before
    assert result == before
