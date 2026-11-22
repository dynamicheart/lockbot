"""Publish the service-owned Agent API v2 Skill."""

from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

from fastapi import Response

from lockbot.backend.app.resource_api.skill import _deployment_origin

SKILL_NAME = "lockbot"
MANIFEST_URL = "/api/agent/v2/manifest"
SKILL_PATH = "/skill/SKILL.md"
SKILL_URL = f"/api/agent/v2{SKILL_PATH}"


class SkillMarkdown(Response):
    media_type = "text/markdown"


@lru_cache(maxsize=4)
def _artifact(origin: str) -> tuple[bytes, str]:
    template = files("lockbot.backend.app").joinpath("agent_skill/v2.md").read_text(encoding="utf-8")
    connection = "Ask the user for the LockBot service address before signing in."
    if origin:
        connection = (
            f"Service address: `{origin}`. If the user approves this service and it is not saved in "
            f"`lb connections`, run `lb login {origin}`. "
            f"Select this service explicitly with `lb --origin {origin} resource list`."
        )
    content = template.replace("{{LOCKBOT_CONNECTION}}", connection, 1).encode()
    return content, sha256(content).hexdigest()


def skill_release() -> dict[str, str]:
    return {"name": SKILL_NAME, "download_url": SKILL_URL, "sha256": _artifact(_deployment_origin())[1]}


def skill_metadata() -> dict[str, str]:
    release = skill_release()
    revision = release["sha256"][:16]
    return {**release, "version": revision, "package_version": revision, "skill_url": SKILL_URL}


def download_skill() -> SkillMarkdown:
    content, digest = _artifact(_deployment_origin())
    return SkillMarkdown(content, headers={"ETag": f'"{digest}"', "Cache-Control": "no-cache"})
