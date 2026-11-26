"""Optional Feishu-only deployment proxy; never change process-wide HTTP defaults."""

import os
import threading
import time
from urllib.parse import urlsplit

import requests

_lock = threading.RLock()
_mode = "deployment"
_configured = ""
_preferred = None
_cooldowns = {}
TRANSPORT_ERRORS = (requests.ConnectionError, requests.Timeout)
RETRY_PROXY_EXIT = 75


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


def validate_proxy_urls(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError("Invalid Feishu proxy list")
    urls = tuple(
        dict.fromkeys(
            validate_proxy_url(part.strip(" \t\r"))
            for part in value.replace(",", "\n").split("\n")
            if part.strip(" \t\r")
        )
    )
    if len(urls) > 8:
        raise ValueError("At most eight Feishu proxies are supported")
    return urls


def configure(mode="deployment", value=""):
    if mode not in {"deployment", "direct", "custom"}:
        raise ValueError("Invalid Feishu proxy mode")
    normalized = "\n".join(validate_proxy_urls(value))
    if mode == "custom" and not normalized:
        raise ValueError("A Feishu proxy URL is required")
    global _mode, _configured, _preferred
    with _lock:
        if (_mode, _configured) != (mode, normalized):
            _cooldowns.clear()
            _preferred = None
        _mode, _configured = mode, normalized


def proxy_policy():
    with _lock:
        mode, configured = _mode, _configured
    if mode == "deployment":
        configured = os.environ.get("LOCKBOT_FEISHU_PROXY_URLS")
        if not configured:
            single = validate_proxy_url(os.environ.get("LOCKBOT_FEISHU_PROXY_URL", ""))
            return mode, (single,) if single else ()
    return mode, validate_proxy_urls(configured) if mode != "direct" else ()


def _ordered_proxies(urls):
    with _lock:
        ordered = (_preferred, *(url for url in urls if url != _preferred)) if _preferred in urls else urls
        now = time.monotonic()
        return tuple(url for url in ordered if _cooldowns.get(url, 0) <= now)


def proxy_state():
    mode, urls = proxy_policy()
    effective = next(iter(_ordered_proxies(urls) or urls), "")
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


def request(method, url, *, retry, **options):
    """Keep working proxies sticky and retry only operations safe to repeat."""
    global _preferred
    policy = proxy_policy()
    _, urls = policy
    if len(urls) <= 1:
        return method(url, **options, **http_options())
    options["timeout"] = tuple(min(value, limit) for value, limit in zip(options["timeout"], (2, 4), strict=True))
    last_error = None
    for proxy in _ordered_proxies(urls)[: 3 if retry else 1]:
        try:
            response = method(url, **options, proxies={"http": proxy, "https": proxy})
        except TRANSPORT_ERRORS as error:
            with _lock:
                if proxy_policy() == policy:
                    _cooldowns[proxy] = time.monotonic() + 15
                    if _preferred == proxy:
                        _preferred = None
            last_error = error
            if not retry:
                raise
        else:
            with _lock:
                if proxy_policy() == policy:
                    _cooldowns.pop(proxy, None)
                    _preferred = proxy
            return response
    if last_error is not None:
        raise last_error
    raise requests.exceptions.ProxyError("No Feishu proxy is currently available")
