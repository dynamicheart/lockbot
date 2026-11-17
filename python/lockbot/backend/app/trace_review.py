"""Opt-in read-only reviewer for sanitized Agent evaluation snapshots."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from lockbot.backend.app.auth.dependencies import require_super_admin

PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def trace_review_router(directory: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/dev/trace-review")
    def viewer():
        page = directory / "viewer.html"
        if not page.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(page, media_type="text/html", headers=PRIVATE_HEADERS)

    @router.get("/api/dev/trace-review/snapshot", dependencies=[Depends(require_super_admin)])
    def snapshot():
        try:
            with (directory / "latest.json").open("rb") as source:
                data = source.read(1_048_577)
        except FileNotFoundError:
            raise HTTPException(status_code=404, headers=PRIVATE_HEADERS) from None
        if len(data) > 1_048_576:
            raise HTTPException(status_code=503, headers=PRIVATE_HEADERS)
        return Response(data, media_type="application/json", headers=PRIVATE_HEADERS)

    return router
