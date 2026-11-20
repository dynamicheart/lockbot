"""Public, credential-free distribution of the bundled Agent skill."""

import hashlib
import io
import json
import os
import re
import zipfile
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from lockbot.backend.app.agent_skill.scripts.lockbot_cli import (
    BUNDLE_FORMAT,
    REFERENCE_PATHS,
    SCRIPT_PATH,
    content_version,
    deployment_fragments,
)
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter

router = APIRouter(prefix="/api/agent-skill", tags=["Public API"])
# Every packaged-CLI invocation checks this cached public identity before sending
# a Key. Allow shared NATs to carry many independent credential budgets; keep a
# finite anonymous bound and a separate, lower allowance for archive downloads.
PUBLIC_MANIFEST_RATE_LIMIT = "6000/minute"
INSTALLATION_INSTRUCTIONS = (
    "A shared manifest or ZIP link is not an installation request. If the user only shared a link, "
    "explain what the Skill does and wait for an explicit request before downloading the ZIP, "
    "writing files, or binding an account. For an authorized installation, download the same-origin ZIP once "
    "to a writable location and verify it against installation.sha256. Install in the client's supported "
    "user-level Skill directory by default; use project scope only when the user explicitly requests it. Stop if the "
    "checksum does not match. Reuse the verified local ZIP for inspection, the client's installer and any "
    "manual installation step instead of fetching it again. "
    "Verify the installed files and whether the client loads the Skill. If the supported location is protected, "
    "report the installation as blocked and give the client-specific manual step. An unpacked copy elsewhere "
    "does not count as installed. If the user also authorized saving the service, run the verified bundled CLI "
    "with Python 3.10+ using discover --origin <approved-service> --destination <new-private-file>. "
    "If that command cannot complete, report that the service was not saved instead of writing a substitute file."
)
SKILL_UPDATE_INSTRUCTIONS = (
    "Verify any prior command with an authenticated resource read. Do not replay the command. "
    "On the same approved origin, fetch manifest_url and its installation.download_url without credentials. "
    "Compare the ZIP with installation.sha256, then read the complete SKILL.md from the verified archive. "
    "If you loaded it for this task, send its version on the next authenticated read "
    "in X-Lockbot-Client-Skill-Version. "
    "Keep the installed Skill, connections and Keys intact while awaiting approval. "
    "When installation is authorized, install the verified ZIP using the client installer, reload it and keep "
    "existing connections. Never rebind for an update. "
    "Use the updated Skill before sending another resource mutation."
)


def _deployment_origin():
    value = os.environ.get("PLATFORM_URL", "").strip()
    if not re.fullmatch(r"https?://[A-Za-z0-9.:[\]-]+/?", value):
        return ""
    try:
        url = urlsplit(value)
        if not url.hostname or (url.port is not None and url.port == 0):
            return ""
    except ValueError:
        return ""
    return f"{url.scheme}://{url.netloc}"


def skill_artifacts():
    return _skill_artifacts(_deployment_origin())


@lru_cache(maxsize=4)
def _skill_artifacts(origin):
    folder = "agent_skill"
    skill_id = "lockbot-agent"
    download_url = f"/api/agent-skill/{skill_id}.zip"
    markdown = files("lockbot.backend.app").joinpath(f"{folder}/SKILL.md").read_bytes()
    canonical_markdown = markdown
    if origin:
        for marker, rendered in deployment_fragments(origin).items():
            markdown = markdown.replace(marker.encode(), rendered.encode(), 1)
    resources = files("lockbot.backend.app")
    script = resources.joinpath(f"{folder}/{SCRIPT_PATH}").read_bytes()
    references = {path: resources.joinpath(f"{folder}/{path}").read_bytes() for path in REFERENCE_PATHS}
    extras = {SCRIPT_PATH: script, **references}
    version = content_version(markdown, script, references)
    package = {
        "package_format": BUNDLE_FORMAT,
        "package_version": content_version(canonical_markdown, script, references),
    }
    stamp = {
        **package,
        "deployment_origin": origin,
        "service_id": "lockbot.resource-api",
        "api_version": "1",
        "version": version,
        "files": {
            path: hashlib.sha256(content).hexdigest() for path, content in {"SKILL.md": markdown, **extras}.items()
        },
    }
    extras["installation.json"] = json.dumps(stamp, sort_keys=True, separators=(",", ":")).encode()
    bundle = _zip_skill(markdown, skill_id, extras)
    bundle_sha256 = hashlib.sha256(bundle).hexdigest()
    return (
        markdown,
        bundle,
        {
            "name": skill_id,
            "skill_id": skill_id,
            "installation": {
                "name": skill_id,
                "download_url": download_url,
                "sha256": bundle_sha256,
                "instructions": INSTALLATION_INSTRUCTIONS,
            },
            "version": version,
            **package,
            "skill_url": download_url,
            "download_url": download_url,
            "skill_sha256": hashlib.sha256(markdown).hexdigest(),
            "sha256": bundle_sha256,
            "size_bytes": len(bundle),
            "issues_url": "https://github.com/dynamicheart/lockbot/issues",
        },
    )


def package_update_status(headers, metadata):
    reported = headers.get("x-lockbot-client-package-version", "")
    if not re.fullmatch(r"[a-f0-9]{16}", reported) or "package_version" not in metadata:
        return "unknown"
    return "current" if reported == metadata["package_version"] else "update_available"


def client_skill_notice(request):
    """A body-level bootstrap for old clients which never inspect response headers.

    An absent/invalid report is unknown, not proof of an outdated installation.
    This is advisory and cannot verify a client's download or loaded instructions.
    """
    metadata = skill_artifacts()[2]
    current = metadata["version"]
    reported = request.headers.get("X-Lockbot-Client-Skill-Version", "")
    if not re.fullmatch(r"[a-f0-9]{16}", reported):
        reported = None
    status = "unknown" if reported is None else "current" if reported == current else "update_available"
    package_status = package_update_status(request.headers, metadata)
    return {
        "status": status,
        "current_version": current,
        "current_package_version": metadata["package_version"],
        "package_status": package_status,
        "reported_version": reported,
        "manifest_url": "/api/agent-skill",
        "skill_url": metadata["skill_url"],
        "instructions": None if status == "current" or package_status == "current" else SKILL_UPDATE_INSTRUCTIONS,
    }


def _zip_skill(markdown, name, extras=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, content in {"SKILL.md": markdown, **(extras or {})}.items():
            entry = zipfile.ZipInfo(f"{name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    return output.getvalue()


def public_skill_manifest(request, response, db):
    from lockbot.backend.app.resource_api.discovery import server_metadata

    response.headers["Cache-Control"] = "no-store"
    return {**skill_artifacts()[2], **server_metadata(request, db)}


def installation_bundle():
    return skill_artifacts()[1]


@router.get("")
@limiter.limit(PUBLIC_MANIFEST_RATE_LIMIT)
def skill_manifest(request: Request, response: Response, db: Session = Depends(get_db)):
    """Discover the skill version, relative URLs and ZIP SHA-256; no API key required."""
    return public_skill_manifest(request, response, db)


def _download(request, content, media_type, filename=None):
    etag = '"' + hashlib.sha256(content).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"}
    if etag in request.headers.get("if-none-match", "").split(", "):
        return Response(status_code=304, headers=headers)
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/lockbot-agent.zip", response_class=Response)
@limiter.limit("60/minute")
def namespaced_skill_zip(request: Request):
    """Install alongside other Lockbot implementations without replacing their skill."""
    return _download(request, installation_bundle(), "application/zip", "lockbot-agent.zip")
