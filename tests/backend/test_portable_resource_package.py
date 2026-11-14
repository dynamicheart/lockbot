"""Origin-independent instruction compatibility without sharing connection authority."""

import hashlib
import io
import json
import zipfile

import pytest
from lockbot.backend.app.resource_api import skill
from starlette.datastructures import Headers

from backend.test_resource_agent_helper import case, commands, helper  # noqa: F401


def extract(root, origin):
    _, archive, metadata = skill._skill_artifacts("agent", origin)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in zipped.namelist():
            path = root / name.split("/", 1)[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zipped.read(name))
    return metadata


def test_two_origins_keep_distinct_rendered_versions_and_equal_package(tmp_path):
    first = extract(tmp_path / "first", "https://first.example.test")
    second = extract(tmp_path / "second", "https://SECOND.example.test:443")
    assert first["version"] != second["version"]
    assert first["skill_sha256"] != second["skill_sha256"]
    assert first["installation"]["sha256"] != second["installation"]["sha256"]
    assert first["package_version"] == second["package_version"]
    for folder, meta in [("first", first), ("second", second)]:
        assert helper.installed_version(tmp_path / folder) == meta["version"]
        assert helper.installed_package_version(tmp_path / folder) == meta["package_version"]
    assert "https://first.example.test" in (tmp_path / "first/SKILL.md").read_text()


def test_same_package_other_origin_allows_write_without_changing_pins(case):  # noqa: F811
    extract(case.root, "https://installed.example.test")
    before = case.path.read_bytes()
    assert case.client.command("lock", ["node-a"], [], "2m")["verified"]
    assert not case.client.needs_update
    assert case.path.read_bytes() == before
    assert len(commands(case)) == 1
    request = case.state.requests[1][2]
    assert request["X-Lockbot-Client-Package-Version"] == case.state.manifest["package_version"]
    assert request["X-Lockbot-Client-Skill-Version"] != case.state.manifest["version"]


@pytest.mark.parametrize("changed", ["markdown", "script"])
def test_actual_instruction_or_helper_change_blocks_mutation(case, changed):  # noqa: F811
    # Build a genuine changed server revision, rather than trusting only a rendered hash.
    canonical = (case.root / "SKILL.md").read_bytes()
    script = (case.root / helper.SCRIPT_PATH).read_bytes()
    if changed == "markdown":
        canonical += b"\nChanged instruction.\n"
    else:
        script += b"\n# Changed helper\n"
    case.state.manifest["package_version"] = hashlib.sha256(canonical + b"\0" + script).hexdigest()[:16]
    with pytest.raises(helper.ClientError, match="update_bundle_before_mutation"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)


@pytest.mark.parametrize("legacy", ["server", "stamp"])
def test_legacy_without_portable_metadata_is_conservative(case, legacy):  # noqa: F811
    extract(case.root, "https://installed.example.test")
    if legacy == "server":
        case.state.manifest.pop("package_version")
        case.state.manifest.pop("package_format")
    else:
        path = case.root / "installation.json"
        stamp = json.loads(path.read_text())
        for field in ["package_version", "package_format", "deployment_origin"]:
            stamp.pop(field)
        path.write_text(json.dumps(stamp))
    with pytest.raises(helper.ClientError, match="update_bundle_before_mutation"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)


@pytest.mark.parametrize("tamper", ["locator", "extra_locator", "origin", "format", "package"])
def test_package_normalization_does_not_strip_unknown_text(case, tamper):  # noqa: F811
    extract(case.root, "https://installed.example.test")
    path = case.root / "installation.json"
    stamp = json.loads(path.read_text())
    markdown = (case.root / "SKILL.md").read_bytes()
    if tamper == "locator":
        markdown = markdown.replace(b"Deployment Skill URL:", b"Other Skill URL:")
    elif tamper == "extra_locator":
        markdown += (
            b"\n"
            + helper.deployment_fragments("https://installed.example.test")[
                "<!-- LOCKBOT_DEPLOYMENT_SKILL_URL -->"
            ].encode()
        )
    elif tamper == "origin":
        stamp["deployment_origin"] = "https://other.example.test"
    elif tamper == "format":
        stamp["package_format"] = "future-unverified-format"
    else:
        stamp["package_version"] = "0" * 16
    # Even if the rendered stamp is recomputed, normalization must not hide a changed instruction.
    (case.root / "SKILL.md").write_bytes(markdown)
    stamp["files"]["SKILL.md"] = hashlib.sha256(markdown).hexdigest()
    stamp["version"] = hashlib.sha256(markdown + b"\0" + (case.root / helper.SCRIPT_PATH).read_bytes()).hexdigest()[:16]
    path.write_text(json.dumps(stamp))
    with pytest.raises(helper.ClientError):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)
    assert all("Authorization" not in row[2] for row in case.state.requests)


def test_package_header_notice_does_not_replace_legacy_rendered_status():
    metadata = skill.skill_artifacts()[2]
    assert skill.package_update_status(Headers({}), metadata) == "unknown"
    assert (
        skill.package_update_status(
            Headers({"X-Lockbot-Client-Package-Version": metadata["package_version"]}), metadata
        )
        == "current"
    )
    assert (
        skill.package_update_status(Headers({"X-Lockbot-Client-Package-Version": "0" * 16}), metadata)
        == "update_available"
    )


def test_matching_legacy_stamp_retains_existing_read_behavior(case):  # noqa: F811
    path = case.root / "installation.json"
    stamp = json.loads(path.read_text())
    for field in ["package_version", "package_format", "deployment_origin"]:
        stamp.pop(field)
    path.write_text(json.dumps(stamp))
    case.client.context()
    assert not case.client.needs_update
    assert case.client.package_version is None


def test_changed_response_package_is_not_ignored_after_discovery(case):  # noqa: F811
    case.client.context()
    assert not case.client.needs_update
    assert case.client._version_differs(case.client.version, "0" * 16)
    assert case.client._version_differs("0" * 16, None)  # undeclared response remains conservative


def test_new_response_revision_blocks_before_write_even_after_matching_manifest(case):  # noqa: F811
    case.state.version_header = case.state.manifest["version"]
    case.state.package_header = "0" * 16
    with pytest.raises(helper.ClientError, match="update_bundle_before_mutation"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)


def test_legacy_response_version_is_not_replaced_with_server_revision(case):  # noqa: F811
    case.state.version_header = "0" * 16
    with pytest.raises(helper.ClientError, match="update_bundle_before_mutation"):
        case.client.command("lock", ["node-a"], [], "2m")
    assert not commands(case)
