"""Optional Feishu-only deployment proxy; never change process-wide HTTP defaults."""

import os
from urllib.parse import urlsplit


def proxy_url():
    value = os.environ.get("LOCKBOT_FEISHU_PROXY_URL", "")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("Invalid Feishu proxy URL; use an HTTP(S) address without credentials or a path") from None
    return value.rstrip("/")


def http_options():
    proxy = proxy_url()
    return {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
