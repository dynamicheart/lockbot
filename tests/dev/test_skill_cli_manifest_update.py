"""Manifest-only update checks use real packaged argv without protected reads."""

import json

import pytest
from test_skill_cli_argv_protocol import artifact, release, run, variant_artifact


def prepare(tmp_path, *, version=4):
    current = artifact(tmp_path)
    old = variant_artifact(current, tmp_path / "older")
    root = tmp_path / "update"
    release.prepare(root, current, "update-inquiry", installed_artifact=old, fixture_version=version)
    return release.ReleaseFixture(root)


def read_installed(fixture):
    return fixture.call("read_lockbot_skill", {"offset": 0, "reference": "installation.json"})


def discover(fixture):
    result = run(fixture, "discover", "--origin", fixture.origin)
    assert result.get("exit_code") == 0, result
    rows = [json.loads(line) for line in (fixture.root / "cli-http.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["url"] == fixture.origin + "/api/agent-skill"
    assert rows[0]["authorization_present"] is False
    assert rows[0]["method"] == "GET"
    return result


def test_v4_accepts_verified_installed_metadata_and_anonymous_discovery_only(tmp_path):
    fixture = prepare(tmp_path)
    read_installed(fixture)
    discover(fixture)
    score = release.score(fixture.root, "有新版，尚未安装。")
    assert score["observable_pass"], score
    assert score["read_count"] == 1
    assert score["installation_unchanged"]


@pytest.mark.parametrize(
    "negative",
    [
        "no-local-read",
        "forged-local-read",
        "equal-version",
        "forged-manifest",
        "wrong-origin",
        "missing-cli-provenance",
    ],
)
def test_v4_rejects_incomplete_or_forged_manifest_comparison(tmp_path, negative):
    fixture = prepare(tmp_path)
    if negative != "no-local-read":
        read_installed(fixture)
    if negative != "forged-manifest":
        discover(fixture)
    if negative == "equal-version":
        path = fixture.root / "case.json"
        case = json.loads(path.read_text())
        case["installed_package_version"] = fixture.manifest()["package_version"]
        path.write_text(json.dumps(case))
    elif negative == "forged-local-read":
        path = fixture.root / "trace.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["result"]["content"] = "Not the installed metadata"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif negative == "forged-manifest":
        path = fixture.root / "cli-http.jsonl"
        path.write_text(
            json.dumps(
                {
                    "url": fixture.origin + "/api/agent-skill",
                    "method": "GET",
                    "status": 200,
                    "authorization_present": False,
                }
            )
            + "\n"
        )
    elif negative == "wrong-origin":
        path = fixture.root / "cli-http.jsonl"
        row = json.loads(path.read_text())
        row["url"] = "http://unapproved.example.test/api/agent-skill"
        path.write_text(json.dumps(row) + "\n")
    elif negative == "missing-cli-provenance":
        path = fixture.root / "trace.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1].pop("argv")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert not release.score(fixture.root, "有新版。")["observable_pass"]


def test_v3_keeps_its_original_protected_read_requirement(tmp_path):
    fixture = prepare(tmp_path, version=3)
    read_installed(fixture)
    result = fixture.call("run_lockbot", {"action": "discover"})
    assert "error" not in result
    score = release.score(fixture.root, "有新版。")
    assert score["update_required_observed"]
    assert score["read_count"] == 0
    assert not score["observable_pass"]
