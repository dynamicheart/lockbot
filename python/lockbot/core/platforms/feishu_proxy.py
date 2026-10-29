"""Optional Feishu-only deployment proxy; never change process-wide HTTP defaults."""

import os
import threading
from urllib.parse import urlsplit

_lock = threading.RLock()
_mode = "deployment"
_configured = ""


def validate_proxy_url(value):
    if not isinstance(value, str):
        raise ValueError("Invalid Feishu proxy URL")
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


def configure(mode="deployment", value=""):
    if mode not in {"deployment", "direct", "custom"}:
        raise ValueError("Invalid Feishu proxy mode")
    normalized = validate_proxy_url(value)
    if mode == "custom" and not normalized:
        raise ValueError("A Feishu proxy URL is required")
    global _mode, _configured
    with _lock:
        _mode, _configured = mode, normalized


def proxy_state():
    with _lock:
        mode, configured = _mode, _configured
    effective = (
        validate_proxy_url(os.environ.get("LOCKBOT_FEISHU_PROXY_URL", ""))
        if mode == "deployment"
        else configured
        if mode == "custom"
        else ""
    )
    return {"mode": mode, "source": "deployment" if mode == "deployment" else "site", "effective_url": effective}


def proxy_url():
    return proxy_state()["effective_url"]


def sdk_proxy_override():
    state = proxy_state()
    return state["effective_url"] if state["mode"] != "deployment" or state["effective_url"] else None


def http_options():
    state = proxy_state()
    proxy = state["effective_url"]
    # Empty strings explicitly disable requests' process-environment proxy discovery.
    return {"proxies": {"http": proxy, "https": proxy}} if proxy or state["mode"] != "deployment" else {}
