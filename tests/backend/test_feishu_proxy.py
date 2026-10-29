import asyncio
from types import SimpleNamespace

import pytest
from lockbot.backend.app.feishu.ws_proxy import configure_sdk_proxy
from lockbot.core.platforms.feishu import FeishuClient
from lockbot.core.platforms.feishu_proxy import http_options


def test_feishu_http_proxy_is_explicit_and_preserves_other_transport(monkeypatch):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://proxy.example:8188")
    monkeypatch.setenv("https_proxy", "http://unrelated.example:1234")
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status_code=200, json=lambda: {"code": 0, "tenant_access_token": "token", "expire": 7200}
        )

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.post", post)
    assert FeishuClient("cli_demo", "secret").token() == "token"
    assert calls[0]["proxies"] == {"http": "http://proxy.example:8188", "https": "http://proxy.example:8188"}
    import os

    assert os.environ["https_proxy"] == "http://unrelated.example:1234"


@pytest.mark.parametrize(
    "url",
    [
        "socks5://proxy:80",
        "http://user:secret@proxy",
        "http://proxy/path",
        "http://proxy?token=x",
        "http://proxy:99999",
        "http://proxy\n",
    ],
)
def test_proxy_rejects_unsupported_or_sensitive_urls(monkeypatch, url):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", url)
    with pytest.raises(ValueError, match="Invalid Feishu proxy URL") as error:
        http_options()
    assert "secret" not in str(error.value)


def test_sdk_proxy_covers_endpoint_http_and_websocket_even_if_sdk_disables_proxy(monkeypatch):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://proxy.example:8188")
    calls = []

    async def connect(uri, *, proxy=None, **kwargs):
        calls.append(("ws", uri, proxy, kwargs))
        return "connection"

    def post(url, **kwargs):
        calls.append(("http", url, kwargs))
        return "endpoint"

    sdk = SimpleNamespace(websockets=SimpleNamespace(connect=connect), requests=SimpleNamespace(post=post))
    handshakes = []
    configure_sdk_proxy(sdk, connected=lambda: handshakes.append(True))
    assert (
        asyncio.run(sdk.websockets.connect("wss://example.feishu.cn/events", proxy=None, open_timeout=10))
        == "connection"
    )
    assert handshakes == [True]
    assert sdk.requests.post("https://open.feishu.cn/callback/ws/endpoint", json={}) == "endpoint"
    assert calls[0][2] == "http://proxy.example:8188"
    assert calls[0][3]["open_timeout"] == 10
    assert calls[1][2]["proxies"]["https"] == "http://proxy.example:8188"
    assert calls[1][2]["timeout"] == (3, 10)
    assert calls[1][2]["allow_redirects"] is False


def test_sdk_proxy_fails_closed_on_unsupported_websockets(monkeypatch):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://proxy.example:8188")
    sdk = SimpleNamespace(websockets=SimpleNamespace(connect=lambda uri: None))
    with pytest.raises(RuntimeError, match="websockets 15"):
        configure_sdk_proxy(sdk)


def test_failed_handshake_does_not_report_connected(monkeypatch):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://proxy.example:8188")
    handshakes = []

    async def connect(uri, *, proxy=None):
        raise OSError("test connection failed")

    sdk = SimpleNamespace(websockets=SimpleNamespace(connect=connect), requests=SimpleNamespace(post=lambda: None))
    configure_sdk_proxy(sdk, connected=lambda: handshakes.append(True))
    with pytest.raises(OSError):
        asyncio.run(sdk.websockets.connect("wss://example.feishu.cn/events"))
    assert handshakes == []


def test_no_proxy_preserves_sdk_and_http_defaults(monkeypatch):
    monkeypatch.delenv("LOCKBOT_FEISHU_PROXY_URL", raising=False)
    sdk = SimpleNamespace()
    configure_sdk_proxy(sdk)
    assert vars(sdk) == {}
    assert http_options() == {}
