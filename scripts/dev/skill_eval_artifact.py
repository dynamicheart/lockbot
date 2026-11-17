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
        for name in ("manifest.json", "lockbot-resource-agent.zip"):
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
                raise ArtifactBlocked("complete_artifact_required")
            raw[name] = path.read_bytes()
        meta = json.loads(raw["manifest.json"])
        descriptor = meta["installation"]
        if meta["skill_id"] != "lockbot-resource-agent" or descriptor["name"] != "lockbot-resource-agent":
            raise ArtifactBlocked("unexpected_package_identity")
        if descriptor["download_url"] != "/api/agent-skill/lockbot-resource-agent.zip":
            raise ArtifactBlocked("unexpected_download_path")
        if digest(raw["lockbot-resource-agent.zip"]) != descriptor["sha256"]:
            raise ArtifactBlocked("artifact_digest_mismatch")
        prefix = descriptor["name"] + "/"
        reference_paths = REFERENCE_PATHS if meta.get("package_format") == BUNDLE_FORMAT else ()
        expected = {*FILES, *reference_paths}
        with zipfile.ZipFile(io.BytesIO(raw["lockbot-resource-agent.zip"])) as archive:
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
        return {"manifest": meta, "archive": raw["lockbot-resource-agent.zip"], "contents": contents}
    except ArtifactBlocked:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, zipfile.BadZipFile) as error:
        raise ArtifactBlocked("complete_artifact_invalid_or_missing") from error
