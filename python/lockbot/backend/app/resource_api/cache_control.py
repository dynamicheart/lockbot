"""Do not let HTTP caches retain authenticated resource snapshots or failures."""

import re

from starlette.datastructures import MutableHeaders

_RESOURCE_PATH = re.compile(r"^/api/bots/[^/]+/(?:op/)?resources/?$")


class ResourceSnapshotCacheMiddleware:
    """Cover dependency, validation and rate-limit errors as well as successful reads.

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
