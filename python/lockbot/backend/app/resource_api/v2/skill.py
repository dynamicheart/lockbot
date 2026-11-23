"""Publish the service-owned Agent API v2 Skill."""

import os
import re
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

from fastapi import Response

from lockbot.backend.app.resource_api.skill import _deployment_origin, _zip_skill

SKILL_NAME = "lockbot"
# Bump when releasing changes to the Skill instructions or bundled metadata.
SKILL_VERSION = "1.0.0"
MANIFEST_URL = "/api/agent/v2/manifest"
SKILL_PATH = "/skill/lockbot.zip"
SKILL_URL = f"/api/agent/v2{SKILL_PATH}"


class SkillZip(Response):
    media_type = "application/zip"


@lru_cache(maxsize=4)
def _artifact(origin: str, release_manifest_url: str) -> tuple[bytes, str]:
    source = files("lockbot.backend.app").joinpath("agent_skill")
    template = source.joinpath("v2.md").read_text(encoding="utf-8")
    connection = "Ask the user for the LockBot service address before signing in."
    if origin:
        connection = (
            f"Service address: `{origin}`. For a first connection, run `lb login {origin}` "
            "only after the user explicitly authorizes binding to this service. "
            f"Select this service explicitly with `lb --origin {origin} resource list`."
        )
    install_source = "Ask the user for an approved internal lb release manifest URL before downloading."
    if release_manifest_url:
        install_source = f"Internal lb release manifest: `{release_manifest_url}`."
    markdown = (
        template.replace("{{LOCKBOT_CONNECTION}}", connection, 1)
        .replace("{{LOCKBOT_LB_INSTALL_SOURCE}}", install_source, 1)
        .replace("{{LOCKBOT_SKILL_VERSION}}", SKILL_VERSION, 1)
        .encode()
    )
    metadata = source.joinpath("agents/openai.yaml").read_bytes()
    content = _zip_skill(markdown, SKILL_NAME, {"agents/openai.yaml": metadata})
    return content, sha256(content).hexdigest()


def _configured_artifact() -> tuple[bytes, str]:
    url = os.environ.get("LB_RELEASE_MANIFEST_URL", "").strip()
    # The unauthenticated ZIP must never contain userinfo or a signed query.
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/-]+", url):
        url = ""
    return _artifact(_deployment_origin(), url)


def skill_release() -> dict[str, str]:
    return {
        "name": SKILL_NAME,
        "version": SKILL_VERSION,
        "download_url": SKILL_URL,
        "sha256": _configured_artifact()[1],
    }


def skill_metadata() -> dict[str, str]:
    release = skill_release()
    revision = release["sha256"][:16]
    # Existing Agent API update headers identify the exact rendered package.
    return {
        **release,
        "release_version": release["version"],
        "version": revision,
        "package_version": revision,
        "skill_url": SKILL_URL,
    }


def download_skill() -> SkillZip:
    content, digest = _configured_artifact()
    return SkillZip(
        content,
        headers={
            "ETag": f'"{digest}"',
            "Cache-Control": "no-cache",
            "Content-Disposition": 'attachment; filename="lockbot.zip"',
        },
    )
