"""Additive response metadata: old API bodies and request requirements stay intact."""

import re

from starlette.datastructures import Headers

from lockbot.backend.app.resource_api.discovery import API_VERSION, SERVICE_ID
from lockbot.backend.app.resource_api.ingress import _PATH
from lockbot.backend.app.resource_api.skill import skill_artifacts


class AgentVersionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        applies = scope["type"] == "http" and (
            _PATH.fullmatch(path) or path.startswith(("/api/agent-skill", "/api/op-skill"))
        )
        if not applies:
            return await self.app(scope, receive, send)

        async def send_metadata(message):
            if message["type"] == "http.response.start":
                kind = "operator" if path.startswith("/api/op-skill") else "agent"
                manifest = skill_artifacts(kind)[2]
                reported = Headers(scope=scope)
                revision = reported.get("x-lockbot-client-skill-version", "")
                api_version = reported.get("x-lockbot-client-api-version", "")
                skill_status = (
                    "unknown"
                    if not re.fullmatch(r"[a-f0-9]{16}", revision)
                    else "current"
                    if revision == manifest["version"]
                    else "update_available"
                )
                api_status = (
                    "unknown"
                    if not re.fullmatch(r"[0-9]{1,3}", api_version)
                    else "current"
                    if api_version == API_VERSION
                    else "incompatible"
                )
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-lockbot-service-id", SERVICE_ID.encode()),
                        (b"x-lockbot-api-version", API_VERSION.encode()),
                        (b"x-lockbot-server-version", scope["app"].version.encode()),
                        (b"x-lockbot-skill-version", manifest["version"].encode()),
                        (b"x-lockbot-skill-update", skill_status.encode()),
                        (b"x-lockbot-api-compatibility", api_status.encode()),
                        (b"x-lockbot-skill-url", manifest["skill_url"].encode()),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_metadata)
