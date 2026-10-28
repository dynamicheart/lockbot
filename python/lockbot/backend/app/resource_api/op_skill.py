"""Public instructions for the existing privileged operator API."""

from fastapi import APIRouter, Request, Response

from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.skill import _download, skill_artifacts

router = APIRouter(prefix="/api/op-skill", tags=["Public API"])


@router.get("")
@limiter.limit("60/minute")
def operator_skill_manifest(request: Request):
    return skill_artifacts("operator")[2]


@router.get("/SKILL.md", response_class=Response)
@limiter.limit("60/minute")
def operator_skill_markdown(request: Request):
    return _download(request, skill_artifacts("operator")[0], "text/markdown")


@router.get("/lockbot-operator-api.zip", response_class=Response)
@limiter.limit("60/minute")
def operator_skill_zip(request: Request):
    _, bundle, manifest = skill_artifacts("operator")
    return _download(request, bundle, "application/zip", f"lockbot-operator-api-{manifest['version']}.zip")
