"""Opt-in read-only reviewer for Agent evaluation snapshots."""

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
    def snapshot(raw: bool = False):
        filename, max_bytes = ("raw.json", 32 * 1024 * 1024) if raw else ("latest.json", 1024 * 1024)
        try:
            with (directory / filename).open("rb") as source:
                data = source.read(max_bytes + 1)
        except FileNotFoundError:
            raise HTTPException(status_code=404, headers=PRIVATE_HEADERS) from None
        if len(data) > max_bytes:
            raise HTTPException(status_code=503, headers=PRIVATE_HEADERS)
        return Response(data, media_type="application/json", headers=PRIVATE_HEADERS)

    return router
