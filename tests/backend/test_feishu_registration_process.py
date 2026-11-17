import asyncio
import json
import sys
from io import BytesIO, TextIOWrapper
from urllib.parse import parse_qs

import pytest
from lockbot.backend.app.feishu import registration_process as worker


def test_feishu_registration_uses_the_pinned_sdk_without_network(monkeypatch):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("lark_oapi.scene.registration")
    payloads = {
        "init": {"supported_auth_methods": ["client_secret"]},
        "begin": {
            "device_code": "synthetic-device",
            "verification_uri_complete": "https://accounts.feishu.cn/verify?code=synthetic",
            "expires_in": 120,
        },
        "poll": {"client_id": "cli_test", "client_secret": "synthetic-secret", "user_info": {"tenant_brand": "feishu"}},
    }
    actions = []

    def respond(request):
        assert request.url.host == "accounts.feishu.cn"
        assert request.method == "POST"
        action = parse_qs(request.content.decode())["action"][0]
        actions.append(action)
        return httpx.Response(200, json=payloads[action])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(respond)))
    messages = []
    asyncio.run(worker.register("Test application", messages.append))

    assert actions == ["init", "begin", "poll"]
    assert messages[0]["status"] == "waiting"
    assert messages[0]["expires_in"] == 120
    assert messages[0]["url"].startswith("https://accounts.feishu.cn/verify?")
    assert messages[-1] == {"status": "confirmed", "app_id": "cli_test", "app_secret": "synthetic-secret"}


def test_registration_only_requests_selected_scopes_and_does_not_update_apps(monkeypatch):
    sdk = pytest.importorskip("lark_oapi")

    async def fake_registration(**options):
        assert options["create_only"] is True
        assert "app_id" not in options
        assert options["addons"] == {
            "preset": False,
            "scopes": {"tenant": ["im:message.group_at_msg:readonly", "im:message:send_as_bot", "im:chat:read"]},
            "events": {"items": {"tenant": ["im.message.receive_v1"]}},
        }
        options["on_status_change"]({"status": "domain_switched"})

    monkeypatch.setattr(sdk, "aregister_app", fake_registration)
    with pytest.raises(ValueError, match="Feishu tenant"):
        asyncio.run(worker.register("Test application", lambda _: pytest.fail("Unexpected output")))


def test_timeout_cancels_sdk_polling(monkeypatch):
    sdk = pytest.importorskip("lark_oapi")
    messages = []
    stopped = []

    async def stalled(**options):
        options["on_qr_code"]({"url": "https://accounts.feishu.cn/verify", "expire_in": 120})
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    monkeypatch.setattr(sdk, "aregister_app", stalled)
    monkeypatch.setattr(worker, "REGISTRATION_TIMEOUT", 0.05)
    asyncio.run(worker.register("Test application", messages.append))
    assert stopped == [True]
    assert [message["status"] for message in messages] == ["waiting", "expired"]


@pytest.mark.parametrize(
    "url", ["http://accounts.feishu.cn/", "https://accounts.feishu.cn.invalid/", "https://user@accounts.feishu.cn/"]
)
def test_rejects_untrusted_confirmation_url(url):
    with pytest.raises(ValueError):
        worker.confirmation_url(url)


def test_proxy_configuration_clears_conflicting_environment_only_when_overridden(monkeypatch):
    for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "ambient")
    worker.configure_registration_proxy(None)
    assert worker.os.environ["https_proxy"] == "ambient"

    monkeypatch.setenv("HTTPS_PROXY", "ambient")
    worker.configure_registration_proxy("http://example.test:8080")
    assert worker.os.environ["HTTPS_PROXY"] == "http://example.test:8080"
    assert not any(name in worker.os.environ for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "no_proxy"))

    worker.configure_registration_proxy("")
    assert "HTTPS_PROXY" not in worker.os.environ


def test_worker_does_not_echo_error_details(monkeypatch, capsys):
    async def fail(_name, _send):
        print("synthetic credential")
        raise ValueError("synthetic credential")

    monkeypatch.setattr(worker, "register", fail)
    monkeypatch.setattr(sys, "stdin", TextIOWrapper(BytesIO(b'{"name": "Fixture"}\n')))
    worker.main()
    capture = capsys.readouterr()
    assert json.loads(capture.out) == {"status": "failed"}
    assert not capture.err
