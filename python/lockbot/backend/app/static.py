"""Serve current HTML entrypoints; hashed assets keep their normal caching."""

from pathlib import PurePosixPath

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        entrypoint = not PurePosixPath(path).suffix or path.endswith(".html")
        if entrypoint:
            # Release archives can give different index files identical size/mtime.
            # Do not answer a stale index validator with 304 and retain an old JS bundle.
            scope = {
                **scope,
                "headers": [
                    (k, v)
                    for k, v in scope.get("headers", [])
                    if k.lower() not in {b"if-none-match", b"if-modified-since"}
                ],
            }
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            accepts_html = any(
                name.lower() == b"accept" and b"text/html" in value.lower() for name, value in scope.get("headers", [])
            )
            if exc.status_code != 404 or not accepts_html or path.startswith("api/") or PurePosixPath(path).suffix:
                raise
            response = await super().get_response("index.html", scope)
        if entrypoint:
            response.headers["Cache-Control"] = "no-store"
            for key in ["etag", "last-modified"]:
                if key in response.headers:
                    del response.headers[key]
        return response
