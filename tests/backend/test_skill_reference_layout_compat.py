"""Recognize exactly the two published v2 reference layouts, with intact integrity checks."""

import hashlib
import io
import json
import stat
import zipfile
from copy import deepcopy

import pytest
from lockbot.backend.app.agent_skill.scripts import lockbot_cli as helper

ORIGIN = "https://fixture.example.test"


def archive_bytes(contents, *, duplicate=None, symlink=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            entry = zipfile.ZipInfo("lockbot-agent/" + name)
            if name == symlink:
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, data)
        if duplicate:
            archive.writestr("lockbot-agent/" + duplicate, contents[duplicate])
    return output.getvalue()


def bundle(paths):
    canonical = b"# Synthetic Skill\n\n<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->\n"
    markdown = canonical
    for marker, rendered in helper.deployment_fragments(ORIGIN).items():
        markdown = markdown.replace(marker.encode(), rendered.encode())
    script = b"# Synthetic helper: installation tests never execute it.\n"
    references = {name: ("Reference " + name).encode() for name in paths}
    revision = helper.content_version(markdown, script, references)
    package = helper.content_version(canonical, script, references)
    contents = {"SKILL.md": markdown, helper.SCRIPT_PATH: script, **references}
    stamp = {
        "service_id": helper.SERVICE,
        "api_version": helper.API_VERSION,
        "package_format": helper.BUNDLE_FORMAT,
        "package_version": package,
        "deployment_origin": ORIGIN,
        "version": revision,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    contents["installation.json"] = json.dumps(stamp).encode()
    archive = archive_bytes(contents)
    manifest = {
        "name": "lockbot-agent",
        "version": revision,
        "package_format": helper.BUNDLE_FORMAT,
        "package_version": package,
        "skill_sha256": hashlib.sha256(markdown).hexdigest(),
        "installation": {
            "name": "lockbot-agent",
            "download_url": "/api/agent-skill/lockbot-agent.zip",
            "sha256": hashlib.sha256(archive).hexdigest(),
        },
    }
    return contents, archive, manifest


@pytest.fixture(
    params=[helper.PREVIOUS_V2_REFERENCE_PATHS, helper.REFERENCE_PATHS], ids=["previous-three", "current-four"]
)
def package(request):
    return bundle(request.param)


def installer(monkeypatch, archive, manifest):
    client = helper.Client(approved_origin=ORIGIN)
    requests = []
    monkeypatch.setattr(client, "discover", lambda: deepcopy(manifest))

    def request(url, *, binary=False):
        assert binary is True
        assert url == ORIGIN + manifest["installation"]["download_url"]
        requests.append(url)
        return archive

    monkeypatch.setattr(client, "_request", request)
    return client, requests


def unpack(contents, root):
    for relative, data in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_both_complete_v2_layouts_install_and_verify(package, tmp_path, monkeypatch):
    _, archive, manifest = package
    client, requests = installer(monkeypatch, archive, manifest)
    root = tmp_path / "installed"
    result = client.install(root)
    assert result["client_reload_required"] is True
    assert result["connection_and_enrollment_created"] is False
    assert helper.installed_version(root) == manifest["version"]
    assert helper.installed_package_version(root) == manifest["package_version"]
    assert len(requests) == 1


@pytest.mark.parametrize("tamper", ["reference", "missing", "extra-stamp", "incomplete-stamp", "revision", "package"])
def test_installed_layout_cannot_hide_changed_or_unlisted_content(package, tmp_path, tamper):
    contents, _, manifest = package
    root = tmp_path / "installed"
    unpack(contents, root)
    stamp = json.loads(contents["installation.json"])
    name = helper.PREVIOUS_V2_REFERENCE_PATHS[0]
    if tamper == "reference":
        (root / name).write_bytes(contents[name] + b"changed")
    elif tamper == "missing":
        (root / name).unlink()
    elif tamper == "extra-stamp":
        stamp["files"]["references/unknown.md"] = "0" * 64
    elif tamper == "incomplete-stamp":
        stamp["files"].pop(name)
    elif tamper == "revision":
        stamp["version"] = "0" * 16
    else:
        stamp["package_version"] = "0" * 16
    (root / "installation.json").write_text(json.dumps(stamp))
    with pytest.raises(helper.ClientError):
        helper.installed_package_version(root)
    assert manifest["package_version"] != "0" * 16


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-reference",
        "alternate-reference",
        "extra-reference",
        "zip-stamp-layout",
        "extra-stamp",
        "namespace",
        "duplicate",
        "symlink",
        "oversize",
        "reference-digest",
        "skill-digest",
        "rendered-version",
        "package-version",
        "stamp-format",
        "service",
        "api-version",
        "origin",
        "nonobject-stamp",
    ],
)
def test_install_keeps_full_validation_for_both_v2_layouts(package, tmp_path, monkeypatch, tamper):
    contents, _, manifest = deepcopy(package)
    stamp = json.loads(contents["installation.json"])
    name = helper.PREVIOUS_V2_REFERENCE_PATHS[0]
    duplicate = symlink = None
    if tamper == "missing-reference":
        contents.pop(name)
    elif tamper == "alternate-reference":
        contents["references/alternate.md"] = contents.pop(name)
    elif tamper == "extra-reference":
        contents["references/extra.md"] = b"unlisted"
    elif tamper == "zip-stamp-layout":
        # Even an individually recognized stamp layout must match this exact ZIP.
        query = "references/resource-queries.md"
        if query in stamp["files"]:
            stamp["files"].pop(query)
        else:
            stamp["files"][query] = "0" * 64
    elif tamper == "extra-stamp":
        stamp["files"]["references/extra.md"] = "0" * 64
    elif tamper == "namespace":
        contents["../escape"] = b"not a package member"
    elif tamper == "duplicate":
        duplicate = name
    elif tamper == "symlink":
        symlink = name
    elif tamper == "oversize":
        contents[name] = b"x" * (helper.MAX_BYTES + 1)
    elif tamper == "reference-digest":
        contents[name] += b"changed"
    elif tamper == "skill-digest":
        manifest["skill_sha256"] = "0" * 64
    elif tamper == "rendered-version":
        stamp["version"] = manifest["version"] = "0" * 16
    elif tamper == "package-version":
        stamp["package_version"] = manifest["package_version"] = "0" * 16
    elif tamper == "stamp-format":
        stamp["package_format"] = helper.LEGACY_BUNDLE_FORMAT
    elif tamper == "service":
        stamp["service_id"] = "other.resource-api"
    elif tamper == "api-version":
        stamp["api_version"] = "2"
    elif tamper == "origin":
        stamp["deployment_origin"] = "https://other.example.test"
    else:
        stamp = []
    contents["installation.json"] = json.dumps(stamp).encode()
    archive = archive_bytes(contents, duplicate=duplicate, symlink=symlink)
    # A matching outer ZIP digest must never bypass its internal validation.
    manifest["installation"]["sha256"] = hashlib.sha256(archive).hexdigest()
    client, requests = installer(monkeypatch, archive, manifest)
    root = tmp_path / "rejected"
    with pytest.raises(helper.ClientError):
        client.install(root)
    assert len(requests) == 1
    assert not root.exists()
    assert not list(tmp_path.glob(".lockbot-install-*"))
