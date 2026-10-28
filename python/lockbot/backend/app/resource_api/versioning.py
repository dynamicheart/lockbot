"""Additive response metadata: old API bodies and request requirements stay intact."""

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
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-lockbot-service-id", SERVICE_ID.encode()),
                        (b"x-lockbot-api-version", API_VERSION.encode()),
                        (b"x-lockbot-server-version", scope["app"].version.encode()),
                        (b"x-lockbot-skill-version", manifest["version"].encode()),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_metadata)
