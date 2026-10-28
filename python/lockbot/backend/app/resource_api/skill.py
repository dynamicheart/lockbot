"""Public, credential-free distribution of the bundled Agent skill."""

import hashlib
import io
import zipfile
from functools import lru_cache
from importlib.resources import files

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter

router = APIRouter(prefix="/api/agent-skill", tags=["Public API"])


@lru_cache(maxsize=2)
def skill_artifacts(kind="agent"):
    folder, name, prefix = {
        "agent": ("agent_skill", "lockbot-agent-api", "/api/agent-skill"),
        "operator": ("op_skill", "lockbot-operator-api", "/api/op-skill"),
    }[kind]
    markdown = files("lockbot.backend.app").joinpath(f"{folder}/SKILL.md").read_bytes()
    version = hashlib.sha256(markdown).hexdigest()[:16]
    bundle = _zip_skill(markdown, name)
    skill_id = {"agent": "lockbot-resource-agent", "operator": "lockbot-operator-agent"}[kind]
    installation = _zip_skill(markdown, skill_id)
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
            "skill_url": f"{prefix}/SKILL.md",
            "download_url": f"{prefix}/{name}.zip",
            "skill_sha256": hashlib.sha256(markdown).hexdigest(),
            "sha256": hashlib.sha256(bundle).hexdigest(),
            "size_bytes": len(bundle),
            "issues_url": "https://github.com/dynamicheart/lockbot/issues",
        },
    )


def _zip_skill(markdown, name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo(f"{name}/SKILL.md", date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, markdown)
    return output.getvalue()


def public_skill_manifest(kind, request, response, db):
    from lockbot.backend.app.resource_api.discovery import server_metadata

    response.headers["Cache-Control"] = "no-store"
    identity = server_metadata(request, db)
    if kind == "operator":
        identity["credential_scheme"] = "lbk_bot_scoped"
        identity["capabilities"].remove("credential_context")
        identity["capabilities"].remove("resource_view_link")
        identity.pop("context_url")
        identity.pop("enrollment_url")
    return {**skill_artifacts(kind)[2], **identity}


def installation_bundle(kind):
    markdown, _, meta = skill_artifacts(kind)
    return _zip_skill(markdown, meta["skill_id"])


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
