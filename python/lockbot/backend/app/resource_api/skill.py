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

from lockbot.backend.app.agent_skill.scripts.lockbot_resource import deployment_fragments
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter

router = APIRouter(prefix="/api/agent-skill", tags=["Public API"])


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


def skill_artifacts(kind="agent"):
    return _skill_artifacts(kind, _deployment_origin() if kind == "agent" else "")


@lru_cache(maxsize=4)
def _skill_artifacts(kind, origin):
    folder, name, prefix = {
        "agent": ("agent_skill", "lockbot-agent-api", "/api/agent-skill"),
        "operator": ("op_skill", "lockbot-operator-api", "/api/op-skill"),
    }[kind]
    markdown = files("lockbot.backend.app").joinpath(f"{folder}/SKILL.md").read_bytes()
    canonical_markdown = markdown
    if origin:
        for marker, rendered in deployment_fragments(origin).items():
            markdown = markdown.replace(marker.encode(), rendered.encode(), 1)
    extras = {}
    if kind == "agent":
        extras["scripts/lockbot_resource.py"] = (
            files("lockbot.backend.app").joinpath("agent_skill/scripts/lockbot_resource.py").read_bytes()
        )
    version = hashlib.sha256(markdown + (b"\0" + extras["scripts/lockbot_resource.py"] if extras else b"")).hexdigest()[
        :16
    ]
    package = {}
    if extras:
        package = {
            "package_format": "canonical-origin-v1",
            "package_version": hashlib.sha256(
                canonical_markdown + b"\0" + extras["scripts/lockbot_resource.py"]
            ).hexdigest()[:16],
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
    bundle = _zip_skill(markdown, name, extras)
    skill_id = {"agent": "lockbot-resource-agent", "operator": "lockbot-operator-agent"}[kind]
    installation = _zip_skill(markdown, skill_id, extras)
    return (
        markdown,
        bundle,
        {
            "name": name,
            "skill_id": skill_id,
            "installation": {
                "name": skill_id,
                "download_url": f"{prefix}/{skill_id}.zip",
                "sha256": hashlib.sha256(installation).hexdigest(),
            },
            "version": version,
            **package,
            "skill_url": f"{prefix}/SKILL.md",
            "download_url": f"{prefix}/{name}.zip",
            "skill_sha256": hashlib.sha256(markdown).hexdigest(),
            "sha256": hashlib.sha256(bundle).hexdigest(),
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
        "skill_url": "/api/agent-skill/SKILL.md",
        "instructions": None
        if status == "current" or package_status == "current"
        else (
            "Before the next resource mutation, check the manifest on this same approved origin, "
            "load the latest Skill, and report its version in X-Lockbot-Client-Skill-Version. "
            "Update an installed copy only within the user's authorized setup. Do not rebind or retry the last command."
        ),
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


def public_skill_manifest(kind, request, response, db):
    from lockbot.backend.app.resource_api.discovery import server_metadata

    response.headers["Cache-Control"] = "no-store"
    identity = server_metadata(request, db)
    if kind == "operator":
        identity["credential_scheme"] = "lbk_bot_scoped"
        identity["capabilities"].remove("credential_context")
        identity["capabilities"].remove("resource_view_link")
        identity["capabilities"].extend(
            ["public_agent_operation_history", "allocator_lease_history", "allocator_lifecycle_audit_v2"]
        )
        identity["lifecycle_audit_path_template"] = "/api/bots/{bot_id}/op/lifecycle-audit"
        identity["op_history_path_template"] = "/api/bots/{bot_id}/op/history"
        identity.pop("context_url")
        identity.pop("enrollment_url")
    return {**skill_artifacts(kind)[2], **identity}


def installation_bundle(kind):
    markdown, bundle, meta = skill_artifacts(kind)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        prefix = meta["name"] + "/"
        extras = {path[len(prefix) :]: archive.read(path) for path in archive.namelist() if path != prefix + "SKILL.md"}
    return _zip_skill(markdown, meta["skill_id"], extras)


@router.get("")
@limiter.limit("60/minute")
def skill_manifest(request: Request, response: Response, db: Session = Depends(get_db)):
    """Discover the skill version, relative URLs and ZIP SHA-256; no API key required."""
    return public_skill_manifest("agent", request, response, db)


def _download(request, content, media_type, filename=None):
    etag = '"' + hashlib.sha256(content).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"}
    if etag in request.headers.get("if-none-match", "").split(", "):
        return Response(status_code=304, headers=headers)
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/SKILL.md", response_class=Response, responses={200: {"content": {"text/markdown": {}}}})
@limiter.limit("60/minute")
def skill_markdown(request: Request):
    """Read the same instructions contained in the downloadable ZIP."""
    return _download(request, skill_artifacts()[0], "text/markdown")


@router.get("/lockbot-agent-api.zip", response_class=Response, responses={200: {"content": {"application/zip": {}}}})
@limiter.limit("60/minute")
def skill_zip(request: Request):
    """Download a versioned, deterministic skill bundle without credentials or runtime data."""
    _, bundle, manifest = skill_artifacts()
    return _download(request, bundle, "application/zip", f"lockbot-agent-api-{manifest['version']}.zip")


@router.get("/lockbot-resource-agent.zip", response_class=Response)
@limiter.limit("60/minute")
def namespaced_skill_zip(request: Request):
    """Install alongside other Lockbot implementations without replacing their skill."""
    return _download(request, installation_bundle("agent"), "application/zip", "lockbot-resource-agent.zip")
