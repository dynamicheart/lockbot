"""Adapt the pinned SDK inside its dedicated child process only."""

import inspect
from functools import wraps

from lockbot.core.platforms.feishu_proxy import proxy_url


def configure_sdk_proxy(sdk_module, *, connected=None, proxy_override=None):
    proxy = proxy_url() if proxy_override is None else proxy_override
    explicit = bool(proxy) or proxy_override is not None
    if not explicit and connected is None:
        return
    connect = sdk_module.websockets.connect
    if proxy and "proxy" not in inspect.signature(connect).parameters:
        raise RuntimeError("Feishu proxy requires websockets 15 or newer")
    post = sdk_module.requests.post

    @wraps(connect)
    async def connect_via_proxy(uri, **kwargs):
        # The SDK may explicitly pass proxy=None, disabling environment discovery.
        if explicit:
            kwargs["proxy"] = proxy or None
        connection = await connect(uri, **kwargs)
        if connected is not None:
            connected()
        return connection

    @wraps(post)
    def post_via_proxy(*args, **kwargs):
        kwargs["proxies"] = {"http": proxy, "https": proxy}
        kwargs.setdefault("timeout", (3, 10))
        kwargs["allow_redirects"] = False
        return post(*args, **kwargs)

    sdk_module.websockets.connect = connect_via_proxy
    if explicit:
        sdk_module.requests.post = post_via_proxy
