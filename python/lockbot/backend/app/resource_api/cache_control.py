"""Do not let HTTP caches retain resource observations, including command results."""

import re

from starlette.datastructures import MutableHeaders

_RESOURCE_PATH = re.compile(
    r"^/api/(?:agent/(?:resources(?:/query)?|commands)|bots/[^/]+/(?:op/)?(?:resources|commands))/?$"
)


class ResourceSnapshotCacheMiddleware:
    """Cover errors and successful reads or command responses containing observations.

    The internal snapshot cache remains unchanged; it is scoped by bot generation
    and does not replace authentication or HTTP cache isolation.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _RESOURCE_PATH.fullmatch(scope.get("path", "")):
            return await self.app(scope, receive, send)

        async def send_private(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_private)
