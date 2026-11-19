"""Run the shipped helper: anonymous inspection must not imply a current install."""

import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from lockbot.backend.app.resource_api import skill

from backend.test_portable_resource_package import extract
from backend.test_resource_agent_helper import case, helper  # noqa: F401


def run_discover(case, script=None):  # noqa: F811
    result = subprocess.run(
        [sys.executable, "-S", str(script or case.root / helper.SCRIPT_PATH), "discover", "--origin", case.base],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(case.state.requests) == 1
    path, method, headers, _ = case.state.requests[0]
    assert (path, method) == ("/api/agent-skill", "GET")
    assert "Authorization" not in headers
    return json.loads(result.stdout)


def tree_hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_packaged_discovery_verifies_current_bundle_without_installing(case):  # noqa: F811
    before = tree_hashes(case.root)
    result = run_discover(case)
    assert result["update_required"] is False
    assert result["update_check"] == {
        "status": "current",
        "installed_version": case.state.manifest["version"],
        "installed_package_version": case.state.manifest["package_version"],
        "comparison": "package_version",
    }
    assert tree_hashes(case.root) == before


def test_changed_remote_package_is_an_update_even_with_compatible_api(case, monkeypatch):  # noqa: F811
    # Build a real alternate public package, not an invented comparison result.
    original = skill.files

    class ChangedResources:
        def joinpath(self, relative):
            path = original("lockbot.backend.app").joinpath(relative)
            if relative.endswith("scripts/lockbot_cli.py"):
                return SimpleNamespace(read_bytes=lambda: path.read_bytes() + b"\n# Alternate published revision.\n")
            return path

    monkeypatch.setattr(skill, "files", lambda _: ChangedResources())
    skill._skill_artifacts.cache_clear()
    try:
        remote = skill._skill_artifacts("")[2]
    finally:
        skill._skill_artifacts.cache_clear()
    before = tree_hashes(case.root)
    installed_package = case.state.manifest["package_version"]
    assert installed_package != remote["package_version"]
    case.state.manifest.update(remote)
    result = run_discover(case)
    assert result["result"]["api_version"] == helper.API_VERSION
    assert result["update_required"] is True
    assert result["update_check"]["status"] == "update_available"
    assert result["update_check"]["installed_package_version"] == installed_package
    assert result["result"]["package_version"] == remote["package_version"]
    assert tree_hashes(case.root) == before


@pytest.mark.parametrize("invalid", ["standalone", "missing-stamp", "missing-reference", "tampered", "bad-package"])
def test_bootstrap_or_unverified_installation_reports_unknown_not_current(case, invalid):  # noqa: F811
    script = None
    if invalid == "standalone":
        script = case.tmp / "standalone/scripts/lockbot_cli.py"
        script.parent.mkdir(parents=True)
        script.write_bytes((case.root / helper.SCRIPT_PATH).read_bytes())
    elif invalid == "missing-stamp":
        (case.root / "installation.json").unlink()
    elif invalid == "missing-reference":
        (case.root / helper.REFERENCE_PATHS[0]).unlink()
    elif invalid == "tampered":
        path = case.root / "SKILL.md"
        path.write_bytes(path.read_bytes() + b"\nUnverified local changes.\n")
    else:
        path = case.root / "installation.json"
        stamp = json.loads(path.read_text())
        stamp["package_version"] = "0" * 16
        path.write_text(json.dumps(stamp))
    before = tree_hashes(case.root)
    result = run_discover(case, script)
    assert result["update_required"] is None
    assert result["update_check"]["status"] == "unknown"
    assert "installed_version" not in result["update_check"]
    assert result["result"]["package_version"] == case.state.manifest["package_version"]
    assert tree_hashes(case.root) == before


def test_portable_same_package_other_origin_is_current(case):  # noqa: F811
    installed = extract(case.root, "https://other-approved.example.test")
    assert installed["version"] != case.state.manifest["version"]
    assert installed["package_version"] == case.state.manifest["package_version"]
    before = tree_hashes(case.root)
    result = run_discover(case)
    assert result["update_required"] is False
    assert result["update_check"]["status"] == "current"
    assert result["update_check"]["installed_version"] == installed["version"]
    assert result["update_check"]["comparison"] == "package_version"
    assert tree_hashes(case.root) == before


def test_server_without_package_metadata_uses_existing_rendered_comparison(case):  # noqa: F811
    case.state.manifest.pop("package_format")
    case.state.manifest.pop("package_version")
    result = run_discover(case)
    assert result["update_required"] is False
    assert result["update_check"]["status"] == "current"
    assert result["update_check"]["comparison"] == "rendered_version"


def test_invalid_remote_service_still_fails_before_local_comparison(case):  # noqa: F811
    case.state.manifest["api_version"] = "unsupported"
    result = subprocess.run(
        [sys.executable, "-S", str(case.root / helper.SCRIPT_PATH), "discover", "--origin", case.base],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"] == "unsupported_service"
    assert not result.stdout
    assert len(case.state.requests) == 1
    assert all(row[1] == "GET" and "Authorization" not in row[2] for row in case.state.requests)


@pytest.mark.parametrize("prior_notice", [False, True])
def test_failed_canonical_check_is_atomic_and_cannot_bypass_later_guards(case, prior_notice):  # noqa: F811
    path = case.root / "installation.json"
    stamp = json.loads(path.read_text())
    stamp["package_version"] = "0" * 16
    path.write_text(json.dumps(stamp))
    case.client.discover()
    case.client.needs_update = prior_notice
    for _ in range(2):
        status = case.client.discovery_update_check()
        assert status == {"status": "unknown", "reason": "installed_package_modified"}
        assert case.client.version is None and case.client.package_version is None
        assert case.client.needs_update is prior_notice
    for action in (case.client.context, lambda: case.client.command("lock", ["node-a"], [], "2m")):
        with pytest.raises(helper.ClientError, match="installed_package_modified"):
            action()
    assert len(case.state.requests) == 1
    assert all(row[1] == "GET" and "Authorization" not in row[2] for row in case.state.requests)
