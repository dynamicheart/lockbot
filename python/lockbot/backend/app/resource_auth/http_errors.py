"""Add machine-readable auth diagnostics without changing published 401 detail strings."""

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class ResourceAuthenticationError(HTTPException):
    def __init__(self, message: str, code: str):
        super().__init__(status_code=401, detail=message)
        self.code = code


def install_resource_auth_errors(app):
    @app.exception_handler(ResourceAuthenticationError)
    async def authentication_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "context_url": "/api/agent/context"},
            headers={"Cache-Control": "no-store", "X-Lockbot-Error-Code": exc.code},
        )
