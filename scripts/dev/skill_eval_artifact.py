"""Validate an explicitly supplied, complete resource Skill artifact without execution."""

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

from lockbot.backend.app.agent_skill.scripts.lockbot_cli import BUNDLE_FORMAT, REFERENCE_PATHS, content_version

FILES = {"SKILL.md", "scripts/lockbot_cli.py", "installation.json"}
LIMIT = 1024 * 1024


class ArtifactBlocked(ValueError):
    pass


def digest(content):
    return hashlib.sha256(content).hexdigest()


def load(directory):
    """Missing helper/stamp is blocked, never downgraded to a Markdown-only pass."""
    try:
        directory = Path(directory)
        raw = {}
        for name in ("manifest.json", "lockbot-agent.zip"):
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
                raise ArtifactBlocked("complete_artifact_required")
            raw[name] = path.read_bytes()
        meta = json.loads(raw["manifest.json"])
        descriptor = meta["installation"]
        if meta["skill_id"] != "lockbot-agent" or descriptor["name"] != "lockbot-agent":
            raise ArtifactBlocked("unexpected_package_identity")
        if descriptor["download_url"] != "/api/agent-skill/lockbot-agent.zip":
            raise ArtifactBlocked("unexpected_download_path")
        if digest(raw["lockbot-agent.zip"]) != descriptor["sha256"]:
            raise ArtifactBlocked("artifact_digest_mismatch")
        prefix = descriptor["name"] + "/"
        reference_paths = REFERENCE_PATHS if meta.get("package_format") == BUNDLE_FORMAT else ()
        expected = {*FILES, *reference_paths}
        with zipfile.ZipFile(io.BytesIO(raw["lockbot-agent.zip"])) as archive:
            entries = archive.infolist()
            if len(entries) != len(expected) or {entry.filename for entry in entries} != {
                prefix + item for item in expected
            }:
                raise ArtifactBlocked("complete_helper_bundle_required")
            if sum(entry.file_size for entry in entries) > LIMIT or any(
                stat.S_ISLNK(entry.external_attr >> 16) for entry in entries
            ):
                raise ArtifactBlocked("unsafe_bundle")
            contents = {name: archive.read(prefix + name) for name in expected}
        stamp = json.loads(contents["installation.json"])
        if digest(contents["SKILL.md"]) != meta["skill_sha256"] or not contents["scripts/lockbot_cli.py"]:
            raise ArtifactBlocked("artifact_markdown_or_helper_mismatch")
        legacy_snapshot = directory / "SKILL.md"
        if legacy_snapshot.is_symlink() or (
            legacy_snapshot.exists()
            and (legacy_snapshot.stat().st_size > LIMIT or legacy_snapshot.read_bytes() != contents["SKILL.md"])
        ):
            raise ArtifactBlocked("artifact_markdown_or_helper_mismatch")
        if stamp["service_id"] != "lockbot.resource-api" or stamp["api_version"] != "1":
            raise ArtifactBlocked("unsupported_bundle_protocol")
        revision = content_version(
            contents["SKILL.md"],
            contents["scripts/lockbot_cli.py"],
            {name: contents[name] for name in reference_paths},
        )
        if stamp["version"] != meta["version"] or meta["version"] != revision:
            raise ArtifactBlocked("artifact_revision_mismatch")
        if stamp["files"] != {name: digest(contents[name]) for name in expected - {"installation.json"}}:
            raise ArtifactBlocked("artifact_file_digest_mismatch")
        return {"manifest": meta, "archive": raw["lockbot-agent.zip"], "contents": contents}
    except ArtifactBlocked:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, zipfile.BadZipFile) as error:
        raise ArtifactBlocked("complete_artifact_invalid_or_missing") from error


def installed_service_origin(directory, artifact, origin):
    """A complete verified deployment package retains its approved service origin."""
    if artifact is None:
        return False
    try:
        stamp = json.loads(artifact["contents"]["installation.json"])
        return stamp.get("deployment_origin") == origin and all(
            not (directory / name).is_symlink() and (directory / name).read_bytes() == content
            for name, content in artifact["contents"].items()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def installed_skill_catalog(directory, artifact):
    """Expose only public routing metadata from an intact, verified installation."""
    try:
        origin = json.loads(artifact["contents"]["installation.json"])["deployment_origin"]
        if not installed_service_origin(directory, artifact, origin):
            raise ArtifactBlocked("verified_installation_required_for_catalog")
        text = artifact["contents"]["SKILL.md"].decode("utf-8")
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            raise ArtifactBlocked("skill_catalog_metadata_missing")
        frontmatter = lines[1 : lines.index("---", 1)]
        fields = {}
        for key in ("name", "description"):
            starts = [i for i, line in enumerate(frontmatter) if line.startswith(key + ":")]
            if len(starts) != 1:
                raise ArtifactBlocked("skill_catalog_metadata_missing")
            start = starts[0]
            end = start + 1
            while end < len(frontmatter) and frontmatter[end].startswith((" ", "\t")):
                end += 1
            block = frontmatter[start:end]
            value = block[0].split(":", 1)[1].strip()
            if not value or (value in {">", ">-", ">+", "|", "|-", "|+"} and not any(block[1:])):
                raise ArtifactBlocked("skill_catalog_metadata_missing")
            fields[key] = "\n".join(block)
        return (
            "\n\nAvailable installed Skills (verified package metadata):\n"
            + fields["name"]
            + "\n"
            + fields["description"]
            + "\nInstructions: read_lockbot_skill(offset=0).\n"
            "Skill use: If an available Skill clearly applies to the user's task, read its instructions "
            "before using its workflow. Read linked references only when needed."
        )
    except ArtifactBlocked:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ArtifactBlocked("verified_installation_required_for_catalog") from error
