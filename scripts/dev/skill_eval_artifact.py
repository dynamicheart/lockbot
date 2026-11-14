"""Validate an explicitly supplied, complete resource Skill artifact without execution."""

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

FILES = {"SKILL.md", "scripts/lockbot_resource.py", "installation.json"}
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
        for name in ("manifest.json", "SKILL.md", "lockbot-resource-agent.zip"):
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
        if (
            digest(raw["lockbot-resource-agent.zip"]) != descriptor["sha256"]
            or digest(raw["SKILL.md"]) != meta["skill_sha256"]
        ):
            raise ArtifactBlocked("artifact_digest_mismatch")
        prefix = descriptor["name"] + "/"
        with zipfile.ZipFile(io.BytesIO(raw["lockbot-resource-agent.zip"])) as archive:
            entries = archive.infolist()
            if len(entries) != 3 or {entry.filename for entry in entries} != {prefix + item for item in FILES}:
                raise ArtifactBlocked("complete_helper_bundle_required")
            if sum(entry.file_size for entry in entries) > LIMIT or any(
                stat.S_ISLNK(entry.external_attr >> 16) for entry in entries
            ):
                raise ArtifactBlocked("unsafe_bundle")
            contents = {name: archive.read(prefix + name) for name in FILES}
        stamp = json.loads(contents["installation.json"])
        if contents["SKILL.md"] != raw["SKILL.md"] or not contents["scripts/lockbot_resource.py"]:
            raise ArtifactBlocked("artifact_markdown_or_helper_mismatch")
        if stamp["service_id"] != "lockbot.resource-api" or stamp["api_version"] != "1":
            raise ArtifactBlocked("unsupported_bundle_protocol")
        revision = digest(contents["SKILL.md"] + b"\0" + contents["scripts/lockbot_resource.py"])[:16]
        if stamp["version"] != meta["version"] or meta["version"] != revision:
            raise ArtifactBlocked("artifact_revision_mismatch")
        if stamp["files"] != {name: digest(contents[name]) for name in FILES - {"installation.json"}}:
            raise ArtifactBlocked("artifact_file_digest_mismatch")
        return {"manifest": meta, "archive": raw["lockbot-resource-agent.zip"], "contents": contents}
    except ArtifactBlocked:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, zipfile.BadZipFile) as error:
        raise ArtifactBlocked("complete_artifact_invalid_or_missing") from error
